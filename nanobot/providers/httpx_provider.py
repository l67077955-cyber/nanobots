"""Direct httpx provider — no litellm dependency.

Talks to OpenAI-compatible ``/v1/chat/completions`` endpoints using
``httpx.AsyncClient``.  Provider routing is handled via
``~/.nanobot/providers_models.json``.
"""

import hashlib
import json as _json
import os
import secrets
import string
import time as _time
from pathlib import Path
from typing import Any

import httpx
import json_repair
from loguru import logger

from nanobot.providers.cache_probe import estimate_cache_ratio

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.registry import find_by_model, find_gateway

# Standard chat-completion message keys.
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"})
_ANTHROPIC_EXTRA_KEYS = frozenset({"thinking_blocks"})
_ALNUM = string.ascii_letters + string.digits

_NATIVE_PROVIDERS = {"openrouter", "anthropic", "openai", "google", "google_genai", "xai"}


def _short_tool_id() -> str:
    """Generate a 9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


class HttpxProvider(LLMProvider):
    """LLM provider using httpx for direct API access.

    No litellm dependency — sends requests directly to
    OpenAI-compatible endpoints using ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        retry_delays: tuple[int, ...] | None = None,
    ):
        super().__init__(api_key, api_base, retry_delays=retry_delays)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        # Detect gateway / local deployment.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Runtime auto-detected provider capabilities (inherited from LLMProvider)

        # Sampling parameters — modifiable at runtime via /hyperparams
        defaults = {
            "temperature": 0.95,
            "top_p": 0.92,
            "frequency_penalty": 0.4,
            "presence_penalty": 0.25,
            "repetition_penalty": 1.15,
        }
        hp_path = Path.home() / ".nanobot" / "hyperparams.json"
        if hp_path.exists():
            try:
                saved = _json.loads(hp_path.read_text())
                if isinstance(saved, dict):
                    defaults.update(saved)
                    logger.info("Loaded saved hyperparams from {}", hp_path)
            except Exception:
                pass
        self.sampling_params: dict[str, float] = defaults

        # Shared httpx client (created lazily)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    def get_default_model(self) -> str:
        return self.default_model

    # ── Provider resolution ──

    @staticmethod
    def _load_pm() -> dict:
        """Load ~/.nanobot/providers_models.json."""
        pm_path = Path.home() / ".nanobot" / "providers_models.json"
        if not pm_path.exists():
            return {}
        try:
            return _json.loads(pm_path.read_text())
        except Exception:
            return {}

    def _resolve_provider(self, model: str) -> dict[str, str | None]:
        """Resolve api_base, api_key, raw model name, and provider name.

        Returns dict with keys: api_base, api_key, model (raw), provider_name.
        """
        pm = self._load_pm()

        # 1) Exact match in models lists
        for prov_name, model_list in pm.get("models", {}).items():
            if model in model_list:
                info = pm.get("providers", {}).get(prov_name, {})
                raw = model.split("/", 1)[1] if "/" in model else model
                return {
                    "api_base": (info.get("url") or "").rstrip("/"),
                    "api_key": info.get("apiKey"),
                    "model": raw,
                    "provider_name": prov_name,
                    "retry_delays": info.get("retryDelays"),
                    "flatten_tools": info.get("flattenTools", False),
                }

        # 2) Prefix match (e.g. "闲鱼api/model" -> provider "闲鱼api")
        prefix = model.split("/")[0] if "/" in model else ""
        if prefix and prefix in pm.get("providers", {}):
            info = pm["providers"][prefix]
            raw = model.split("/", 1)[1] if "/" in model else model
            return {
                "api_base": (info.get("url") or "").rstrip("/"),
                "api_key": info.get("apiKey"),
                "model": raw,
                "provider_name": prefix,
                "retry_delays": info.get("retryDelays"),
                "flatten_tools": info.get("flattenTools", False),
            }

        return {"api_base": None, "api_key": None, "model": None, "provider_name": None}

    # ── Message processing ──

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    def _sanitize_messages(self, messages: list[dict[str, Any]], model: str = "") -> list[dict[str, Any]]:
        """Strip non-standard keys and normalize IDs."""
        extra_keys = _ANTHROPIC_EXTRA_KEYS if "claude" in model.lower() else frozenset()
        allowed = _ALLOWED_MSG_KEYS | extra_keys
        sanitized = LLMProvider._sanitize_request_messages(messages, allowed)

        id_map: dict[str, str] = {}
        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return id_map.setdefault(value, self._normalize_tool_call_id(value))

        for clean in sanitized:
            if isinstance(clean.get("tool_calls"), list):
                normalized = []
                for tc in clean["tool_calls"]:
                    if not isinstance(tc, dict):
                        normalized.append(tc)
                        continue
                    tc_clean = dict(tc)
                    tc_clean["id"] = map_id(tc_clean.get("id"))
                    normalized.append(tc_clean)
                clean["tool_calls"] = normalized
            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

    def _supports_cache_control(self, model: str) -> bool:
        """Return True when the provider supports cache_control on content blocks.
        
        Gateways (e.g. OpenRouter) may support prompt caching for some backend
        models (Anthropic, DeepSeek) but not others.
        """
        if self._gateway is not None:
            # If the gateway supports it, we allow it unless the native spec 
            # explicitly forbids it (False). If native_spec is None, we trust the gateway.
            native_spec = find_by_model(model)
            if native_spec is not None and native_spec.supports_prompt_caching is False:
                return False
            return self._gateway.supports_prompt_caching
        
        spec = find_by_model(model)
        return spec is not None and spec.supports_prompt_caching

    def _apply_cache_control(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies with cache_control injected for Anthropic prompt caching.

        Strategy (up to 4 breakpoints):

        BP-tools: Last tool definition — caches entire tool list (stable).
        BP1: Last message of the initial contiguous system-only prefix — covers
             the entire static system-prompt block, the most stable part of every
             request.  Anchored to the *first* non-system boundary, so later
             dynamic system messages (timestamps, nudges) injected mid-conversation
             do NOT shift this breakpoint.
        BP2: Message just before the most recent user message — caches the
             accumulated conversation history up to the current turn.

        Anchoring to semantic boundaries (not "last N system messages") prevents
        the cache invalidation that occurred when dynamically injected system
        messages shifted breakpoint positions every turn.
        """
        _MAX_CACHE_BLOCKS = 4
        remaining = _MAX_CACHE_BLOCKS
        cacheable: set[int] = set()

        # BP-tools: last tool definition
        new_tools = tools
        if tools and remaining > 0:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}
            remaining -= 1

        # BP1: end of stable system-prompt prefix (before first user/assistant msg)
        first_non_system = next(
            (i for i, m in enumerate(messages) if m.get("role") != "system"),
            len(messages),
        )
        if first_non_system > 0 and remaining > 0:
            cacheable.add(first_non_system - 1)
            remaining -= 1

        # BP2: end of conversation history (just before the latest user turn)
        user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if len(user_indices) >= 2 and remaining > 0:
            history_end = user_indices[-1] - 1
            if history_end >= 0 and history_end not in cacheable:
                cacheable.add(history_end)
                remaining -= 1

        # Apply cache_control markers
        new_messages: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i not in cacheable:
                new_messages.append(msg)
                continue
            content = msg.get("content")
            if content is None:
                new_messages.append(msg)
                continue
            if isinstance(content, str):
                new_content: list[dict[str, Any]] = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]
            elif isinstance(content, list) and content:
                new_content = list(content)
                new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
            else:
                new_messages.append(msg)
                continue
            new_messages.append({**msg, "content": new_content})

        return new_messages, new_tools

    @staticmethod
    def _flatten_tool_messages(messages: list[dict[str, Any]], flatten_tools: bool = False) -> list[dict[str, Any]]:
        """Convert tool-protocol messages to plain text for incompatible APIs."""
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "")

            if role == "assistant" and m.get("tool_calls"):
                content = m.get("content") or ""
                tc_lines: list[str] = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "unknown")
                    args = fn.get("arguments", "")
                    if isinstance(args, str) and len(args) > 100:
                        args = args[:100] + "…"
                    tc_lines.append(f"[调用 {name}({args})]")
                if tc_lines:
                    content = (content + "\n" + "\n".join(tc_lines)).strip()
                out.append({
                    k: v for k, v in {
                        "role": "assistant", "content": content, "name": m.get("name"),
                    }.items() if v is not None
                })
                continue

            if role == "tool":
                # Only flatten if explicitly requested by the provider config
                if flatten_tools:
                    result_text = m.get("content") or ""
                    out.append({"role": "assistant", "content": f"--- TOOL RESULT ---\n{result_text}"})
                else:
                    out.append(m)
                continue

            out.append(m)
        return out

    def _apply_model_overrides(self, model: str, params: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model.lower():
                    params.update(overrides)
                    return

    # ── Request body building ──

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        model: str,
        provider_name: str | None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the JSON request body for /chat/completions."""
        # Original model for cache_control check
        original_model = model

        # Apply cache control for Anthropic models
        cache_headers: dict = {}
        if self._supports_cache_control(original_model):
            messages, tools = self._apply_cache_control(messages, tools)
            # Estimate cache hit ratio from breakpoints
            try:
                _probe = estimate_cache_ratio(messages, tools)
                cache_headers = _probe.request_headers
                logger.debug(
                    "cache probe ({}): {}", original_model, _probe.summary
                )
            except Exception as _pe:
                logger.debug("cache probe failed: {}", _pe)
        # Store for _log_request access (ephemeral per-call)
        self._last_cache_headers = cache_headers

        # Sanitize messages
        messages = self._sanitize_empty_content(messages)
        messages = self._sanitize_messages(messages, model)

        # Flatten tool messages for incompatible providers
        if provider_name:
            needs_flatten = provider_name in self._compat_flatten
            if not needs_flatten:
                pm = self._load_pm()
                prov_cfg = pm.get("providers", {}).get(provider_name, {})
                needs_flatten = prov_cfg.get("flattenTools", False)
            if needs_flatten:
                messages = self._flatten_tool_messages(messages, flatten_tools=True)

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, max_tokens),
            "stream": stream,
        }

        # Add sampling params (skip unsupported ones for this provider)
        drop_keys = self._compat_drop_params.get(provider_name or "", set())
        for k, v in self.sampling_params.items():
            if k not in drop_keys:
                body[k] = v

        # Apply model-specific overrides
        self._apply_model_overrides(model, body)

        # Tools
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        # Stream options for usage reporting
        if stream:
            body["stream_options"] = {"include_usage": True}

        return body

    # ── Response parsing ──

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: list[dict] | None) -> list[ToolCallRequest]:
        """Parse tool_calls from API response."""
        if not raw_tool_calls:
            return []
        result = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "_unknown_")
            args_str = fn.get("arguments", "")
            if isinstance(args_str, str):
                if not args_str.strip():
                    args = {}
                else:
                    args = json_repair.loads(args_str)
                    if not isinstance(args, dict):
                        args = {}
            elif isinstance(args_str, dict):
                args = args_str
            else:
                args = {}
            tc_id = tc.get("id") or _short_tool_id()
            result.append(ToolCallRequest(id=tc_id, name=name, arguments=args))
        return result

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse a non-streaming API response JSON into LLMResponse."""
        choices = data.get("choices", [])
        if not choices:
            return LLMResponse(content="Empty response from API", finish_reason="error")

        choice = choices[0]
        msg = choice.get("message", {})
        content = msg.get("content")
        finish_reason = choice.get("finish_reason", "stop")
        tool_calls = self._parse_tool_calls(msg.get("tool_calls"))

        usage_raw = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
            "total_tokens": usage_raw.get("total_tokens", 0),
        }

        # Extract cost (OpenRouter puts it in top-level or usage fields)
        cost = None
        if "cost" in data and data["cost"] is not None:
            cost = float(data["cost"])
        if cost is None and "cost" in usage_raw:
            cost = float(usage_raw["cost"])

        # Extract cache tokens: Anthropic native uses cache_read_input_tokens,
        # OpenAI-compat uses prompt_tokens_details.cached_tokens
        cache_tokens = 0
        cache_tokens = int(usage_raw.get("cache_read_input_tokens", 0) or 0)
        if not cache_tokens:
            ptd = usage_raw.get("prompt_tokens_details") or {}
            if isinstance(ptd, dict):
                cache_tokens = int(ptd.get("cached_tokens", 0) or 0)

        # Extract provider metadata (e.g. OpenRouter generation details)
        provider_meta = []
        if "provider_specific_fields" in data:
            provider_meta.append(data["provider_specific_fields"])

        reasoning = msg.get("reasoning_content")
        thinking = msg.get("thinking_blocks")

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            cost=cost,
            cache_tokens=cache_tokens,
            provider_meta=provider_meta if provider_meta else None,
            reasoning_content=reasoning,
            thinking_blocks=thinking,
        )

    # ── Logging ──

    @staticmethod
    def _log_request(
        *,
        model: str, api_base: str, max_tokens: int, stream: bool,
        params: dict, tools_count: int, messages: list[dict],
        metadata: dict | None,
        response_data: dict | None = None,
        error: Exception | None = None,
        latency: float = 0.0,
        cache_headers: dict | None = None,
    ) -> None:
        """Log every LLM request to ~/.nanobot/request_logs/YYYY-MM-DD.jsonl."""
        try:
            log_dir = Path.home() / ".nanobot" / "request_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{_time.strftime('%Y-%m-%d')}.jsonl"

            meta = metadata or {}
            record: dict[str, Any] = {
                "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "agent": meta.get("log_agent"),
                "session": meta.get("log_session"),
                "topic": meta.get("log_topic"),
                "mode": meta.get("log_mode"),
                "model": model,
                "api_base": api_base,
                "max_tokens": max_tokens,
                "stream": stream,
                "params": {k: v for k, v in params.items() if v is not None},
                "tools_count": tools_count,
            }

            # Messages
            msgs_log: list[dict[str, Any]] = []
            total_chars = 0
            for msg in messages:
                c = msg.get("content")
                if isinstance(c, str):
                    c_len = len(c)
                elif isinstance(c, list):
                    c_len = sum(len(b.get("text", "")) for b in c if isinstance(b, dict))
                else:
                    c_len = 0
                total_chars += c_len
                entry: dict[str, Any] = {"role": msg.get("role"), "content": c, "content_len": c_len}
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
            if error:
                record["status"] = "error"
                record["error"] = str(error)
                record["error_type"] = type(error).__name__
                record["status_code"] = getattr(error, "status_code", None)
            elif response_data:
                record["status"] = "ok"
                choices = response_data.get("choices", [])
                if choices:
                    ch = choices[0]
                    msg_data = ch.get("message", {})
                    record["reply_len"] = len(msg_data.get("content", "") or "")
                    record["reply_preview"] = (msg_data.get("content") or "")[:500]
                    record["finish_reason"] = ch.get("finish_reason")
                    record["has_tool_calls"] = bool(msg_data.get("tool_calls"))
                    if msg_data.get("tool_calls"):
                        record["reply_tool_calls"] = [
                            {"name": tc.get("function", {}).get("name"), "args_len": len(tc.get("function", {}).get("arguments", ""))}
                            for tc in msg_data["tool_calls"]
                        ]
                usage = response_data.get("usage", {})
                if usage:
                    record["usage"] = {
                        "prompt": usage.get("prompt_tokens", 0),
                        "completion": usage.get("completion_tokens", 0),
                        "total": usage.get("total_tokens", 0),
                    }
            else:
                record["status"] = "unknown"

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as log_err:
            logger.debug("Request logging failed: {}", log_err)

    # ── Core API methods ──

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        metadata: dict[str, Any] | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request via httpx (raw JSON)."""
        original_model = model or self.default_model

        # Resolve provider
        resolved = self._resolve_provider(original_model)
        prov_name = resolved.get("provider_name")
        raw_model = resolved.get("model") or original_model
        target_base = resolved.get("api_base") or api_base or self.api_base or ""
        target_key = resolved.get("api_key") or api_key or self.api_key or ""

        # Build request
        body = self._build_body(
            messages=messages, model=raw_model, provider_name=prov_name,
            tools=tools, max_tokens=max_tokens, stream=False,
        )

        url = f"{target_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {target_key}",
            "Content-Type": "application/json",
            **(self.extra_headers or {}),
        }

        t0 = _time.time()
        try:
            client = self._get_client()
            r = await client.post(url, json=body, headers=headers)
            latency = _time.time() - t0

            if r.status_code != 200:
                error_text = r.text[:300]
                error_exc = _APIError(r.status_code, error_text)
                self._log_request(
                    model=raw_model, api_base=target_base, max_tokens=max_tokens,
                    stream=False, params=self.sampling_params, tools_count=len(tools or []),
                    messages=body["messages"], metadata=metadata, error=error_exc, latency=latency,
                    cache_headers=getattr(self, "_last_cache_headers", None),
                )

                # Auto-detect compatibility issues
                has_tool_msgs = any(m.get("role") == "tool" for m in messages)
                needs_flatten, needs_param = self._detect_compat_issues(
                    provider_key=prov_name,
                    status_code=r.status_code,
                    error_text=error_text,
                    has_tool_msgs=has_tool_msgs,
                    kwargs=self.sampling_params,
                )

                if needs_flatten or needs_param:
                    body = self._build_body(messages=messages, model=raw_model, provider_name=prov_name, tools=tools, max_tokens=max_tokens, stream=False)
                    r2 = await client.post(url, json=body, headers=headers)
                    if r2.status_code == 200:
                        data = r2.json()
                        self._log_request(model=raw_model, api_base=target_base, max_tokens=max_tokens, stream=False, params=self.sampling_params, tools_count=len(tools or []), messages=body["messages"], metadata=metadata, response_data=data, latency=_time.time() - t0)
                        return self._parse_response(data)

                return LLMResponse(
                    content=f"Error calling LLM: HTTP {r.status_code} - {error_text}",
                    finish_reason="error",
                    status_code=r.status_code,
                )

            data = r.json()
            self._log_request(
                model=raw_model, api_base=target_base, max_tokens=max_tokens,
                stream=False, params=self.sampling_params, tools_count=len(tools or []),
                messages=body["messages"], metadata=metadata, response_data=data, latency=latency,
                cache_headers=getattr(self, "_last_cache_headers", None),
            )
            return self._parse_response(data)

        except Exception as e:
            latency = _time.time() - t0
            self._log_request(
                model=raw_model, api_base=target_base, max_tokens=max_tokens,
                stream=False, params=self.sampling_params, tools_count=len(tools or []),
                messages=body.get("messages", messages), metadata=metadata,
                error=e, latency=latency,
                cache_headers=getattr(self, "_last_cache_headers", None),
            )
            return LLMResponse(
                content=f"Error calling LLM: {e}",
                finish_reason="error",
            )

    # ── OpenAI SDK client cache for streaming ──

    _openai_clients: dict[str, Any] = {}  # keyed by base_url:key_prefix

    def _get_openai_client(self, base_url: str, api_key: str):
        """Get or create an AsyncOpenAI client for streaming."""
        from openai import AsyncOpenAI
        cache_key = f"{base_url}:{api_key[:8]}"
        client = self._openai_clients.get(cache_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=120.0)
            self._openai_clients[cache_key] = client
        return client

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
        """Stream a chat completion using the OpenAI SDK for fast SSE.

        Uses ``openai.AsyncOpenAI`` for streaming (2-3x faster than httpx).
        Yields ``str`` content deltas, then a final ``LLMResponse``.
        """
        original_model = model or self.default_model

        resolved = self._resolve_provider(original_model)
        prov_name = resolved.get("provider_name")
        raw_model = resolved.get("model") or original_model
        target_base = resolved.get("api_base") or api_base or self.api_base or ""
        target_key = resolved.get("api_key") or api_key or self.api_key or ""

        # Build body (reuse _build_body for compat logic), then split for SDK
        body = self._build_body(
            messages=messages, model=raw_model, provider_name=prov_name,
            tools=tools, max_tokens=max_tokens, stream=True,
        )
        # Remove keys the SDK handles itself
        body.pop("stream", None)
        body.pop("stream_options", None)

        # Separate SDK-native params from non-standard ones
        _SDK_PARAMS = {"model", "messages", "max_tokens", "temperature", "top_p",
                       "frequency_penalty", "presence_penalty", "tools", "tool_choice",
                       "n", "stop", "logit_bias", "logprobs", "top_logprobs", "seed"}
        sdk_body = {k: v for k, v in body.items() if k in _SDK_PARAMS}
        extra_body = {k: v for k, v in body.items() if k not in _SDK_PARAMS}

        t0 = _time.time()
        try:
            oai_client = self._get_openai_client(target_base, target_key)
            stream = await oai_client.chat.completions.create(
                **sdk_body, stream=True, stream_options={"include_usage": True},
                extra_headers=self.extra_headers or None,
                extra_body=extra_body or None,
            )

            full_content = ""
            tool_calls_raw: list[dict] = []
            finish_reason = "stop"
            usage: dict[str, int] = {}
            has_tool_calls = False

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = choice.delta
                if delta and delta.content:
                    full_content += delta.content
                    if not has_tool_calls:
                        yield delta.content

                if delta and delta.tool_calls:
                    has_tool_calls = True
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index or 0
                        while len(tool_calls_raw) <= idx:
                            tool_calls_raw.append({"id": "", "name": "", "arguments": ""})
                        entry = tool_calls_raw[idx]
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry["arguments"] += tc_delta.function.arguments

        except Exception as e:
            self._log_request(
                model=raw_model, api_base=target_base, max_tokens=max_tokens,
                stream=True, params=self.sampling_params, tools_count=len(tools or []),
                messages=body.get("messages", messages), metadata=metadata,
                error=e, latency=_time.time() - t0,
            )
            yield LLMResponse(content=f"Error during streaming: {e}", finish_reason="error")
            return

        # Build tool calls
        parsed_tool_calls = []
        _tool_by_args: dict[frozenset, str] = {}
        if tools:
            for tdef in tools:
                fn = tdef.get("function", {})
                params = fn.get("parameters", {}).get("properties", {})
                if params and fn.get("name"):
                    _tool_by_args[frozenset(params.keys())] = fn["name"]

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
                arg_keys = frozenset(args.keys())
                inferred = _tool_by_args.get(arg_keys)
                if inferred:
                    name = inferred
                else:
                    for param_keys, tool_name in _tool_by_args.items():
                        if arg_keys <= param_keys:
                            name = tool_name
                            break
            if not name:
                name = "_unknown_"
            tc_id = tc["id"] or _short_tool_id()
            parsed_tool_calls.append(ToolCallRequest(id=tc_id, name=name, arguments=args))

        latency = _time.time() - t0

        # Log the completed stream
        self._log_request(
            model=raw_model, api_base=target_base, max_tokens=max_tokens,
            stream=True, params=self.sampling_params, tools_count=len(tools or []),
            messages=body["messages"], metadata=metadata,
            response_data={
                "choices": [{"message": {"content": full_content, "tool_calls": tool_calls_raw or None}, "finish_reason": finish_reason}],
                "usage": usage,
            },
            latency=latency,
        )

        # Extract cost from OpenRouter headers (available via httpx response)
        # The OpenAI SDK doesn't expose response headers in streaming,
        # so cost will be None for httpx_provider streaming.
        # Cost is available via litellm_provider which reads _hidden_params.
        yield LLMResponse(
            content=full_content or None,
            tool_calls=parsed_tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

class _APIError(Exception):
    """HTTP error from API call."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message[:200]}")

