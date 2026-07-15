"""Tests for deliverable task detection in groupchat prompts."""

from nanobot.groupchat.context.deliverable_hint import detect_deliverable_hint


def test_empty_question_returns_empty():
    assert detect_deliverable_hint("") == ""
    assert detect_deliverable_hint("   ") == ""


def test_landing_page_triggers_static_skill():
    hint = detect_deliverable_hint("帮我做一个赛博朋克风的 Claude 单页网站，要公网链接")
    assert "static-landing-page" in hint
    assert "write_file" in hint
    assert "禁止" in hint


def test_gallery_triggers_web_deliverable():
    hint = detect_deliverable_hint("收集壁纸做一个画廊页面，要能下载全部 ZIP")
    assert "画廊/ZIP 交付" in hint
    assert hint.index("web-deliverable") < hint.index("static-landing-page")


def test_html_page_defaults_to_landing():
    hint = detect_deliverable_hint("直接写 HTML index.html 部署")
    assert "static-landing-page" in hint


def test_non_deliverable_returns_empty():
    assert detect_deliverable_hint("解释一下量子纠缠") == ""
    assert detect_deliverable_hint("好了吗") == ""