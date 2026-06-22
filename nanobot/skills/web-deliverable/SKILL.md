---
name: web-deliverable
description: "Build and deploy a browsable web deliverable with downloads: static gallery/site, ZIP bundle, local server, and public tunnel. Use when the user wants a website, gallery, wallpaper collection, download page, or public URL."
always: false
---

# Web Deliverable — 网页交付流水线

当用户要「做网站 / 画廊 / 可下载 / 公网链接」时使用。目标是**可验证的成品**，不是搜索报告。

## 铁律

1. **先查现有资源** — `list_dir` / `exec ls` 看工作目录和相邻目录是否已有成品；有则 `cp -r` 复制后改路径，禁止从零重复搜图。
2. **大 HTML 只用 write_file** — 禁止 `exec` + `cat`/`heredoc`/`python3 -c` 写整页 HTML（>500 字符）。弱模型会把 CSS 拆成 JSON 数组导致 `dictionary update sequence` 崩溃。
3. **先执行后汇报** — 每个阶段必须 `exec` 并检查 exit code / 文件大小。
4. **搜索预算 ≤2 轮** — 两轮搜不到直链就换官方源 / 复制本地已有目录。
5. **必须验证** — 结束前 `curl -I` 本地页面、ZIP、公网 tunnel URL。

> 单页品牌/产品/赛博朋克落地页（非画廊）→ 读 `skills/static-landing-page/SKILL.md`，本 skill 只管画廊+ZIP 流水线。

## 标准流程（按顺序）

### 0. 侦察
```bash
exec command="mkdir -p WORKDIR && ls -la WORKDIR && ls -la /root 2>/dev/null | head -20"
```
若发现类似 `*-wallpapers` 且含 `index.html` + 图片 → 复制：
```bash
exec command="cp -a /path/to/existing/. WORKDIR/ && ls -lh WORKDIR"
```

### 1. 获取资源
```bash
exec command="mkdir -p WORKDIR/wallpapers && curl -fsSL -A 'Mozilla/5.0' -o WORKDIR/wallpapers/a.jpg 'URL'"
exec command="ls -lh WORKDIR/wallpapers | wc -l"
```
失败则换 URL；禁止停在「找到了链接」却不下载。

### 2. 缩略图（可选）
```python
# PIL thumbnail script via write_file + exec python3
```

### 3. 网页画廊
用 `write_file` 创建 `index.html`、`styles.css`、`app.js`：
- 网格展示 + 点击预览 + 单张下载
- 顶部「下载全部 ZIP」按钮

### 4. ZIP
```bash
exec command="cd WORKDIR && python3 -c \"import zipfile, pathlib; ...\"  # or zip -r bundle.zip wallpapers/"
```

### 5. 本地服务 + 公网 tunnel
```bash
exec command="cd WORKDIR && nohup python3 -m http.server 8091 > server.log 2>&1 & sleep 1 && curl -s -o /dev/null -w '%{http_code}' http://localhost:8091/"
exec command="cd WORKDIR && nohup cloudflared tunnel --url http://localhost:8091 > tunnel.log 2>&1 & sleep 10 && grep -oE 'https://[a-zA-Z0-9.-]+\\.trycloudflare\\.com' tunnel.log | tail -1"
```

验证 tunnel（本机 DNS 常无法解析 trycloudflare.com，不要因此判失败）：
```bash
exec command="cd WORKDIR && TUNNEL=$(grep -oE 'https://[a-zA-Z0-9.-]+\\.trycloudflare\\.com' tunnel.log | tail -1) && echo tunnel=$TUNNEL && pgrep -af cloudflared | head -3 && curl -s -o /dev/null -w 'local=%{http_code}\\n' http://localhost:8091/"
```
- 本地 `curl` 200 + tunnel.log 有 URL + cloudflared 进程在跑 → 公网链接有效（外部用户可访问）
- 仅当 tunnel.log 无 URL 或 cloudflared 未启动 → 重试 tunnel

### 6. 交付总结（中文）
必须包含：
- 文件列表（`ls -lh WORKDIR`）
- 本地 URL
- 公网 URL（从 tunnel.log 提取）
- ZIP 下载路径

## 完成判定

全部满足才算完成：
- [ ] `WORKDIR` 下有 ≥3 张图片或用户要求数量
- [ ] `index.html` 存在且本地 `curl` 返回 200
- [ ] ZIP 存在且 >1MB（或合理大小）
- [ ] tunnel.log 含 `https://*.trycloudflare.com` 且 cloudflared 进程存活

任一不满足 → 继续工具调用，不要结束回合。