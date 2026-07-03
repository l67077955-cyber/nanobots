from nanobot.groupchat.history.response_cleanup import clean_response


def test_clean_response_strips_agent_prefix_only_at_line_start():
    content = "Harper: 结论如下\n正文里保留 Kirk: 这个标签用于引用。"

    cleaned = clean_response(content, "Harper", ["Harper", "Kirk"])

    assert cleaned.startswith("结论如下")
    assert "Kirk: 这个标签" in cleaned


def test_clean_response_strips_multiline_agent_prefixes():
    content = "Harper: 第一行\nKirk：第二行\n普通正文"

    cleaned = clean_response(content, "Harper", ["Harper", "Kirk"])

    assert cleaned == "第一行\n第二行\n普通正文"
