"""Telegram provider/model callbacks."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.history.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
    COMPONENT_PHASES as _COMPONENT_PHASES,
)
from ..formatting import TELEGRAM_MAX_MESSAGE_LEN, to_cli_style


class ProvidersCallbackMixin:
    async def _dispatch_providers(self, query, data: str, chat_id: str) -> bool:
        if data == "pm_cancel":
            self._edit_state.pop(chat_id, None)
            await query.edit_message_text("❌ 已取消")

            return True

        if data == "st_prov":
            await self._speedtest_providers(query.message)

            return True

        if data == "st_agent":
            await self._speedtest_agents(query.message)

            return True

        if data.startswith("pm_newm:"):
            # User picked a provider for /newmodel
            prov = data[8:]
            self._edit_state[chat_id] = {"field": "pm_model_id", "mode": "pm", "provider": prov}
            await query.edit_message_text(
                f"🏢 提供商: {prov}\n\n"
                "请输入模型ID (如 google/gemini-3-flash-preview):"
            )

            return True

        if data.startswith("pm_delp:"):
            prov = data[8:]
            pm = self._load_pm()
            model_count = len(pm.get("models", {}).get(prov, []))
            await query.edit_message_text(
                f"⚠️ 确认删除提供商 **{prov}**？\n\n"
                f"这将同时删除该提供商下的 **{model_count}** 个模型。\n"
                f"此操作不可撤销！",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 确认删除", callback_data=f"pm_delp_yes:{prov}")],
                    [InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")],
                ])
            )

            return True

        if data.startswith("pm_delp_yes:"):
            prov = data[12:]
            pm = self._load_pm()
            pm.get("providers", {}).pop(prov, None)
            pm.get("models", {}).pop(prov, None)
            self._save_pm(pm)
            await query.edit_message_text(f"✅ 提供商 {prov} 及其所有模型已删除")

            return True

        if data.startswith("pm_delm_p:"):
            prov = data[10:]
            pm = self._load_pm()
            models = pm.get("models", {}).get(prov, [])
            if not models:
                await query.edit_message_text("⚠️ 该提供商没有模型")
                return
            buttons = [[InlineKeyboardButton(f"🗑 {m}", callback_data=f"pm_delm:{prov}:{m}")] for m in models]
            buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
            await query.edit_message_text(f"🗑 删除 {prov} 的模型:", reply_markup=InlineKeyboardMarkup(buttons))

            return True

        if data.startswith("pm_delm:"):
            parts = data.split(":", 2)
            prov, model = parts[1], parts[2]
            pm = self._load_pm()
            if prov in pm.get("models", {}):
                pm["models"][prov] = [m for m in pm["models"][prov] if m != model]
            self._save_pm(pm)
            try:
                await query.answer(f"🗑 已删除 {model}", show_alert=False)
            except Exception:
                pass
            # Refresh model list
            remaining = pm.get("models", {}).get(prov, [])
            if remaining:
                buttons = [[InlineKeyboardButton(f"🗑 {m}", callback_data=f"pm_delm:{prov}:{m}")] for m in remaining]
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="pm_cancel")])
                await query.edit_message_text(f"🗑 删除 {prov} 的模型 ({len(remaining)}):", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await query.edit_message_text(f"✅ {prov} 的模型已全部删除")

        # ── Edit agent model: 2-step provider → model selection ──
            return True

        if data.startswith("em_prov:"):
            parts = data.split(":", 2)
            agent_name, prov = parts[1], parts[2]
            pm = self._load_pm()
            models = pm.get("models", {}).get(prov, [])
            if not models:
                self._edit_state[chat_id] = {"agent": agent_name, "field": "model", "provider": prov}
                await query.edit_message_text(
                    f"🏢 {prov} 暂无已注册模型\n\n"
                    "请直接输入模型ID:"
                )
                return
            # Cache models for index-based lookup (avoids 64-byte callback limit)
            if not hasattr(self, "_em_model_cache"):
                self._em_model_cache = {}
            self._em_model_cache[f"{agent_name}:{prov}"] = models
            buttons = []
            for i, m in enumerate(models):
                buttons.append([InlineKeyboardButton(f"🤖 {m}", callback_data=f"em_mi:{agent_name}:{prov}:{i}")])
            buttons.append([InlineKeyboardButton("✏️ 手动输入", callback_data=f"em_manual:{agent_name}")])
            await query.edit_message_text(f"🏢 {prov} — 选择模型:", reply_markup=InlineKeyboardMarkup(buttons))

            return True

        if data.startswith("em_mi:") or data.startswith("em_model:"):
            # em_mi:agent:prov:index — resolve model from index cache
            if data.startswith("em_mi:"):
                parts = data.split(":")
                agent_name, prov, idx = parts[1], parts[2], int(parts[3])
                cache = getattr(self, "_em_model_cache", {})
                models = cache.get(f"{agent_name}:{prov}", [])
                if idx >= len(models):
                    await query.edit_message_text("⚠️ 模型索引无效，请重新操作")
                    return
                model = models[idx]
            else:
                parts = data.split(":", 3)
                agent_name, prov, model = parts[1], parts[2], parts[3]
            if self._groupchat_engine and agent_name in self._groupchat_engine.registry:
                self._groupchat_engine.registry[agent_name]["model"] = model
                # Update config on disk
                from pathlib import Path as _P
                agent_entry = self._groupchat_engine.registry[agent_name]
                if agent_entry.get("_default"):
                    # Default agent (Nanobot): update config.json
                    main_cfg_path = _P.home() / ".nanobot" / "config.json"
                    if main_cfg_path.exists():
                        cfg = json.loads(main_cfg_path.read_text())
                        cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
                        main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                else:
                    cfg_path = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
                    if cfg_path.exists():
                        cfg = json.loads(cfg_path.read_text())
                        cfg["model"] = model
                        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                await query.edit_message_text(f"✅ {agent_name} 模型已更新:\n🏢 {prov} / 🤖 {model}")
            else:
                await query.edit_message_text(f"❌ Agent '{agent_name}' 不存在")
            self._edit_state.pop(chat_id, None)

            return True

        if data.startswith("em_manual:"):
            agent_name = data[10:]
            self._edit_state[chat_id] = {"agent": agent_name, "field": "model"}
            await query.edit_message_text("请输入新模型名 (如 anthropic/claude-sonnet-4-5):")

        # ── Edit provider callbacks ──
            return True

        if data.startswith("ep_pick:"):
            prov = data[8:]
            pm = self._load_pm()
            info = pm.get("providers", {}).get(prov, {})
            url = info.get("url", "?")
            key_preview = info.get("apiKey", "")[:8] + "..." if info.get("apiKey") else "(none)"
            retry = info.get("retryDelays", [1, 2, 4])
            retry_str = f"{len(retry)}次 ({','.join(str(d) for d in retry)}s)"
            buttons = [
                [InlineKeyboardButton("🔗 修改 URL", callback_data=f"ep_field:{prov}:url")],
                [InlineKeyboardButton("🔑 修改 API Key", callback_data=f"ep_field:{prov}:key")],
                [InlineKeyboardButton(f"🔄 重试策略: {retry_str}", callback_data=f"ep_retry:{prov}")],
                [InlineKeyboardButton("📋 拉取模型列表", callback_data=f"ep_models:{prov}")],
                [InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")],
            ]
            await query.edit_message_text(
                f"✏️ 编辑提供商: {prov}\n\n"
                f"🔗 URL: {url}\n"
                f"🔑 Key: {key_preview}\n"
                f"🔄 重试: {retry_str}",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

            return True

        if data.startswith("ep_field:"):
            parts = data.split(":", 2)
            prov, fld = parts[1], parts[2]
            self._edit_state[chat_id] = {"field": f"ep_{fld}", "mode": "pm", "prov_name": prov}
            prompts = {"url": "请输入新的 API Base URL:", "key": "请输入新的 API Key:"}
            await query.edit_message_text(f"✏️ {prov} — {prompts.get(fld, fld)}")

            return True

        if data.startswith("ep_retry:"):
            prov = data[9:]
            # Show retry presets
            presets = {
                "std": ("标准 3次", [1, 2, 4]),
                "strong": ("加强 5次", [1, 2, 4, 8, 16]),
                "max": ("极限 7次", [1, 2, 4, 8, 16, 32, 60]),
            }
            pm = self._load_pm()
            current = pm.get("providers", {}).get(prov, {}).get("retryDelays", [1, 2, 4])
            current_str = f"{len(current)}次 ({','.join(str(d) for d in current)}s)"
            buttons = []
            for key, (label, delays) in presets.items():
                mark = " ✓" if delays == current else ""
                buttons.append([InlineKeyboardButton(
                    f"🔄 {label} ({','.join(str(d) for d in delays)}s){mark}",
                    callback_data=f"ep_retry_set:{prov}:{key}",
                )])
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")])
            await query.edit_message_text(
                f"🔄 {prov} 重试策略\n\n"
                f"当前: {current_str}\n\n"
                f"选择预设:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

            return True

        if data.startswith("ep_retry_set:"):
            parts = data.split(":", 2)
            prov, preset = parts[1], parts[2]
            presets = {
                "std": [1, 2, 4],
                "strong": [1, 2, 4, 8, 16],
                "max": [1, 2, 4, 8, 16, 32, 60],
            }
            delays = presets.get(preset, [1, 2, 4])
            pm = self._load_pm()
            if prov in pm.get("providers", {}):
                pm["providers"][prov]["retryDelays"] = delays
                self._save_pm(pm)
            # Apply to live provider
            engine = self._groupchat_engine
            if engine and hasattr(engine, "provider"):
                engine.provider._retry_delays = tuple(delays)
            await query.edit_message_text(
                f"✅ {prov} 重试策略已更新!\n"
                f"🔄 {len(delays)}次重试 ({','.join(str(d) for d in delays)}s)\n\n"
                f"总等待时间: {sum(delays)}s"
            )

            return True

        if data.startswith("ep_models:"):
            prov = data[10:]
            pm = self._load_pm()

            # Use cache if available (for back navigation)
            cache = getattr(self, "_model_cache", {})
            if prov in cache:
                model_ids = cache[prov]
            else:
                info = pm.get("providers", {}).get(prov, {})
                url = info.get("url", "").rstrip("/")
                api_key = info.get("apiKey", "")
                if not url or not api_key:
                    await query.edit_message_text(f"⚠️ {prov} 缺少 URL 或 API Key")
                    return
                # Fetch /v1/models
                import aiohttp
                import json as _json
                if "openrouter" in url.lower():
                    models_url = "https://openrouter.ai/api/v1/models"
                elif "/v1" in url:
                    models_url = f"{url}/models"
                else:
                    models_url = f"{url}/v1/models"
                try:
                    async with aiohttp.ClientSession() as session:
                        headers = {"Authorization": f"Bearer {api_key}"}
                        async with session.get(models_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            body = await resp.text()
                            if resp.status != 200:
                                await query.edit_message_text(f"❌ 拉取失败 (HTTP {resp.status})\n{body[:200]}")
                                return
                            try:
                                result = _json.loads(body)
                            except Exception:
                                await query.edit_message_text(f"❌ 解析失败\n{body[:200]}")
                                return
                except Exception as e:
                    await query.edit_message_text(f"❌ 请求异常: {str(e)[:100]}")
                    return

                model_list = result.get("data", []) if isinstance(result, dict) else []
                if not model_list:
                    await query.edit_message_text(f"⚠️ {prov} 无可用模型")
                    return
                model_ids = sorted(set(m.get("id", "") for m in model_list if m.get("id")))
                if not hasattr(self, "_model_cache"):
                    self._model_cache = {}
                self._model_cache[prov] = model_ids

            # Show prefix filters
            prefixes: dict[str, int] = {}
            for mid in model_ids:
                pfx = mid.split("/")[0] if "/" in mid else "other"
                prefixes[pfx] = prefixes.get(pfx, 0) + 1
            sorted_pfx = sorted(prefixes.items(), key=lambda x: -x[1])
            lines = [f"📋 {prov} 可用模型 ({len(model_ids)}):\n", "选择厂商前缀筛选:"]
            buttons = []
            for pfx, cnt in sorted_pfx[:20]:
                buttons.append([InlineKeyboardButton(f"📂 {pfx} ({cnt})", callback_data=f"ml_pfx:{prov}:{pfx}")])
            buttons.append([InlineKeyboardButton("🔍 搜索模型", callback_data=f"ml_srch:{prov}")])
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")])
            await query.edit_message_text(
                "\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons),
            )

            return True

        if data.startswith("ml_pfx:"):
            # ml_pfx:provider:prefix or ml_pfx:provider:prefix:page
            parts = data.split(":")
            if len(parts) < 3:
                return
            prov, prefix = parts[1], parts[2]
            page = int(parts[3]) if len(parts) > 3 else 0
            per_page = 15
            cache = getattr(self, "_model_cache", {})
            model_ids = cache.get(prov, [])
            filtered = [m for m in model_ids if m.startswith(f"{prefix}/") or (prefix == "other" and "/" not in m)]
            pm = self._load_pm()
            existing = set(pm.get("models", {}).get(prov, []))

            filtered = self._sort_models_newest_first(filtered)
            total_pages = max(1, (len(filtered) + per_page - 1) // per_page)
            page = min(page, total_pages - 1)
            start = page * per_page
            page_items = filtered[start:start + per_page]

            lines = [f"📋 {prov} / {prefix} ({len(filtered)}) [第{page+1}/{total_pages}页]:\n"]
            for mid in page_items:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
            buttons = self._build_model_buttons_2col(
                page_items, prov, existing, strip_prefix=prefix,
            )
            # Navigation
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"ml_pfx:{prov}:{prefix}:{page-1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"ml_pfx:{prov}:{prefix}:{page+1}"))
            if nav:
                buttons.append(nav)
            buttons.append([InlineKeyboardButton("⬅️ 返回厂商列表", callback_data=f"ep_models:{prov}")])
            await query.edit_message_text(
                "\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons),
            )

            return True

        if data.startswith("ml_srch:"):
            # ml_srch:provider — prompt user to type search keyword
            prov = data[8:]
            chat_id = str(query.message.chat_id)
            self._edit_state[chat_id] = {"action": "model_search", "provider": prov}
            await query.edit_message_text(f"🔍 搜索 {prov} 模型\n\n请输入关键词 (如 claude, llama, qwen):")

            return True

        if data.startswith("ep_addm:"):
            # ep_addm:provider:model_id — add model to provider
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            prov, model_id = parts[1], parts[2]
            pm = self._load_pm()
            if prov not in pm.get("providers", {}):
                await query.edit_message_text(f"❌ 提供商 {prov} 不存在")
                return
            models = pm.setdefault("models", {})
            prov_models = models.setdefault(prov, [])
            if model_id in prov_models:
                await query.edit_message_text(f"⚠️ {model_id} 已存在")
                return
            prov_models.append(model_id)
            self._save_pm(pm)
            # Reload in provider
            if self._groupchat_engine:
                self._groupchat_engine.provider._pm_overrides = None
            # Toast notification + refresh list locally
            try:
                await query.answer(f"✅ 已添加 {model_id}", show_alert=False)
            except Exception:
                pass
            # Rebuild model list from saved pm (no API re-fetch)
            existing = set(prov_models)
            all_models = getattr(self, "_model_cache", {}).get(prov, [])
            if not all_models:
                # No cache, just show confirmation
                await query.edit_message_text(
                    f"✅ 已添加 {model_id} 到 {prov}\n"
                    f"当前 {len(prov_models)} 个模型\n\n"
                    f"用 /editagent 切换 agent 模型",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 刷新列表", callback_data=f"ep_models:{prov}")],
                        [InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")],
                    ])
                )
                return
            # Rebuild from cache — stay in same prefix filter
            prefix = model_id.split("/")[0] if "/" in model_id else "other"
            filtered = [m for m in all_models if m.startswith(f"{prefix}/") or (prefix == "other" and "/" not in m)]
            filtered = self._sort_models_newest_first(filtered)
            lines = [f"📋 {prov} / {prefix} ({len(filtered)}):\n"]
            page_items = filtered[:30]
            for mid in page_items:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
            buttons = self._build_model_buttons_2col(page_items, prov, existing, strip_prefix=prefix)
            if len(filtered) > 30:
                lines.append(f"  ... 和 {len(filtered) - 30} 个更多")
            buttons.append([InlineKeyboardButton("⬅️ 返回厂商列表", callback_data=f"ep_models:{prov}")])
            await query.edit_message_text(
                "\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        # ── History settings callbacks ──
            return True

        return False
