"""Provider and model management commands for Telegram."""

from __future__ import annotations

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from loguru import logger


class ProviderCommandsMixin:
    """Mixin providing provider/model management commands."""

    def _pm_path(self) -> Path:
        from nanobot.state import settings_store
        return settings_store.provmodels_path()

    def _load_pm(self) -> dict:
        """Load providers_models.json via the single settings store.

        Model arrays can accumulate hand-edited separator/comment lines
        (``═══ xxx ═══``). settings_store.load_pm sanitizes every model list
        with :func:`sanitize_model_list` and writes the clean version back —
        so the file heals itself on next use, through ONE code path.
        """
        from nanobot.state import settings_store
        return settings_store.load_pm()

    def _save_pm(self, data: dict) -> None:
        from nanobot.state import settings_store
        settings_store.save_pm(data)

    async def _on_newprovider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start flow: name → URL → apiKey."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        chat_id = str(update.message.chat_id)
        self._begin_edit(chat_id, {"field": "pm_prov_name", "mode": "pm"})
        await update.message.reply_text("🆕 创建提供商\n\n请输入提供商名称 (如 openrouter, aihubmix):")

    async def _on_newmodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show provider keyboard, then ask for model ID."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = list(pm.get("providers", {}).keys())
        if not provs:
            await update.message.reply_text("⚠️ 还没有提供商，请先 /newprovider")
            return
        buttons = [[InlineKeyboardButton(f"🏢 {p}", callback_data=f"pm_newm:{p}")] for p in provs]
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text("🆕 添加模型\n\n选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_deleteprovider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = list(pm.get("providers", {}).keys())
        if not provs:
            await update.message.reply_text("⚠️ 没有提供商")
            return
        buttons = [[InlineKeyboardButton(f"🗑 {p}", callback_data=f"pm_delp:{p}")] for p in provs]
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text("🗑 删除提供商\n\n选择要删除的:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_deletemodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = [p for p in pm.get("providers", {}) if pm.get("models", {}).get(p)]
        if not provs:
            await update.message.reply_text("⚠️ 没有可删除的模型")
            return
        buttons = [[InlineKeyboardButton(f"🏢 {p} ({len(pm['models'].get(p, []))} models)", callback_data=f"pm_delm_p:{p}")] for p in provs]
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text("🗑 删除模型\n\n先选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _render_provider_edit_list(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """Build the provider-pick list (shared by /editprovider and its back).

        Returns (text, markup); markup is None when there are no providers.
        """
        pm = self._load_pm()
        provs = list(pm.get("providers", {}).keys())
        if not provs:
            return "⚠️ 没有提供商,请先 /newprovider", None
        buttons = []
        for p in provs:
            info = pm["providers"][p]
            url = info.get("url", "?")
            buttons.append([InlineKeyboardButton(f"✏️ {p} ({url})", callback_data=f"ep_pick:{p}")])
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        return "✏️ 编辑提供商\n\n选择要编辑的:", InlineKeyboardMarkup(buttons)

    async def _on_editprovider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Edit an existing provider's URL or API key."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        text, markup = await self._render_provider_edit_list()
        if markup is None:
            await update.message.reply_text(text)
            return
        await update.message.reply_text(text, reply_markup=markup)

    def _render_ep_panel(self, prov: str) -> tuple[str, InlineKeyboardMarkup]:
        """Build a single provider's edit panel (secondary). Pure: no sends.

        Shared by ep_pick (enter) and any 'return to this provider' handler,
        so back navigation re-renders the exact same panel.
        """
        pm = self._load_pm()
        info = pm.get("providers", {}).get(prov, {})
        url = info.get("url", "?")
        key_preview = info.get("apiKey", "")[:8] + "..." if info.get("apiKey") else "(none)"
        retry = info.get("retryDelays", [1, 2, 4])
        retry_str = f"{len(retry)}次 ({','.join(str(d) for d in retry)}s)"
        models = pm.get("models", {}).get(prov, [])
        lines = [f"✏️ 编辑提供商: {prov}\n\n", f"🔗 URL: {url}\n", f"🔑 Key: {key_preview}\n", f"🔄 重试: {retry_str}\n"]
        if models:
            lines.append(f"\n🤖 模型 ({len(models)}):")
            for m in models:
                lines.append(f"   {m}  [🗑 delete]")
        else:
            lines.append("\n🤖 模型: (无,用下方 添加/拉取)")
        buttons = [
            [InlineKeyboardButton("🔗 修改 URL", callback_data=f"ep_field:{prov}:url")],
            [InlineKeyboardButton("🔑 修改 API Key", callback_data=f"ep_field:{prov}:key")],
            [InlineKeyboardButton(f"🔄 重试策略: {retry_str}", callback_data=f"ep_retry:{prov}")],
            [InlineKeyboardButton("📋 拉取模型列表", callback_data=f"ep_models:{prov}")],
        ]
        child_buttons = [InlineKeyboardButton("➕ 添加模型", callback_data=f"m:add_model:{prov}")]
        if models:
            child_buttons.append(InlineKeyboardButton("🗑 删除模型", callback_data=f"pm_delm_p:{prov}"))
        buttons.append(child_buttons)
        buttons.append([InlineKeyboardButton("🗑 删除此提供商", callback_data=f"pm_delp:{prov}")])
        buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="ep_back")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    async def _on_providers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List all providers and their models."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = pm.get("providers", {})
        models = pm.get("models", {})
        if not provs:
            await update.message.reply_text("📭 暂无提供商\n\n用 /newprovider 添加")
            return
        lines = ["📋 **提供商 & 模型列表**\n"]
        for name, info in provs.items():
            url = info.get("url", "?")
            key = info.get("apiKey", "")
            key_preview = key[:8] + "..." if key else "(未设置)"
            ms = models.get(name, [])
            lines.append(f"🏢 **{name}**")
            lines.append(f"   🔗 {url}")
            lines.append(f"   🔑 {key_preview}")
            if ms:
                for m in ms:
                    lines.append(f"   🤖 {m}")
            else:
                lines.append("   (无模型，用 /newmodel 添加)")
            lines.append("")
        buttons = [
            [InlineKeyboardButton("➕ 新建提供商", callback_data="m:new_provider")],
            [InlineKeyboardButton("✏️ 编辑提供商", callback_data="ep_list")],
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_speedtest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Benchmark providers or active agents."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        engine = self._groupchat_engine
        active_count = len(engine.active_agents) if engine else 0
        buttons = [
            [InlineKeyboardButton("🏢 测试所有提供商", callback_data="st_prov")],
        ]
        if active_count > 0:
            buttons.insert(0, [InlineKeyboardButton(
                f"🤖 测试活跃Agent ({active_count}个)",
                callback_data="st_agent"
            )])
        await update.message.reply_text(
            "⚡ 测速模式选择:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def _speedtest_providers(self, msg) -> None:
        """Test all providers with a simple request."""
        import aiohttp
        import time as _time
        import json as _json

        pm = self._load_pm()
        provs = pm.get("providers", {})
        models = pm.get("models", {})
        if not provs:
            await msg.edit_text("⚠️ 没有提供商")
            return

        results = []
        status_lines = {name: f"⏳ {name} — 等待中" for name in provs}
        await msg.edit_text("⏳ 提供商测速中...\n\n" + "\n".join(status_lines.values()))

        for prov_name, info in provs.items():
            url = (info.get("url") or "").rstrip("/")
            api_key = info.get("apiKey", "")
            prov_models = models.get(prov_name, [])
            test_model = prov_models[0] if prov_models else None

            if not url or not api_key or not test_model:
                status_lines[prov_name] = f"⚠️ {prov_name} — 缺少配置"
                results.append({"name": prov_name, "error": "缺少配置"})
                continue

            status_lines[prov_name] = f"🔄 {prov_name} — 测试中..."
            try:
                await msg.edit_text("⏳ 提供商测速中...\n\n" + "\n".join(status_lines.values()))
            except Exception:
                pass

            if "openrouter" in url.lower():
                raw_model = test_model
            else:
                raw_model = test_model.split("/", 1)[-1] if "/" in test_model else test_model
            chat_url = f"{url}/chat/completions" if "/v1" in url else f"{url}/v1/chat/completions"

            payload = {
                "model": raw_model,
                "messages": [{"role": "user", "content": "say hi"}],
                "max_tokens": 50,
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            start = _time.monotonic()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(chat_url, json=payload, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        elapsed = round(_time.monotonic() - start, 2)
                        body = await resp.text()
                        if resp.status != 200:
                            err_msg = body[:80].replace("\n", " ").strip()
                            status_lines[prov_name] = f"❌ {prov_name} — HTTP {resp.status}"
                            results.append({"name": prov_name, "model": raw_model, "error": f"HTTP {resp.status}: {err_msg}", "time": elapsed})
                            continue
                        try:
                            data = _json.loads(body)
                        except Exception:
                            status_lines[prov_name] = f"❌ {prov_name} — 非JSON响应"
                            results.append({"name": prov_name, "model": raw_model, "error": "非JSON响应", "time": elapsed})
                            continue

                elapsed = round(_time.monotonic() - start, 2)
                usage = data.get("usage", {})
                comp_tok = usage.get("completion_tokens", 0)
                tok_per_s = round(comp_tok / elapsed, 1) if elapsed > 0 and comp_tok else 0
                reply = ""
                choices = data.get("choices", [])
                if choices:
                    reply = (choices[0].get("message", {}).get("content", "") or "")[:50]
                status_lines[prov_name] = f"✅ {prov_name} — {elapsed}s"
                results.append({"name": prov_name, "model": raw_model, "time": elapsed, "tok_s": tok_per_s, "reply": reply})
            except Exception as e:
                elapsed = round(_time.monotonic() - start, 2)
                status_lines[prov_name] = f"❌ {prov_name} — {str(e)[:30]}"
                results.append({"name": prov_name, "error": str(e)[:50], "time": elapsed})

        results.sort(key=lambda r: r.get("time", 999))
        lines = ["🏁 提供商测速结果 (按延迟排序):\n"]
        for i, r in enumerate(results):
            if r.get("error"):
                lines.append(f"❌ {r['name']} — {r.get('time', '?')}s — {r['error']}")
            else:
                medal = ["🥇", "🥈", "🥉"][i] if i < 3 else "  "
                lines.append(
                    f"{medal} {r['name']} — {r['time']}s\n"
                    f"     🤖 {r.get('model', '?')} | {r.get('tok_s', 0)} tok/s\n"
                    f"     💬 {r.get('reply', '')}"
                )
        await msg.edit_text("\n".join(lines)[:4096])

    async def _speedtest_agents(self, msg) -> None:
        """Test each active agent's model for connectivity."""
        import aiohttp
        import time as _time
        import json as _json

        engine = self._groupchat_engine
        if not engine or not engine.active_agents:
            await msg.edit_text("⚠️ 没有活跃 agent")
            return

        pm = self._load_pm()
        agents_to_test = [(name, engine.registry[name]) for name in engine.active_agents if name in engine.registry]
        status_lines = {name: f"⏳ {name} — 等待中" for name, _ in agents_to_test}
        await msg.edit_text("⏳ Agent 连接测试中...\n\n" + "\n".join(status_lines.values()))

        results = []
        for agent_name, info in agents_to_test:
            model = info.get("model", "")
            # Resolve provider for this model
            url, api_key, raw_model = None, None, model
            for pn, model_list in pm.get("models", {}).items():
                if model in model_list:
                    prov_info = pm.get("providers", {}).get(pn, {})
                    url = (prov_info.get("url") or "").rstrip("/")
                    api_key = prov_info.get("apiKey", "")
                    if "openrouter" in url.lower():
                        raw_model = model
                    else:
                        raw_model = model.split("/", 1)[-1] if "/" in model else model
                    break

            if not url or not api_key:
                status_lines[agent_name] = f"⚠️ {agent_name} — 无提供商"
                results.append({"name": agent_name, "model": model, "error": "无提供商配置"})
                continue

            status_lines[agent_name] = f"🔄 {agent_name} — 测试中..."
            try:
                await msg.edit_text("⏳ Agent 连接测试中...\n\n" + "\n".join(status_lines.values()))
            except Exception:
                pass

            chat_url = f"{url}/chat/completions" if "/v1" in url else f"{url}/v1/chat/completions"
            payload = {"model": raw_model, "messages": [{"role": "user", "content": "say hi"}], "max_tokens": 50}
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            start = _time.monotonic()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(chat_url, json=payload, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        elapsed = round(_time.monotonic() - start, 2)
                        body = await resp.text()
                        if resp.status != 200:
                            err_msg = body[:60].replace("\n", " ").strip()
                            status_lines[agent_name] = f"❌ {agent_name} — HTTP {resp.status}"
                            results.append({"name": agent_name, "model": raw_model, "error": f"HTTP {resp.status}: {err_msg}", "time": elapsed})
                            continue
                        try:
                            data = _json.loads(body)
                        except Exception:
                            status_lines[agent_name] = f"❌ {agent_name} — 非JSON响应"
                            results.append({"name": agent_name, "model": raw_model, "error": "非JSON响应", "time": elapsed})
                            continue

                elapsed = round(_time.monotonic() - start, 2)
                comp_tok = data.get("usage", {}).get("completion_tokens", 0)
                tok_per_s = round(comp_tok / elapsed, 1) if elapsed > 0 and comp_tok else 0
                reply = ""
                choices = data.get("choices", [])
                if choices:
                    reply = (choices[0].get("message", {}).get("content", "") or "")[:40]
                status_lines[agent_name] = f"✅ {agent_name} — {elapsed}s"
                results.append({"name": agent_name, "model": raw_model, "time": elapsed, "tok_s": tok_per_s, "reply": reply})
            except Exception as e:
                elapsed = round(_time.monotonic() - start, 2)
                status_lines[agent_name] = f"❌ {agent_name} — {str(e)[:30]}"
                results.append({"name": agent_name, "error": str(e)[:40], "time": elapsed})

        # Final results
        ok = [r for r in results if not r.get("error")]
        fail = [r for r in results if r.get("error")]
        lines = [f"🏁 Agent 连接测试 ({len(ok)}✅ {len(fail)}❌):\n"]
        for r in ok:
            lines.append(
                f"✅ {r['name']} — {r['time']}s\n"
                f"   🤖 {r.get('model','?')} | {r.get('tok_s',0)} tok/s\n"
                f"   💬 {r.get('reply','')}"
            )
        for r in fail:
            lines.append(f"❌ {r['name']} — {r.get('time','?')}s — {r.get('model','?')}\n   {r['error']}")
        await msg.edit_text("\n".join(lines)[:4096])

