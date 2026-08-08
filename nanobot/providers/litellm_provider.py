"""LiteLLM provider implementation for multi-provider support."""

import hashlib
import os
import secrets
import string
from pathlib import Path
from typing import Any

import json_repair
import litellm
from litellm import acompletion
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.cache_probe import estimate_cache_ratio
from nanobot.providers.registry import find_by_model, find_gateway

# Standard chat-completion message keys.
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"})
_ANTHROPIC_EXTRA_KEYS = frozenset({"thinking_blocks"})
_ALNUM = string.ascii_letters + string.digits

def _short_tool_id() -> str:
    """Generate a 9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str | None = None,
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        retry_delays: tuple[int, ...] | None = None,
    ):
        super().__init__(api_key, api_base, retry_delays=retry_delays)
        # When no explicit default is passed, inherit the configured model from
        # ~/.nanobot/config.json (agents.defaults.model) at construction time so
        # the provider never silently falls back to a stale hardcoded model.
        if default_model is None:
            default_model = self._inherited_default_model()
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Runtime auto-detected provider capabilities (inherited from LLMProvider)
        # Pre-seed known param incompatibilities
        self._compat_drop_params.update({
            "xai": {"presence_penalty", "frequency_penalty", "repetition_penalty", "top_k", "min_p", "top_a"},
            "闲鱼api": {"presence_penalty", "frequency_penalty", "repetition_penalty", "top_k", "min_p", "top_a"},
        })

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Suppress langfuse import errors
        import os
        os.environ["LITELLM_LOG"] = "ERROR"
        litellm.success_callback = []
        litellm.failure_callback = []
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True
        litellm.modify_params = True
        # Enable response headers so we can extract OpenRouter trace/cache info
        litellm.return_response_headers = True

        # Langfuse tracing via LiteLLM OTEL callback (langfuse v3+/v4 compatible)
        if os.environ.get("LANGFUSE_PUBLIC_KEY"):
            try:
                import langfuse  # noqa: F401
                litellm.success_callback = ["langfuse"]
                litellm.failure_callback = ["langfuse"]
                logger.info("Langfuse tracing enabled (OTEL mode)")
            except ImportError:
                logger.warning("LANGFUSE_PUBLIC_KEY set but langfuse not installed, skipping")
                os.environ.pop("LANGFUSE_PUBLIC_KEY", None)

        # Sampling parameters — modifiable at runtime via /hyperparams
        defaults = {
            "temperature": 0.95,
            "top_p": 0.92,
            "top_k": 40,
            "min_p": 0.07,
            "repetition_penalty": 1.15,
            "frequency_penalty": 0.10,
            "presence_penalty": 0.05,
            "top_a": 0,
        }
        # Load saved hyperparams from disk
        hp_path = Path.home() / ".nanobot" / "hyperparams.json"
        if hp_path.exists():
            try:
                import json as _json
                saved = _json.loads(hp_path.read_text())
                defaults.update(saved)
                logger.info("Loaded saved hyperparams from {}", hp_path)
            except Exception:
                pass
        self.sampling_params: dict[str, float] = defaults

        self._langsmith_enabled = bool(os.getenv("LANGSMITH_API_KEY"))

    @staticmethod
    def _inherited_default_model() -> str:
        """Resolve the provider's default model from the main config, falling back
        to a conservative last-resort — never a silently stale hardcode first."""
        try:
            import json as _json
            from pathlib import Path
            cfg_path = Path.home() / ".nanobot" / "config.json"
            if cfg_path.exists():
                cfg = _json.loads(cfg_path.read_text())
                val = (cfg.get("agents", {}).get("defaults", {}) or {}).get("model")
                if val:
                    return str(val)
        except Exception:
            pass
        return "anthropic/claude-opus-4-5"

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix:
                model = f"{prefix}/{model}"
            return model

        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            model = self._canonicalize_explicit_prefix(model, spec.name, spec.litellm_prefix)
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    @staticmethod
    def _canonicalize_explicit_prefix(model: str, spec_name: str, canonical_prefix: str) -> str:
        """Normalize explicit provider prefixes like `github-copilot/...`."""
        if "/" not in model:
            return model
        prefix, remainder = model.split("/", 1)
        if prefix.lower().replace("-", "_") != spec_name:
            return model
        return f"{canonical_prefix}/{remainder}"

    def _supports_cache_control(self, model: str) -> bool:
        """Return True when the provider supports cache_control on content blocks.
        
        Gateways (e.g. OpenRouter) may support prompt caching for some backend
        models (Anthropic) but not others (Zhipu/GLM).  We require BOTH the
        gateway AND the underlying model's native provider to opt in.

        Providers with "automatic" cache mode (e.g. DeepSeek) do NOT need
        explicit cache_control breakpoints — injecting them would alter the
        content structure and potentially break the provider's native prefix
        matching.
        """
        if self._gateway is not None:
            # Gateway says yes — but does the *underlying* model's provider?
            native_spec = find_by_model(model)
            if native_spec is not None:
                if not native_spec.supports_prompt_caching:
                    return False
                if native_spec.cache_control_mode == "automatic":
                    return False
            return self._gateway.supports_prompt_caching
        spec = find_by_model(model)
        if spec is None or not spec.supports_prompt_caching:
            return False
        return spec.cache_control_mode == "explicit"

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies of messages and tools with cache_control injected.

        Anthropic (and Azure) support at most 4 cache_control breakpoints total.
        tools gets 1 breakpoint, leaving at most 3 for system messages.
        We mark only the last N system messages to stay within this limit.
        """
        _MAX_CACHE_BLOCKS = 4
        sys_quota = _MAX_CACHE_BLOCKS - (1 if tools else 0)

        # Collect indices of system messages (in order)
        sys_indices = [i for i, m in enumerate(messages) if m.get("role") == "system"]
        # Only cache the last sys_quota system messages
        cacheable = set(sys_indices[-sys_quota:]) if sys_quota > 0 else set()

        new_messages = []
        for i, msg in enumerate(messages):
            if i in cacheable:
                content = msg["content"]
                if isinstance(content, str):
                    new_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    new_content = list(content)
                    new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}

        return new_messages, new_tools

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    @staticmethod
    def _log_request(
        kwargs: dict[str, Any],
        response: Any | None = None,
        error: Exception | None = None,
        latency: float = 0.0,
        cache_headers: dict | None = None,
    ) -> None:
        """Log every LLM request to ~/.nanobot/request_logs/YYYY-MM-DD.jsonl.

        Each line is a JSON object with request kwargs (full message content),
        response summary, and error info if any.
        """
        import json as _json
        import time as _time

        try:
            log_dir = Path.home() / ".nanobot" / "request_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{_time.strftime('%Y-%m-%d')}.jsonl"

            # ── Request ──
            meta = kwargs.get("metadata") or {}
            record: dict[str, Any] = {
                "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "agent": meta.get("log_agent"),
                "session": meta.get("log_session"),
                "topic": meta.get("log_topic"),
                "mode": meta.get("log_mode"),
                "model": kwargs.get("model"),
                "api_base": kwargs.get("api_base"),
                "max_tokens": kwargs.get("max_tokens"),
                "stream": kwargs.get("stream", False),
                "params": {
                    k: kwargs.get(k) for k in
                    ("temperature", "top_p", "top_k", "min_p",
                     "repetition_penalty", "frequency_penalty",
                     "presence_penalty", "top_a",
                     "reasoning_effort")
                    if k in kwargs
                },
                "tools_count": len(kwargs.get("tools", []) or []),
            }

            # Full messages (complete content, not truncated)
            msgs_log: list[dict[str, Any]] = []
            total_chars = 0
            for msg in kwargs.get("messages", []):
                content = msg.get("content")
                if isinstance(content, str):
                    c_len = len(content)
                elif isinstance(content, list):
                    c_len = sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
                else:
                    c_len = 0
                total_chars += c_len

                entry: dict[str, Any] = {
                    "role": msg.get("role"),
                    "content": content,           # full content
                    "content_len": c_len,
                }
                if msg.get("name"):
                    entry["name"] = msg["name"]
                if msg.get("tool_call_id"):
                    entry["tool_call_id"] = msg["tool_call_id"]
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                msgs_log.append(entry)

            record["messages"] = msgs_log
            record["total_chars"] = total_chars
            record["msg_count"] = len(msgs_log)
            record["latency"] = round(latency, 2)
            if cache_headers:
                record["cache_probe"] = cache_headers

            # ── Response ──
            if error:
                record["status"] = "error"
                record["error"] = str(error)
                record["error_type"] = type(error).__name__
                record["status_code"] = getattr(error, "status_code", None)
            elif response is not None:
                record["status"] = "ok"
                # Extract response summary
                if hasattr(response, "choices") and response.choices:
                    ch = response.choices[0]
                    msg = ch.message
                    record["reply_len"] = len(msg.content) if msg.content else 0
                    record["reply_preview"] = (msg.content or "")[:500]
                    record["finish_reason"] = ch.finish_reason
                    record["has_tool_calls"] = bool(
                        hasattr(msg, "tool_calls") and msg.tool_calls
                    )
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        record["reply_tool_calls"] = [
                            {"name": tc.function.name, "args_len": len(tc.function.arguments or "")}
                            for tc in msg.tool_calls
                        ]
                if hasattr(response, "usage") and response.usage:
                    record["usage"] = {
                        "prompt": response.usage.prompt_tokens,
                        "completion": response.usage.completion_tokens,
                        "total": response.usage.total_tokens,
                    }
                # ── Extract OpenRouter / provider IDs from hidden params ──
                try:
                    _hidden = getattr(response, "_hidden_params", None) or {}
                    _ah = _hidden.get("additional_headers", {}) or {}
                    for hdr, field_name in [
                        ("x-openrouter-generation-id", "generation_id"),
                        ("x-request-id", "request_id"),
                        ("x-openrouter-provider", "or_provider"),
                        ("x-openrouter-caching", "or_caching"),
                    ]:
                        val = _ah.get(hdr)
                        if val:
                            record[field_name] = val
                except Exception:
                    pass
            else:
                record["status"] = "unknown"

            # Append as one JSON line
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False, default=str) + "\n")

        except Exception as log_err:
            logger.debug("Request logging failed: {}", log_err)

    def _log_stream_request(
        self,
        kwargs: dict[str, Any],
        content: str,
        tool_calls: list,
        finish_reason: str,
        usage: dict,
        latency: float,
        cache_headers: dict | None = None,
    ) -> None:
        """Log a completed streaming request (no raw response object available)."""
        import json as _json
        import time as _time

        try:
            log_dir = Path.home() / ".nanobot" / "request_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{_time.strftime('%Y-%m-%d')}.jsonl"

            # Build the same record structure as _log_request
            meta = kwargs.get("metadata") or {}
            record: dict[str, Any] = {
                "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "agent": meta.get("log_agent"),
                "session": meta.get("log_session"),
                "topic": meta.get("log_topic"),
                "mode": meta.get("log_mode"),
                "model": kwargs.get("model"),
                "api_base": kwargs.get("api_base"),
                "max_tokens": kwargs.get("max_tokens"),
                "stream": True,
                "params": {
                    k: kwargs.get(k) for k in
                    ("temperature", "top_p", "top_k", "min_p",
                     "repetition_penalty", "frequency_penalty",
                     "presence_penalty", "top_a",
                     "reasoning_effort")
                    if k in kwargs
                },
                "tools_count": len(kwargs.get("tools", []) or []),
            }

            # Full messages
            msgs_log: list[dict[str, Any]] = []
            total_chars = 0
            for msg in kwargs.get("messages", []):
                c = msg.get("content")
                if isinstance(c, str):
                    c_len = len(c)
                elif isinstance(c, list):
                    c_len = sum(len(b.get("text", "")) for b in c if isinstance(b, dict))
                else:
                    c_len = 0
                total_chars += c_len
                entry: dict[str, Any] = {
                    "role": msg.get("role"),
                    "content": c,
                    "content_len": c_len,
                }
                if msg.get("name"):
                    entry["name"] = msg["name"]
                if msg.get("tool_call_id"):
                    entry["tool_call_id"] = msg["tool_call_id"]
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                msgs_log.append(entry)

            record["messages"] = msgs_log
            record["total_chars"] = total_chars
            record["msg_count"] = len(msgs_log)
            record["latency"] = round(latency, 2)
            if cache_headers:
                record["cache_probe"] = cache_headers

            # Response
            record["status"] = "ok"
            record["reply_len"] = len(content) if content else 0
            record["reply_preview"] = (content or "")[:500]
            record["finish_reason"] = finish_reason
            record["has_tool_calls"] = bool(tool_calls)
            if tool_calls:
                record["reply_tool_calls"] = [
                    {"name": tc.name, "args_preview": str(tc.arguments)[:200]}
                    for tc in tool_calls
                ]
            if usage:
                record["usage"] = usage


            with open(log_file, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False, default=str) + "\n")

        except Exception as log_err:
            logger.debug("Stream request logging failed: {}", log_err)

    @staticmethod
    def _extra_msg_keys(original_model: str, resolved_model: str) -> frozenset[str]:
        """Return provider-specific extra keys to preserve in request messages."""
        spec = find_by_model(original_model) or find_by_model(resolved_model)
        if (spec and spec.name == "anthropic") or "claude" in original_model.lower() or resolved_model.startswith("anthropic/"):
            return _ANTHROPIC_EXTRA_KEYS
        return frozenset()

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        """Normalize tool_call_id to a provider-safe 9-char alphanumeric form."""
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]], extra_keys: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key."""
        allowed = _ALLOWED_MSG_KEYS | extra_keys
        sanitized = LLMProvider._sanitize_request_messages(messages, allowed)
        id_map: dict[str, str] = {}

        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return id_map.setdefault(value, LiteLLMProvider._normalize_tool_call_id(value))

        for clean in sanitized:
            # Keep assistant tool_calls[].id and tool tool_call_id in sync after
            # shortening, otherwise strict providers reject the broken linkage.
            if isinstance(clean.get("tool_calls"), list):
                normalized_tool_calls = []
                for tc in clean["tool_calls"]:
                    if not isinstance(tc, dict):
                        normalized_tool_calls.append(tc)
                        continue
                    tc_clean = dict(tc)
                    tc_clean["id"] = map_id(tc_clean.get("id"))
                    normalized_tool_calls.append(tc_clean)
                clean["tool_calls"] = normalized_tool_calls

            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

    # Providers that LiteLLM knows natively — no model rewriting needed
    _NATIVE_PROVIDERS = frozenset({
        "openrouter", "anthropic", "openai", "deepseek", "groq",
        "gemini", "dashscope", "zhipu", "minimax", "moonshot",
        "siliconflow", "volcengine", "aihubmix",
    })

    def _resolve_pm_overrides(self, model: str) -> dict[str, str | None]:
        """Resolve api_base/api_key/model from ~/.nanobot/providers_models.json.

        Uses ``nanobot.providers.model_match.resolve_provider`` (exact → prefix
        → normalized-family cascade). When nothing matches, logs an explicit
        warning and returns all-None; the caller surfaces the failure instead of
        silently routing to a hardcoded default model.
        """
        from nanobot.providers.model_match import resolve_provider

        import json as _json
        from pathlib import Path
        pm_path = Path.home() / ".nanobot" / "providers_models.json"
        if not pm_path.exists():
            return {"api_base": None, "api_key": None, "model": None, "provider_name": None}
        try:
            pm = _json.loads(pm_path.read_text())
        except Exception:
            return {"api_base": None, "api_key": None, "model": None, "provider_name": None}

        hit = resolve_provider(pm, model, native_providers=self._NATIVE_PROVIDERS)
        if hit is None:
            logger.warning(
                "Provider routing: model '{}' matches no provider in providers_models.json "
                "(exact/prefix/family). Will fall back to LiteLLM default — check the "
                "provider's model list or add this model via /editprovider.",
                model,
            )
            return {"api_base": None, "api_key": None, "model": None, "provider_name": None}
        return {
            "api_base": hit.get("api_base"),
            "api_key": hit.get("api_key"),
            "model": hit.get("model"),
            "provider_name": hit.get("provider_name"),
        }


    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        metadata: dict[str, Any] | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Build the kwargs dict for acompletion (shared by chat and chat_stream)."""
        original_model = model or self.default_model

        # Auto-resolve provider from providers_models.json
        pm_api_base = api_base
        pm_api_key = api_key
        pm_resolved = False
        pm_provider_name = None
        if not api_base and not api_key:
            resolved = self._resolve_pm_overrides(original_model)
            if resolved["api_base"] or resolved["api_key"] or resolved["model"]:
                pm_api_base = resolved["api_base"]
                pm_api_key = resolved["api_key"]
                if resolved["model"]:
                    pm_resolved = True
                    original_model = resolved["model"]
                    logger.debug("PM override: {} → {} via {}", model, original_model, pm_api_base)
            pm_provider_name = resolved.get("provider_name")

        if pm_resolved:
            model = original_model
        else:
            model = self._resolve_model(original_model)
        extra_msg_keys = self._extra_msg_keys(original_model, model)

        if self._supports_cache_control(original_model):
            messages, tools = self._apply_cache_control(messages, tools)
            # Estimate cache hit ratio from breakpoints
            try:
                _probe = estimate_cache_ratio(messages, tools)
                self._last_cache_headers = _probe.request_headers
                logger.debug(
                    "cache probe ({}): {}", original_model, _probe.summary
                )
            except Exception as _pe:
                logger.debug("cache probe failed: {}", _pe)
                self._last_cache_headers = {}
        else:
            self._last_cache_headers = {}

        max_tokens = max(1, max_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(self._sanitize_empty_content(messages), extra_keys=extra_msg_keys),
            "max_tokens": max_tokens,
            **self.sampling_params,
        }

        self._apply_model_overrides(model, kwargs)

        # ── Provider-specific message transformations ──

        # Flatten tool messages for providers that don't support the
        # OpenAI tool-message protocol (tool role, tool_calls on assistant).
        # Auto-detected at runtime OR configured via "flattenTools" in providers_models.json.
        if pm_provider_name:
            _needs_flatten = pm_provider_name in self._compat_flatten
            if not _needs_flatten:
                import json as _json
                _pm_path = Path.home() / ".nanobot" / "providers_models.json"
                try:
                    pm = _json.loads(_pm_path.read_text()) if _pm_path.exists() else {}
                except Exception:
                    pm = {}
                prov_cfg = pm.get("providers", {}).get(pm_provider_name, {})
                _needs_flatten = prov_cfg.get("flattenTools", False)
            if _needs_flatten:
                kwargs["messages"] = self._flatten_tool_messages(kwargs["messages"], flatten_tools=True)

        # Auto-detected param drops — providers that reject specific params.
        # Populated at runtime when a 400 error mentions the param name.
        # Check both the resolved provider name and the fallback model-prefix key.
        _drop_keys_to_check = set()
        if pm_provider_name:
            _drop_keys_to_check.add(pm_provider_name)
        # Fallback: derive provider key from model name (mirrors 400 handler logic)
        _m = kwargs.get("model", "")
        if "/" in _m:
            _drop_keys_to_check.add(_m.split("/")[0])
        elif "-" in _m:
            _drop_keys_to_check.add(_m.split("-")[0])
        for _dk in _drop_keys_to_check:
            if _dk in self._compat_drop_params:
                for k in self._compat_drop_params[_dk]:
                    kwargs.pop(k, None)

        api_base = pm_api_base
        api_key = pm_api_key

        override_key = api_key or self.api_key
        if override_key:
            kwargs["api_key"] = override_key

        override_base = api_base or self.api_base
        if override_base:
            kwargs["api_base"] = override_base

        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["drop_params"] = True

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            kwargs["parallel_tool_calls"] = True

        if metadata:
            kwargs["metadata"] = metadata

        # Hard timeout to prevent OpenRouter cold-start stalls (observed up to 135s).
        # Existing retry logic handles the resulting Timeout error gracefully.
        kwargs["timeout"] = 20

        if pm_provider_name == "openrouter" or (not pm_provider_name and "openrouter" in (model or "")):
            import hashlib
            # Stable cache key: prefer session_id for intra-session cache hits
            # Fallback to system prompt prefix hash for sessionless requests
            cache_key = None
            session_id = (metadata or {}).get("log_session")
            if session_id:
                cache_key = hashlib.md5(f"{session_id}:{model}".encode()).hexdigest()[:16]
            else:
                system_text = ""
                for msg in messages:
                    if msg.get("role") == "system":
                        content = msg.get("content")
                        if isinstance(content, str):
                            system_text = content[:2000]
                        elif isinstance(content, list):
                            system_text = "".join(
                                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                            )[:2000]
                        if system_text:
                            break
                if system_text:
                    cache_key = hashlib.md5(system_text.encode()).hexdigest()[:16]
            extra = {
                "provider": {
                    "sort": "latency",
                    "order": ["nebius", "mistral", "groq", "fireworks", "deepseek"],
                    "allow_fallbacks": True,
                }
            }
            if cache_key and self._supports_cache_control(original_model):
                extra["prompt_cache_key"] = f"nanobot-{cache_key}"
            kwargs["extra_body"] = extra

        return kwargs

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        metadata: dict[str, Any] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request via LiteLLM."""
        import time as _time
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, reasoning_effort, metadata, api_base, api_key,
        )
        # Override tool_choice if explicitly passed
        if tool_choice and tools:
            kwargs["tool_choice"] = tool_choice
        t0 = _time.time()
        try:
            response = await acompletion(**kwargs)
            self._log_request(kwargs, response=response, latency=_time.time() - t0,
                              cache_headers=getattr(self, "_last_cache_headers", None))
            return self._parse_response(response)
        except Exception as e:
            self._log_request(kwargs, error=e, latency=_time.time() - t0,
                              cache_headers=getattr(self, "_last_cache_headers", None))

            sc = getattr(e, "status_code", None)
            has_tool_msgs = any(m.get("role") == "tool" for m in messages)
            
            resolved = self._resolve_pm_overrides(model or self.default_model)
            prov = resolved.get("provider_name")
            if not prov:
                _m = model or self.default_model or ""
                prov = _m.split("/")[0] if "/" in _m else _m.split("-")[0] if "-" in _m else _m
                if prov:
                    logger.debug("Using fallback provider key: {}", prov)

            needs_flatten, needs_param = self._detect_compat_issues(
                provider_key=prov,
                status_code=sc,
                error_text=str(e),
                has_tool_msgs=has_tool_msgs,
                kwargs=kwargs,
            )

            if needs_flatten or needs_param:
                return await self.chat(
                    messages=messages, tools=tools, model=model,
                    max_tokens=max_tokens, temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    metadata=metadata, api_base=api_base, api_key=api_key,
                )

            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        metadata: dict[str, Any] | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        """Stream a chat completion, yielding text deltas.

        Yields:
            str — content delta tokens as they arrive.

        Returns the full LLMResponse (access via ``async for ... in chat_stream()``
        and then ``result = gen.asend(None)`` or use the helper in tool_loop).

        If the response contains tool_calls, streaming is silently collected
        and the *last* yielded value is a complete ``LLMResponse``.
        """
        import time as _time
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, reasoning_effort, metadata, api_base, api_key,
        )
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        t0 = _time.time()
        try:
            response = await acompletion(**kwargs)
        except Exception as e:
            self._log_request(kwargs, error=e, latency=_time.time() - t0,
                              cache_headers=getattr(self, "_last_cache_headers", None))
            sc = getattr(e, "status_code", None)
            has_tool_msgs = any(m.get("role") == "tool" for m in messages)
            
            resolved = self._resolve_pm_overrides(model or self.default_model)
            prov = resolved.get("provider_name")
            if not prov:
                _m = model or self.default_model or ""
                prov = _m.split("/")[0] if "/" in _m else _m.split("-")[0] if "-" in _m else _m
                if prov:
                    logger.debug("Using fallback provider key: {}", prov)

            needs_flatten, needs_param = self._detect_compat_issues(
                provider_key=prov,
                status_code=sc,
                error_text=str(e),
                has_tool_msgs=has_tool_msgs,
                kwargs=kwargs,
            )

            if needs_flatten or needs_param:
                async for item in self.chat_stream(
                    messages=messages, tools=tools, model=model,
                    max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                    metadata=metadata, api_base=api_base, api_key=api_key,
                ):
                    yield item
                return

            yield LLMResponse(content=f"Error calling LLM: {str(e)}", finish_reason="error")
            return

        # Collect the stream
        full_content = ""
        tool_calls_raw: list[dict] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}
        has_tool_calls = False
        _stream_cost: float | None = None
        _stream_cache_tokens: int = 0
        _stream_meta: dict = {}

        try:
            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Finish reason
                fr = chunk.choices[0].finish_reason
                if fr:
                    finish_reason = fr

                # Content delta
                if delta.content:
                    full_content += delta.content
                    if not has_tool_calls:
                        yield delta.content

                # Tool call deltas — collect silently
                tc_list = getattr(delta, "tool_calls", None)
                if tc_list:
                    has_tool_calls = True
                    for tc_delta in tc_list:
                        idx = getattr(tc_delta, "index", 0) or 0
                        while len(tool_calls_raw) <= idx:
                            tool_calls_raw.append({"id": "", "name": "", "arguments": ""})
                        entry = tool_calls_raw[idx]
                        tc_id = getattr(tc_delta, "id", None)
                        if tc_id:
                            entry["id"] = tc_id
                        fn = getattr(tc_delta, "function", None)
                        if fn:
                            fn_name = getattr(fn, "name", None)
                            fn_args = getattr(fn, "arguments", None)
                            if fn_name:
                                entry["name"] = fn_name
                            if fn_args:
                                entry["arguments"] += fn_args

                # Usage (some providers send it in the last chunk)
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = {
                        "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                    }
                    # Also extract cache tokens from prompt_tokens_details
                    ptd = getattr(chunk.usage, "prompt_tokens_details", None)
                    if ptd:
                        _stream_cache = getattr(ptd, "cached_tokens", 0) or 0
                        if _stream_cache:
                            _stream_cache_tokens = _stream_cache

                # Cost from hidden params (available on last chunk)
                _chunk_hidden = getattr(chunk, "_hidden_params", None) or {}
                if _chunk_hidden.get("response_cost") is not None:
                    _stream_cost = _chunk_hidden["response_cost"]
                # Also check additional_headers for OpenRouter
                if not _chunk_hidden.get("response_cost"):
                    _ah = _chunk_hidden.get("additional_headers", {}) or {}
                    for hdr, mk in [
                        ("x-openrouter-provider", "_or_provider"),
                        ("x-openrouter-generation-id", "_or_gen_id"),
                    ]:
                        if _ah.get(hdr) and not _stream_meta.get(mk):
                            _stream_meta[mk] = _ah[hdr]
        except Exception as e:
            yield LLMResponse(content=f"Error during streaming: {str(e)}", finish_reason="error")
            return

        # Build tool calls — try to infer names when empty (Claude streaming bug)
        # Build a quick lookup: arg-keys-tuple → tool name from definitions
        _tool_by_args: dict[frozenset, str] = {}
        if tools:
            for tdef in tools:
                fn = tdef.get("function", {})
                params = fn.get("parameters", {}).get("properties", {})
                if params and fn.get("name"):
                    _tool_by_args[frozenset(params.keys())] = fn["name"]

        parsed_tool_calls = []
        for tc in tool_calls_raw:
            args = tc["arguments"]
            if isinstance(args, str):
                if not args.strip():
                    args = {}
                else:
                    args = json_repair.loads(args)
                    if not isinstance(args, dict):
                        args = {}
            name = tc["name"] or ""
            if not name and isinstance(args, dict) and args:
                # Try to infer tool name from argument keys
                arg_keys = frozenset(args.keys())
                inferred = _tool_by_args.get(arg_keys)
                if inferred:
                    name = inferred
                    logger.info("Inferred tool name '{}' from args keys {}", name, list(arg_keys))
                else:
                    # Try partial match (args is a subset of tool params)
                    for param_keys, tool_name in _tool_by_args.items():
                        if arg_keys <= param_keys:
                            name = tool_name
                            logger.info("Inferred tool name '{}' from partial args match", name)
                            break
            if not name:
                name = "_unknown_"
                logger.warning("Tool call with empty name detected: args={}", tc["arguments"][:100])
            parsed_tool_calls.append(ToolCallRequest(
                id=_short_tool_id(),
                name=name,
                arguments=args,
            ))

        # Filter out ghost tool calls (empty name + empty args from streaming artifacts)
        valid_tool_calls = [tc for tc in parsed_tool_calls if tc.name != "_unknown_" or tc.arguments]
        if valid_tool_calls and not any(tc.name != "_unknown_" for tc in valid_tool_calls):
            # All remaining are still _unknown_ — keep them for fallback detection
            pass
        elif valid_tool_calls != parsed_tool_calls:
            logger.info("Filtered {} ghost tool calls", len(parsed_tool_calls) - len(valid_tool_calls))
            parsed_tool_calls = valid_tool_calls

        # If we successfully inferred names, set finish reason to tool_calls
        if parsed_tool_calls and all(tc.name != "_unknown_" for tc in parsed_tool_calls):
            finish_reason = "tool_calls"

        # Log the completed stream request
        self._log_stream_request(kwargs, full_content, parsed_tool_calls, finish_reason, usage,
                                 _time.time() - t0,
                                 cache_headers=getattr(self, "_last_cache_headers", None))

        # Build provider_meta list for logging
        _provider_meta = []
        if _stream_meta:
            _provider_meta.append(_stream_meta)

        # Yield the final complete LLMResponse
        yield LLMResponse(
            content=full_content or None,
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            cost=_stream_cost,
            cache_tokens=_stream_cache_tokens,
            provider_meta=_provider_meta if _provider_meta else None,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        if response is None or not hasattr(response, "choices") or not response.choices:
            return LLMResponse(
                content="Error: provider returned empty response",
                finish_reason="error",
            )
        choice = response.choices[0]
        message = choice.message
        content = message.content
        finish_reason = choice.finish_reason

        # Some providers (e.g. GitHub Copilot) split content and tool_calls
        # across multiple choices. Merge them so tool_calls are not lost.
        raw_tool_calls = []
        for ch in response.choices:
            msg = ch.message
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                raw_tool_calls.extend(msg.tool_calls)
                if ch.finish_reason in ("tool_calls", "stop"):
                    finish_reason = ch.finish_reason
            if not content and msg.content:
                content = msg.content

        if len(response.choices) > 1:
            logger.debug("LiteLLM response has {} choices, merged {} tool_calls",
                         len(response.choices), len(raw_tool_calls))

        tool_calls = []
        for tc in raw_tool_calls:
            # Parse arguments from JSON string if needed
            args = tc.function.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)

            provider_specific_fields = getattr(tc, "provider_specific_fields", None) or None
            function_provider_specific_fields = (
                getattr(tc.function, "provider_specific_fields", None) or None
            )

            tool_calls.append(ToolCallRequest(
                id=_short_tool_id(),
                name=tc.function.name,
                arguments=args,
                provider_specific_fields=provider_specific_fields,
                function_provider_specific_fields=function_provider_specific_fields,
            ))

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # Extract cost from litellm hidden params
        _cost = None
        try:
            _hidden = getattr(response, "_hidden_params", None) or {}
            _cost = _hidden.get("response_cost")
            logger.info("litellm _hidden_params keys: {} cost: {}", list(_hidden.keys()), _cost)
        except Exception:
            pass

        # Extract cached tokens from prompt_tokens_details
        _cache_tokens = 0
        try:
            if hasattr(response, "usage") and response.usage:
                ptd = getattr(response.usage, "prompt_tokens_details", None)
                if ptd:
                    _cache_tokens = getattr(ptd, "cached_tokens", 0) or 0
        except Exception:
            pass

        # Extract provider metadata (OpenRouter, etc.)
        _provider_meta: dict[str, Any] = {}
        try:
            _hidden = getattr(response, "_hidden_params", None) or {}
            # Model/provider from response
            resp_model = getattr(response, "model", None)
            if resp_model:
                _provider_meta["model_id"] = resp_model
            # Additional headers from litellm
            headers = _hidden.get("additional_headers", {}) or {}
            for hdr_key, meta_key in [
                ("x-openrouter-provider", "provider"),
                ("x-openrouter-generation-id", "generation_id"),
                ("x-openrouter-latency", "latency_ms"),
                ("x-openrouter-tokens-per-second", "tps"),
            ]:
                val = headers.get(hdr_key)
                if val:
                    _provider_meta[meta_key] = val
            # Cost breakdown
            if _cost is not None:
                _provider_meta["final_cost"] = _cost
            # Reasoning tokens
            if hasattr(response, "usage") and response.usage:
                rtd = getattr(response.usage, "completion_tokens_details", None)
                if rtd:
                    reasoning = getattr(rtd, "reasoning_tokens", 0) or 0
                    if reasoning:
                        _provider_meta["reasoning_tokens"] = reasoning
            # Cache discount
            if _cache_tokens and _cost is not None:
                _provider_meta["cache_tokens"] = _cache_tokens
        except Exception:
            pass

        reasoning_content = getattr(message, "reasoning_content", None) or None
        thinking_blocks = getattr(message, "thinking_blocks", None) or None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
            cost=_cost,
            cache_tokens=_cache_tokens,
            provider_meta=_provider_meta,
        )

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
