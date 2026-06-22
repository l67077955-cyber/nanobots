# 交付型任务补充指令

用户要**成品**（网站、落地页、画廊、ZIP、公网链接）时：

## 第一步（强制）

1. 单页/品牌/赛博朋克站 → `read_file skills/static-landing-page/SKILL.md`
2. 画廊/ZIP → `read_file skills/web-deliverable/SKILL.md`

## 工具纪律

- **>500 字符 HTML 必须用 `write_file`**，禁止 exec heredoc/cat
- 禁止连续 `web_search` 超过 skill 规定的预算而不写文件
- 相邻目录已有 `index.html` 成品 → 优先 `cp -a` 后 `edit_file` 迭代

## 质量门禁（单页）

- Hero 多层场景 + 氛围组件（sysbar/ticker/glitch 等）
- `#models` 独立 tier 卡片，禁止复用 about-card
- `@media` 响应式
- 本地 curl 200 + tunnel URL（如需要公网）

## 交付格式

```
## 交付物
- 目录 / 文件 / 大小

## 访问
- 本地 / 公网 URL

## 设计要点
- 已满足的 QA 项
```