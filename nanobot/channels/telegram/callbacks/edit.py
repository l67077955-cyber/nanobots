"""Interactive edit-state input handlers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


_SAMPLING_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "top_k": (0, 200),
    "min_p": (0.0, 1.0),
    "top_a": (0.0, 1.0),
    "repetition_penalty": (1.0, 2.0),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}


def _validate_sampling_value(hp_key, value):
    """Return (ok, error_msg). value already parsed as float."""
    if hp_key not in _SAMPLING_RANGES:
        return True, None
    lo, hi = _SAMPLING_RANGES[hp_key]
    if value < lo or value > hi:
        return False, f"⚠️ {hp_key} 范围 [{lo}, {hi}]，当前 {value} 超出"
    return True, None


class EditCallbackMixin:
    """Mixin for multi-step agent/provider edit flows."""

    async def _handle_edit_input(self, chat_id: str, content: str) -> None:
        """Process interactive edit state input."""
        state = self._edit_state[chat_id]
        logger.debug("Edit input: chat_id={} field={} content={}...", chat_id, state.get("field", "?"), content[:50])

        # History setting value input
        if state.get("action") == "history_setting":
            del self._edit_state[chat_id]
            section = state["section"]
            key = state["key"]
            raw = content.strip()
            # Detect type: float keys (ratios), string keys (model), else int
            _float_keys = {
                "soft_ratio",
                "compress_ratio",
                "token_trigger_ratio",
                "context_budget_ratio",
                "cross_turn_repeat_ratio",
            }
            _string_keys = {"summarize_model"}
            if key in _string_keys:
                value: Any = raw
            elif key in _float_keys:
                try:
                    value = float(raw)
                except ValueError:
                    await self._gc_send(chat_id, f"❌ 请输入数字，收到: {raw}")
                    return
            else:
                try:
                    value = int(raw)
                except ValueError:
                    await self._gc_send(chat_id, f"❌ 请输入数字，收到: {raw}")
                    return
            from nanobot.groupchat.history import history_settings as hs
            result = hs.update_field(section, key, value)
            await self._gc_send(chat_id, result)
            return

        # Model search handler
        if state.get("action") == "model_search":
            del self._edit_state[chat_id]
            prov = state["provider"]
            keyword = content.strip().lower()
            cache = getattr(self, "_model_cache", {})
            model_ids = cache.get(prov, [])
            filtered = [m for m in model_ids if keyword in m.lower()]

            pm = self._load_pm()
            existing = set(pm.get("models", {}).get(prov, []))
            filtered = self._sort_models_newest_first(filtered)
            lines = [f"🔍 搜索 \"{content.strip()}\" ({len(filtered)} 结果):\n"]
            page_items = filtered[:25]
            for mid in page_items:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
            buttons = self._build_model_buttons_2col(page_items, prov, existing)
            if len(filtered) > 25:
                lines.append(f"  ... 和 {len(filtered) - 25} 个更多")
            if not filtered:
                lines.append("  无匹配结果")
            buttons.append([InlineKeyboardButton("🔍 重新搜索", callback_data=f"ml_srch:{prov}")])
            buttons.append([InlineKeyboardButton("⬅️ 返回厂商列表", callback_data=f"ep_models:{prov}")])
            await self._app.bot.send_message(
                chat_id=int(chat_id),
                text="\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Handle prompt_edit (from /prompt → edit component)
        if state.get("field") == "prompt_edit":
            del self._edit_state[chat_id]
            agent_name = state.get("agent", "")
            key = state.get("key", "")
            engine = self._groupchat_engine
            if engine:
                try:
                    result = engine.prompt_builder.update_prompt_component(
                        agent_name, key, content.strip(),
                        engine.registry, engine.workspace,
                        Path(engine.config.agents_dir or "~/.nanobot/agents").expanduser(),
                    )
                    # Send confirmation + refreshed component list
                    preview = (content.strip()[:200] + "…") if len(content.strip()) > 200 else content.strip()
                    await self._gc_send(chat_id, f"{result}\n\n📄 内容预览:\n{preview}")
                    text, markup = self._build_prompt_order_view(engine)
                    await self._app.bot.send_message(
                        chat_id=int(chat_id), text=text[:4096], reply_markup=markup,
                    )
                except Exception as e:
                    logger.error("prompt_edit failed: {} key={} err={}", agent_name, key, e)
                    await self._gc_send(chat_id, f"❌ 编辑失败: {e}")
            return

        # Handle custom prompt component name input
        if state.get("field") == "pradd_custom_name":
            del self._edit_state[chat_id]
            name_input = content.strip()
            if not name_input or name_input in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            # Sanitize: use the input as label, derive a key from it
            import re
            key = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff]', '_', name_input).strip('_').lower()
            if not key:
                key = f"custom_{hash(name_input) % 10000}"
            label = f"{name_input} ({key})"
            engine = self._groupchat_engine
            if engine:
                result = PromptBuilder.add_custom_component(key, label)
                if result.startswith("✅"):
                    # Also add to prompt order
                    order = engine.prompt_builder.get_agent_prompt_order()
                    if key not in order:
                        order.append(key)
                        engine.prompt_builder.set_default_prompt_order(order)
                await self._gc_send(chat_id, result)
                # Show refreshed component list with edit buttons
                text, markup = self._build_prompt_order_view(engine)
                await self._app.bot.send_message(
                    chat_id=int(chat_id), text=text[:4096], reply_markup=markup,
                )
            return

        field = state["field"]

        # Handle hyperparams value input
        if field == "hp_value":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            hp_key = state.get("hp_key", "")
            raw_val = content.strip()
            if hp_key in ("reasoning_effort", "stop"):
                value = None if raw_val.lower() in ("off", "none", "null") else raw_val
            else:
                try:
                    value = float(raw_val)
                except ValueError:
                    await self._gc_send(chat_id, "⚠️ 值必须是数字")
                    return
                _ok, _err = _validate_sampling_value(hp_key, value)
                if not _ok:
                    await self._gc_send(chat_id, _err)
                    return
            provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
            params = getattr(provider, 'sampling_params', None) if provider else None
            if params is not None:
                old_val = params.get(hp_key, None)
                params[hp_key] = value
                # Persist to disk
                hp_path = Path.home() / ".nanobot" / "hyperparams.json"
                try:
                    hp_path.write_text(json.dumps(params, indent=2))
                    logger.info("Persisted hyperparams (set {}={}) to {}", hp_key, value, hp_path)
                except Exception as e:
                    logger.error("Failed to persist hyperparams: {}", e)
                    await self._gc_send(chat_id, f"⚠️ 参数已生效但持久化失败: {e}")
                if old_val is not None:
                    await self._gc_send(chat_id, f"✅ {hp_key}: {old_val} → {value}\n即时生效，已持久化")
                else:
                    await self._gc_send(chat_id, f"✅ 已添加 {hp_key} = {value}\n即时生效，已持久化")
                # Refresh keyboard
                await self._send_hyperparams_keyboard(chat_id, params)
            return

        # Handle custom hyperparam name input
        if field == "hp_add_custom":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            key = content.strip().lower().replace(" ", "_")
            self._edit_state[chat_id] = {"field": "hp_value", "hp_key": key, "hp_is_new": True}
            await self._gc_send(chat_id, f"➕ 添加 {key}\n\n请输入值 (数字):")
            return

        # Handle agent hyperparams value input
        if field == "ahp_value":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            hp_key = state.get("hp_key", "")
            a_name = state.get("agent", "")
            raw_val = content.strip()
            if hp_key in ("reasoning_effort", "stop"):
                value = None if raw_val.lower() in ("off", "none", "null") else raw_val
            else:
                try:
                    value = float(raw_val)
                except ValueError:
                    await self._gc_send(chat_id, "⚠️ 值必须是数字")
                    return
                _ok, _err = _validate_sampling_value(hp_key, value)
                if not _ok:
                    await self._gc_send(chat_id, _err)
                    return
            if self._groupchat_engine and a_name in self._groupchat_engine.registry:
                agent = self._groupchat_engine.registry[a_name]
                if "hyperparams" not in agent or not isinstance(agent["hyperparams"], dict):
                    agent["hyperparams"] = {}
                old_val = agent["hyperparams"].get(hp_key)
                agent["hyperparams"][hp_key] = value
                # Persist to config.json
                cfg_path = Path.home() / ".nanobot" / "agents" / a_name.lower() / "config.json"
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                        cfg.setdefault("hyperparams", {})
                        cfg["hyperparams"][hp_key] = value
                        cfg_path.write_text(json.dumps(cfg, indent=2))
                    except Exception as e:
                        logger.error("Failed to save agent hyperparams: {}", e)
                        await self._gc_send(chat_id, f"⚠️ 参数已生效但持久化失败: {e}")
                if old_val is not None:
                    await self._gc_send(chat_id, f"✅ {a_name} {hp_key}: {old_val} → {value}")
                else:
                    await self._gc_send(chat_id, f"✅ {a_name} 已添加 {hp_key} = {value}")
                await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent["hyperparams"])
            return

        # Handle agent custom hyperparam name input
        if field == "ahp_add_custom":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            a_name = state.get("agent", "")
            key = content.strip().lower().replace(" ", "_")
            self._edit_state[chat_id] = {"field": "ahp_value", "agent": a_name, "hp_key": key, "hp_is_new": True}
            await self._gc_send(chat_id, f"➕ 为 {a_name} 添加 {key}\n\n请输入值 (数字):")
            return

        # Handle groupchat settings value input
        if field == "gc_value":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            gc_key = state.get("gc_key", "")
            try:
                value = int(content.strip())
            except ValueError:
                await self._gc_send(chat_id, "⚠️ 值必须是整数")
                return
            if value < 1:
                await self._gc_send(chat_id, "⚠️ 值必须 ≥ 1")
                return
            settings = self._load_gc_settings()
            old_val = settings.get(gc_key, self.GC_SETTINGS_DEFAULTS.get(gc_key))
            settings[gc_key] = value
            self._save_gc_settings(settings)
            label = self.GC_SETTINGS_LABELS.get(gc_key, gc_key)
            await self._gc_send(chat_id, f"✅ {label}: {old_val} → {value}\n下次群聊生效，已持久化")
            return
        if field == "sg_name":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            result = self._groupchat_engine.save_group(content.strip())
            await self._gc_send(chat_id, result)
            return

        # Universal cancel — works at any edit prompt
        if content.strip() in ("0", "取消", "/cancel"):
            del self._edit_state[chat_id]
            await self._gc_send(chat_id, "❌ 已取消")
            return

        # Handle provider/model management flows
        if state.get("mode") == "pm":
            field = state["field"]
            if field == "pm_prov_name":
                name = content.strip().lower()
                state["prov_name"] = name
                state["field"] = "pm_prov_url"
                await self._gc_send(chat_id, f"提供商: {name}\n\n请输入 API Base URL\n(如 https://openrouter.ai/v1):")
                return
            elif field == "pm_prov_url":
                url = content.strip().rstrip("/")
                state["prov_url"] = url
                state["field"] = "pm_prov_key"
                await self._gc_send(chat_id, f"🔗 URL: {url}\n\n请输入 API Key:")
                return
            elif field == "pm_prov_key":
                api_key = content.strip()
                name = state["prov_name"]
                url = state["prov_url"]
                pm = self._load_pm()
                pm.setdefault("providers", {})[name] = {"url": url, "apiKey": api_key}
                pm.setdefault("models", {}).setdefault(name, [])
                self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ 提供商 {name} 已创建!\n🔗 {url}\n🔑 {api_key[:8]}...")
                return
            elif field == "pm_model_id":
                model_id = content.strip()
                prov = state["provider"]
                pm = self._load_pm()
                pm.setdefault("models", {}).setdefault(prov, [])
                if model_id not in pm["models"][prov]:
                    pm["models"][prov].append(model_id)
                self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ 模型已添加!\n🏢 {prov} / 🤖 {model_id}")
                return
            elif field == "ep_url":
                prov = state["prov_name"]
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["url"] = content.strip().rstrip("/")
                    self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ {prov} URL 已更新: {content.strip()}")
                return
            elif field == "ep_key":
                prov = state["prov_name"]
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["apiKey"] = content.strip()
                    self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ {prov} API Key 已更新: {content.strip()[:8]}...")
                return

        agent_name = state.get("agent", "")
        engine = self._groupchat_engine
        if not engine:
            del self._edit_state[chat_id]
            return



        # Handle /newagent create flow
        if state.get("mode") == "create":
            if field == "create_name":
                name = content.strip()
                if name in ("0", "取消"):
                    del self._edit_state[chat_id]
                    await self._gc_send(chat_id, "❌ 已取消")
                    return
                if engine._resolve_agent_name(name):
                    await self._gc_send(chat_id, f"⚠️ '{name}' 已存在，请换个名字:")
                    return
                state["agent"] = name
                state["field"] = "create_model"
                await self._gc_send(chat_id,
                    f"Agent: {name}\n\n请输入模型名:\n"
                    "(如 anthropic/claude-sonnet-4-5, x-ai/grok-4.1-fast)"
                )
                return
            if field == "create_model":
                model_name = content.strip()
                if model_name in ("0", "取消"):
                    del self._edit_state[chat_id]
                    await self._gc_send(chat_id, "❌ 已取消")
                    return
                await self._gc_send(chat_id, f"🔍 测试模型 {model_name}...")
                try:
                    response = await engine.provider.chat(
                        messages=[{"role": "user", "content": "Say 'hello' in one word."}],
                        model=model_name,
                        max_tokens=20,
                    )
                    reply = (response.content or "").strip()
                    state["model"] = model_name
                    state["field"] = "create_persona"
                    await self._gc_send(chat_id,
                        f"✅ 模型 {model_name} 可用!\n"
                        f"测试回复: {reply}\n\n"
                        f"请输入人设 (SOUL.md 内容):"
                    )
                except Exception as e:
                    await self._gc_send(chat_id,
                        f"❌ 模型 {model_name} 不可用: {e}\n\n"
                        f"请重新输入模型名，或发 0 取消:"
                    )
                return
            elif field == "create_persona":
                name = agent_name
                model = state["model"]
                prompt = content
                global_hp = getattr(engine.provider, 'sampling_params', None)
                agent_hp = dict(global_hp) if global_hp else {}
                engine.registry[name] = {"model": model, "prompt": prompt, "hyperparams": agent_hp}
                # Save to disk
                from pathlib import Path as _P
                soul_dir = _P.home() / ".nanobot" / "agents" / name.lower() / "workspace"
                soul_dir.mkdir(parents=True, exist_ok=True)
                (soul_dir / "SOUL.md").write_text(prompt)
                config_path = soul_dir.parent / "config.json"
                config_data = {"model": model, "rank": "basic"}
                if agent_hp:
                    config_data["hyperparams"] = agent_hp
                config_path.write_text(json.dumps(config_data, indent=2))
                preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
                await self._gc_send(chat_id, f"✅ Agent {name} 已创建!\n模型: {model}\n人设: {preview}")
                del self._edit_state[chat_id]
                return

        if field is None:
            c = content.strip()
            if c in ("0", "取消"):
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, "❌ 已取消")
                return
            field_map = {"1": "name", "2": "persona", "3": "model"}
            if c in field_map:
                state["field"] = field_map[c]
                prompts = {"name": "新名字", "persona": "新人设内容", "model": "新模型名 (如 anthropic/claude-sonnet-4-5)"}
                await self._gc_send(chat_id, f"请输入{prompts[field_map[c]]}:")
            else:
                await self._gc_send(chat_id, "请输入 1/2/3 或 0 取消")
            return

        if field == "name":
            new_name = content.strip()
            if new_name and new_name != agent_name:
                data = engine.registry.pop(agent_name)
                engine.registry[new_name] = data
                if agent_name in engine._active_agents:
                    idx = engine._active_agents.index(agent_name)
                    engine._active_agents[idx] = new_name
                # Update leader if needed
                if engine._leader == agent_name:
                    engine._leader = new_name
                    engine._state.save_leader(new_name)
                # Update saved groups
                groups = engine._state.load_groups()
                changed = False
                for gname, members in groups.items():
                    if agent_name in members:
                        groups[gname] = [new_name if m == agent_name else m for m in members]
                        changed = True
                if changed:
                    engine._state.save_groups(groups)
                # Rename directory
                from pathlib import Path as _P
                agents_dir = _P.home() / ".nanobot" / "agents"
                old_dir = agents_dir / agent_name.lower()
                new_dir = agents_dir / new_name.lower()
                if old_dir.exists() and not new_dir.exists():
                    old_dir.rename(new_dir)
                engine._state.save_active(engine._active_agents)
                await self._gc_send(chat_id, f"✅ {agent_name} → {new_name}")
            else:
                await self._gc_send(chat_id, "⚠️ 名字未变")
        elif field == "persona":
            engine.registry[agent_name]["prompt"] = content
            from pathlib import Path as _P
            soul_dir = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "workspace"
            soul_dir.mkdir(parents=True, exist_ok=True)
            (soul_dir / "SOUL.md").write_text(content)
            preview = content[:80] + "..." if len(content) > 80 else content
            await self._gc_send(chat_id, f"✅ {agent_name} 人设已更新:\n{preview}")
        elif field == "model":
            new_model = content.strip()
            engine.registry[agent_name]["model"] = new_model
            # Persist to disk
            from pathlib import Path as _P
            agent_entry = engine.registry[agent_name]
            if agent_entry.get("_default"):
                main_cfg_path = _P.home() / ".nanobot" / "config.json"
                if main_cfg_path.exists():
                    cfg = json.loads(main_cfg_path.read_text())
                    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = new_model
                    main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            else:
                cfg_path = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    cfg["model"] = new_model
                    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            await self._gc_send(chat_id, f"✅ {agent_name} 模型: {new_model}")

        del self._edit_state[chat_id]

