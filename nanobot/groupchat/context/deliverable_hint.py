"""Detect web-deliverable user requests and emit a compact skill routing hint."""

from __future__ import annotations

import re

# Gallery / bundle tasks → web-deliverable
_GALLERY_PATTERNS = re.compile(
    r"画廊|壁纸|wallpaper|gallery|批量下载|下载全部|zip\s*包|bundle\.zip",
    re.IGNORECASE,
)

# Single-page landing tasks → static-landing-page
_LANDING_PATTERNS = re.compile(
    r"网站|网页|落地页|单页|landing\s*page|index\.html|"
    r"html\s*页|赛博朋克|cyberpunk|公网链接|trycloudflare|"
    r"http\.server|tunnel|可浏览",
    re.IGNORECASE,
)

# Strong landing signals — override gallery when both match (e.g. "画廊页面" → gallery)
_LANDING_EXCLUSIVE = re.compile(
    r"落地页|单页|landing\s*page|index\.html|赛博朋克|cyberpunk|品牌页|产品介绍",
    re.IGNORECASE,
)

# Generic deliverable (either skill)
_DELIVERABLE_PATTERNS = re.compile(
    r"公网|部署|上线|browseable|browsable|web\s*page|静态站",
    re.IGNORECASE,
)


def detect_deliverable_hint(user_question: str) -> str:
    """Return a prompt injection block when *user_question* looks like a web deliverable.

    Empty string when no match — keeps prompts lean for non-delivery tasks.
    """
    if not user_question or not user_question.strip():
        return ""

    q = user_question.strip()
    is_gallery = bool(_GALLERY_PATTERNS.search(q))
    is_landing = bool(_LANDING_PATTERNS.search(q))
    is_generic = bool(_DELIVERABLE_PATTERNS.search(q))

    if not (is_gallery or is_landing or is_generic):
        return ""

    landing_exclusive = bool(_LANDING_EXCLUSIVE.search(q))

    if is_gallery and not landing_exclusive:
        primary = "skills/web-deliverable/SKILL.md"
        secondary = "skills/static-landing-page/SKILL.md"
        task = "画廊/ZIP 交付"
    elif is_landing or landing_exclusive:
        primary = "skills/static-landing-page/SKILL.md"
        secondary = "skills/web-deliverable/SKILL.md"
        task = "单页落地站"
    else:
        primary = "skills/static-landing-page/SKILL.md"
        secondary = "skills/web-deliverable/SKILL.md"
        task = "网页交付"

    return (
        f"[交付任务检测] 用户请求属于「{task}」。立即执行：\n"
        f"1. `read_file {primary}` 并按流程执行（备选: {secondary}）\n"
        f"2. **禁止**用 exec/heredoc/cat 写 >500 字符 HTML — 必须用 `write_file`\n"
        f"3. 单页须满足 QA：多层 hero、独立 models 区、@media 响应式、curl 200、tunnel URL\n"
        f"4. 未完成 QA 前禁止汇报「已完成」"
    )