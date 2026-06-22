---
name: static-landing-page
description: "Build a polished single-page static site (HTML/CSS/JS): branded landing page, product intro, cyberpunk/dystopian skin, responsive layout, local server + public tunnel. Use when the user wants a website, landing page, HTML page, or browsable URL — not a wallpaper gallery."
always: false
---

# Static Landing Page — 单页落地站

用户要**可浏览的单页网站**（品牌页、产品介绍、赛博朋克风落地页、公网链接）时使用。目标是**视觉完成度 + 可验证部署**，不是调研报告。

## 铁律（违反即失败）

1. **禁止 exec 写大段 HTML** — `content` 超过 ~500 字符必须用 `write_file(path, content)`。禁止 `cat >`、`heredoc`、`python3 -c` 拼整页 HTML（弱模型会把 CSS 片段拆成 JSON 数组导致 exec 崩溃）。
2. **先写设计 brief 再写代码** — 在 `write_file` 前用 5 行文字定：视觉风格、场景层、文案语气、区块清单、验收标准。
3. **事实与语气分离** — 公司/产品**事实**（成立年、模型名、融资额）必须准确；**视觉/文案语气**可按 brief 做赛博朋克/寡头 dystopia，但未来事件标 `[PROJECTION]`。
4. **每步验证** — `write_file` 后 `exec wc -c path`；部署后 `curl -I` 本地 200；tunnel 从日志取 URL。
5. **搜索预算 ≤1 轮** — 单页交付主要靠设计与写文件，不需要多轮 web_search。

## 设计 Brief 模板（Leader/执行者先填）

```
风格: 赛博朋克 / 企业 dystopia / 极简 / …
场景层: 城市剪影 + 网格地面 + 扫描线 + 警告条 + ticker（按需勾选）
语气: 视觉邪恶寡头感，内容事实准确
区块: sysbar → nav → hero → stats → about → models(独立卡片) → timeline → vision → footer
响应式: @media 720px + 480px
参考气质: "We don't sell intelligence. We lease reality."（可改写，勿照抄）
```

## 标准流程

### 0. 侦察
```bash
exec command="mkdir -p WORKDIR && ls -la WORKDIR && ls -la /root 2>/dev/null | head -20"
```
若已有成品 `index.html` → `read_file` 评估质量；可 `cp` 后 `edit_file` 迭代，不必从零。

### 1. 写 HTML（必须 write_file）
```text
write_file path="WORKDIR/index.html" content="<!DOCTYPE html>..."
```
要求：
- 单文件内联 CSS 即可（<18KB 合理）；复杂站可拆 `styles.css` 但仍用 `write_file`
- `<meta name="viewport" content="width=device-width, initial-scale=1.0">`
- Google Fonts: Orbitron + Rajdhani + Share Tech Mono（赛博朋克场景）

### 2. 视觉最低标准（对齐 v2 质量）

**Hero 必须有多层场景**（至少 2 层叠加）：
- 渐变背景层 `.hero-bg`
- 装饰层：城市剪影 `.cityscape` 或网格地面 `.grid-floor`
- 扫描线/CRT：`body::before` 重复线性渐变 + `body::after` 扫描动画

**氛围组件**（至少命中 3 项）：
- [ ] 顶部警告条 `.sysbar`（CLASSIFIED / clearance / live dot）
- [ ] Hero 内 badge + dystopian tagline
- [ ] 滚动 ticker `.ticker-wrap`
- [ ] Glitch 标题 `data-text` + `::before/::after`
- [ ] 霓虹色板：`--cyan` `--magenta` `--yellow` `--red`

**Models 区块必须独立** — 禁止复用 About 的 `.about-card`；用 `.model` + `.tier-opus/sonnet/haiku` + `.model-meta`（LATENCY/COST/ACCESS）。

**Timeline** — 未发生事件加 `.t-proj` 并显示 `[PROJECTION]`。

**响应式** — 至少：
```css
@media(max-width:720px){ ... }
@media(max-width:480px){ .nav-links{display:none} ... }
```

### 3. 本地服务 + tunnel
```bash
exec command="cd WORKDIR && nohup python3 -m http.server 8777 > server.log 2>&1 & sleep 1 && curl -s -o /dev/null -w '%{http_code}' http://localhost:8777/"
exec command="cd WORKDIR && nohup cloudflared tunnel --url http://localhost:8777 > tunnel.log 2>&1 & sleep 10 && grep -oE 'https://[a-zA-Z0-9.-]+\\.trycloudflare\\.com' tunnel.log | tail -1"
```

### 4. QA 门禁（全部通过才可结束）

- [ ] `index.html` ≥ 12KB 且含 `viewport` + `@media`
- [ ] `#models` 区有 ≥3 个**独立** tier 卡片（非 about-card 复用）
- [ ] Hero 有 ≥2 层场景装饰
- [ ] 本地 `curl -I` 返回 200
- [ ] `tunnel.log` 含 `https://*.trycloudflare.com` 或用户接受仅本地 URL
- [ ] 用 `read_file` 抽查：未来年份带 `[PROJECTION]` 或等效标记

任一不满足 → 继续 `write_file` / `edit_file`，不要 chatroom_send「已完成」。

### 5. 交付总结（中文）
```
## 交付物
- 目录 / 文件 / 大小

## 设计要点
- 场景层 / 语气 / 独立 models 区

## 访问
- 本地 http://localhost:PORT/
- 公网 https://....trycloudflare.com/...
```

## 与 web-deliverable 的分工

| 任务类型 | 用哪个 skill |
|---------|-------------|
| 单页品牌/产品/赛博朋克落地页 | **本 skill** |
| 壁纸画廊 + ZIP 批量下载 | `web-deliverable` |

两者都遵守：**大 HTML 只用 write_file**。