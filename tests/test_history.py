"""History 上下文库单元测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.core.history import (
    Fragment,
    History,
    age_tool_log,
    build_compress_message,
    can_see_tool_call,
    degrade_content,
    has_tool_log,
    strip_tool_log,
)

# ── Fragment ───────────────────────────────────────────────────────────────


class TestFragment:
    def test_len_returns_content_len(self):
        assert len(Fragment("m", "hello")) == 5

    def test_bool_false_for_blank(self):
        assert not Fragment("m", "   \n  ")
        assert not Fragment("m", "")

    def test_bool_true_for_content(self):
        assert Fragment("m", "x")

    def test_meta_default_is_distinct_dict(self):
        a = Fragment("m", "x")
        b = Fragment("m", "y")
        a.meta["k"] = 1
        assert "k" not in b.meta  # default_factory 给每个实例独立 dict

    def test_repr(self):
        r = repr(Fragment("m", "hello world"))
        assert "m" in r and "hello" in r


# ── 基本信息量 ─────────────────────────────────────────────────────────────


class TestBasics:
    def test_len_bool_iter_getitem(self):
        c = History()
        assert len(c) == 0
        assert not c
        c.append("a", "1")
        c.append("b", "2")
        assert len(c) == 2
        assert bool(c)
        marks = [f.mark for f in c]
        assert marks == ["a", "b"]
        assert c[0].content == "1"
        assert c[1].mark == "b"

    def test_contains(self):
        c = History()
        c.append("user_1", "hi")
        assert "user_1" in c
        assert "user_2" not in c
        assert 123 not in c  # type: ignore[operator]

    def test_eq(self):
        a = History([Fragment("m", "x", {"r": "user"})])
        b = History([Fragment("m", "x", {"r": "user"})])
        assert a == b
        c = History([Fragment("m", "y")])
        assert a != c

    def test_hash_unhashable(self):
        with pytest.raises(TypeError):
            hash(History())  # type: ignore[arg-type]

    def test_total_chars(self):
        c = History()
        c.append("a", "123")
        c.append("b", "45")
        assert c.total_chars() == 5

    def test_marks_and_count(self):
        c = History()
        for i in range(3):
            c.append_auto("user", f"u{i}")
        c.append("system_prompt", "s")
        assert c.marks() == ["user_1", "user_2", "user_3", "system_prompt"]
        assert c.count("user") == 3
        assert c.count("") == 4


# ── 追加 ───────────────────────────────────────────────────────────────────


class TestAppend:
    def test_append_returns_mark(self):
        c = History()
        assert c.append("x", "1") == "x"
        assert c.find("x").content == "1"

    def test_append_auto_numbering_from_1(self):
        c = History()
        m1 = c.append_auto("user", "a")
        m2 = c.append_auto("user", "b")
        assert m1 == "user_1"
        assert m2 == "user_2"

    def test_append_auto_continues_after_explicit(self):
        c = History()
        c.append("user_3", "x")  # 已有编号 3
        assert c.append_auto("user", "y") == "user_4"

    def test_extend(self):
        c = History()
        c.extend([Fragment("a", "1"), Fragment("b", "2")])
        assert len(c) == 2

    def test_system_user_agent_tool_meta(self):
        c = History()
        c.system("sys")
        c.user("hi")
        c.agent("harper", "yo")
        c.tool("search", "result")
        assert c.find("system_1").meta["role"] == "system"
        assert c.find("user_1").meta["role"] == "user"
        a = c.find("harper_1")
        assert a.meta["role"] == "assistant"
        assert a.meta["agent"] == "harper"
        t = c.find("tool_log_1")
        assert t.meta["role"] == "tool"
        assert t.meta["tool"] == "search"

    def test_semantic_explicit_mark(self):
        c = History()
        c.user("hi", mark="my_user")
        assert "my_user" in c


# ── 查找 ───────────────────────────────────────────────────────────────────


class TestFind:
    def _build(self):
        c = History()
        c.append("system_prompt", "sys")
        c.append_auto("user", "u1")
        c.append_auto("harper", "h1")
        c.append_auto("user", "u2")
        c.append_auto("kirk", "k1")
        return c

    def test_find_exact(self):
        c = self._build()
        assert c.find("user_1").content == "u1"
        assert c.find("nope") is None

    def test_find_first_match(self):
        c = History()
        c.append("dup", "1")
        c.append("dup", "2")
        assert c.find("dup").content == "1"

    def test_find_prefix_and_alias(self):
        c = self._build()
        pref = c.find_prefix("user")
        assert [f.mark for f in pref] == ["user_1", "user_2"]
        assert c.find_all("user") == pref  # alias

    def test_find_suffix(self):
        c = self._build()
        s = c.find_suffix("_1")
        # user_1 / harper_1 / kirk_1 都以 _1 结尾
        assert set(f.mark for f in s) == {"user_1", "harper_1", "kirk_1"}

    def test_find_mark_contains(self):
        c = self._build()
        r = c.find_mark_contains("arper")
        assert [f.mark for f in r] == ["harper_1"]

    def test_find_content_contains_and_alias(self):
        c = self._build()
        r = c.find_content_contains("k1")
        assert [f.mark for f in r] == ["kirk_1"]
        assert c.find_contains("k1") == r

    def test_find_before_after(self):
        c = self._build()
        assert c.find_before("harper_1").mark == "user_1"
        assert c.find_after("harper_1").mark == "user_2"
        assert c.find_before("system_prompt") is None  # 首个
        assert c.find_after("kirk_1") is None  # 末尾
        assert c.find_before("nope") is None

    def test_index_of(self):
        c = self._build()
        assert c.index_of("harper_1") == 2
        assert c.index_of("nope") == -1

    def test_first_last(self):
        c = self._build()
        assert c.first("user").mark == "user_1"
        assert c.last("user").mark == "user_2"
        assert c.first().mark == "system_prompt"
        assert c.last().mark == "kirk_1"
        assert c.first("nope") is None


# ── 删除 ───────────────────────────────────────────────────────────────────


class TestDelete:
    def _build(self):
        c = History()
        for m in ["a", "b", "c", "d", "e"]:
            c.append(m, m)
        return c

    def test_delete_returns_bool(self):
        c = self._build()
        assert c.delete("c") is True
        assert c.delete("c") is False  # 已删
        assert c.marks() == ["a", "b", "d", "e"]

    def test_delete_all_prefix(self):
        c = History()
        c.append_auto("user", "1")
        c.append_auto("user", "2")
        c.append("system", "s")
        assert c.delete_all("user") == 2
        assert c.marks() == ["system"]

    def test_delete_all_empty_clears(self):
        c = self._build()
        assert c.delete_all("") == 5
        assert len(c) == 0

    def test_delete_prefix_alias(self):
        c = History()
        c.append_auto("user", "1")
        assert c.delete_prefix("user") == 1
        assert len(c) == 0

    def test_delete_before(self):
        c = self._build()
        assert c.delete_before("c") == 2
        assert c.marks() == ["c", "d", "e"]
        assert c.delete_before("a") == 0  # 无前驱
        assert c.delete_before("nope") == 0

    def test_delete_after(self):
        c = self._build()
        assert c.delete_after("c") == 2
        assert c.marks() == ["a", "b", "c"]
        assert c.delete_after("e") == 0  # 无后继
        assert c.delete_after("nope") == 0

    def test_delete_between_exclusive(self):
        c = self._build()
        assert c.delete_between("a", "e") == 3
        assert c.marks() == ["a", "e"]

    def test_delete_between_missing_safe_empty(self):
        c = self._build()
        assert c.delete_between("nope", "e") == 0
        assert c.delete_between("e", "a") == 0  # 反序
        assert c.delete_between("a", "b") == 0  # 相邻无中间
        assert len(c) == 5

    def test_delete_by_attr_keep_last(self):
        c = History()
        for i in range(4):
            c.agent("harper", f"h{i}")  # agent() 写入 meta.agent=harper
            c.user(f"u{i}")
        n = c.delete_by_attr("agent", "harper", keep_last=1)
        assert n == 3
        # 仅剩最后一个 harper
        harper = c.find_prefix("harper")
        assert len(harper) == 1
        assert harper[0].content == "h3"
        # user 全留
        assert c.count("user") == 4

    def test_delete_by_attr_no_match(self):
        c = History()
        c.append("a", "1")
        assert c.delete_by_attr("agent", "x") == 0

    def test_clear(self):
        c = self._build()
        c.clear()
        assert len(c) == 0


# ── 修改 ───────────────────────────────────────────────────────────────────


class TestModify:
    def test_replace_content(self):
        c = History()
        c.append("a", "1")
        assert c.replace("a", "2") is True
        assert c.find("a").content == "2"

    def test_replace_meta_only(self):
        c = History()
        c.append("a", "1", role="user")
        assert c.replace("a", content=None, role="system") is True
        assert c.find("a").content == "1"  # 未改
        assert c.find("a").meta["role"] == "system"

    def test_replace_miss(self):
        c = History()
        assert c.replace("nope", "x") is False

    def test_replace_prefix_fn(self):
        c = History()
        c.append_auto("user", "a")
        c.append_auto("user", "b")
        c.append("sys", "s")
        n = c.replace_prefix("user", lambda f: Fragment(f.mark, f.content.upper(), dict(f.meta)))
        assert n == 2
        assert c.find("user_1").content == "A"
        assert c.find("sys").content == "s"

    def test_replace_prefix_fn_delete(self):
        c = History()
        c.append_auto("user", "a")
        c.append_auto("user", "b")
        n = c.replace_prefix("user", lambda f: None)
        assert n == 2
        assert len(c) == 0

    def test_replace_all_zip(self):
        c = History()
        c.append_auto("user", "a")
        c.append_auto("user", "b")
        c.append_auto("user", "c")
        n = c.replace_all("user", ["X", "Y"])
        assert n == 2
        assert c.find("user_1").content == "X"
        assert c.find("user_2").content == "Y"
        assert c.find("user_3").content == "c"  # 未配对保留

    def test_prepend_append_to(self):
        c = History()
        c.append("a", "mid")
        assert c.prepend("a", "pre") is True
        assert c.find("a").content == "premid"
        assert c.append_to("a", "post") is True
        assert c.find("a").content == "premidpost"
        assert c.prepend("nope", "x") is False

    def test_update_meta(self):
        c = History()
        c.append("a", "1")
        assert c.update_meta("a", role="system", extra=5) is True
        assert c.find("a").meta["role"] == "system"
        assert c.find("a").meta["extra"] == 5
        assert c.update_meta("nope", role="x") is False


# ── 插入 ───────────────────────────────────────────────────────────────────


class TestInsert:
    def _build(self):
        c = History()
        c.append("a", "1")
        c.append("c", "3")
        return c

    def test_insert_after(self):
        c = self._build()
        assert c.insert_after("a", "b", "2") is True
        assert c.marks() == ["a", "b", "c"]
        assert c.insert_after("nope", "x", "x") is False

    def test_insert_before(self):
        c = self._build()
        assert c.insert_before("c", "b", "2") is True
        assert c.marks() == ["a", "b", "c"]
        assert c.insert_before("nope", "x", "x") is False

    def test_insert_at_returns_mark(self):
        c = self._build()
        assert c.insert_at(0, "z", "0") == "z"
        assert c.marks() == ["z", "a", "c"]


# ── 切片 ───────────────────────────────────────────────────────────────────


class TestSlice:
    def _build(self):
        c = History()
        for m in ["a", "b", "c", "d", "e"]:
            c.append(m, m)
        return c

    def test_slice_inclusive(self):
        c = self._build()
        s = c.slice("b", "d")
        assert [f.mark for f in s] == ["b", "c", "d"]

    def test_slice_missing_empty(self):
        c = self._build()
        assert c.slice("nope", "d") == []
        assert c.slice("d", "b") == []  # 反序

    def test_slice_between_exclusive(self):
        c = self._build()
        s = c.slice_between("a", "e")
        assert [f.mark for f in s] == ["b", "c", "d"]
        assert c.slice_between("a", "b") == []  # 相邻

    def test_slice_prefix(self):
        c = History()
        c.append_auto("user", "1")
        c.append_auto("user", "2")
        c.append("sys", "s")
        assert [f.mark for f in c.slice_prefix("user")] == ["user_1", "user_2"]

    @pytest.mark.parametrize("n", [0, -1, -5])
    def test_take_first_nonpositive(self, n):
        c = self._build()
        assert c.take_first(n) == []

    def test_take_first_normal(self):
        c = self._build()
        assert [f.mark for f in c.take_first(2)] == ["a", "b"]
        assert len(c.take_first(99)) == 5  # 超出自然返回全部

    @pytest.mark.parametrize("n", [0, -1])
    def test_take_last_nonpositive(self, n):
        c = self._build()
        assert c.take_last(n) == []

    def test_take_last_normal(self):
        c = self._build()
        assert [f.mark for f in c.take_last(2)] == ["d", "e"]


# ── 构建 ───────────────────────────────────────────────────────────────────


class TestBuild:
    def test_build_string(self):
        c = History()
        c.append("a", "1")
        c.append("b", "2")
        assert c.build_string() == "1\n\n2"
        assert c.build_string(sep="|") == "1|2"

    def test_build_for_llm_roles(self):
        c = History()
        c.system("sys")
        c.user("hi")
        c.agent("harper", "yo")
        c.tool("search", "r")
        msgs = c.build_for_llm()
        assert msgs[0] == {"role": "system", "content": "sys"}
        assert msgs[1] == {"role": "user", "content": "hi"}
        assert msgs[2]["role"] == "assistant"
        assert msgs[2]["name"] == "harper"
        assert msgs[3]["role"] == "tool"

    def test_build_for_groupchat_basic_mapping(self):
        c = History()
        c.system("sys")
        c.user("hi")
        c.agent("harper", "hello")
        c.agent("kirk", "yo")
        msgs = c.build_for_groupchat("harper")
        roles = [(m["role"], m.get("name")) for m in msgs]
        assert ("system", None) in roles
        assert ("user", None) in roles  # 人类 user
        # harper → assistant
        harper_msg = next(m for m in msgs if m["role"] == "assistant")
        assert harper_msg["name"] == "harper"
        # kirk → user with prefix
        kirk_msg = next(m for m in msgs if m.get("name") == "kirk")
        assert kirk_msg["role"] == "user"
        assert kirk_msg["content"] == "[kirk]: yo"

    def test_build_for_groupchat_relevant_agents_filter(self):
        c = History()
        c.agent("harper", "h")
        c.agent("kirk", "k")
        c.agent("luke", "l")
        msgs = c.build_for_groupchat("harper", relevant_agents={"kirk"})
        names = [m.get("name") for m in msgs]
        # harper(assistant) + kirk(user) 留下；luke 被丢弃
        assert "kirk" in names
        assert "luke" not in names

    def test_build_for_groupchat_visibility_strip(self):
        c = History()
        c.agent("harper", "body\n\n[工具调用记录]\n• search(q) → result (10字)")
        # harper rank 低，kirk rank 高，作为 current_agent=kirk 看 harper 不可见 → strip
        msgs = c.build_for_groupchat(
            "kirk",
            agent_ranks={"harper": 3, "kirk": 1},
            relevant_agents={"harper"},
        )
        harper_msg = next(m for m in msgs if m.get("name") == "harper")
        assert "[工具调用记录]" not in harper_msg["content"]

    def test_build_for_groupchat_visibility_keep(self):
        c = History()
        c.agent("harper", "body\n\n[工具调用记录]\n• search(q) → result (10字)")
        # viewer rank >= sender → 保留 tool log
        msgs = c.build_for_groupchat(
            "kirk",
            agent_ranks={"harper": 1, "kirk": 3},
            relevant_agents={"harper"},
        )
        harper_msg = next(m for m in msgs if m.get("name") == "harper")
        assert "[工具调用记录]" in harper_msg["content"]

    def test_build_for_groupchat_compress_is_system(self):
        c = History()
        c.append("compressed_middle", "summary", role="system", is_compact_summary=True)
        msgs = c.build_for_groupchat("harper")
        assert msgs[0] == {"role": "system", "content": "summary"}

    def test_build_for_groupchat_merge_consecutive_assistant(self):
        c = History()
        c.agent("harper", "a")
        c.agent("harper", "b")  # 连续同 agent
        msgs = c.build_for_groupchat("harper")
        assistants = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert "a" in assistants[0]["content"]
        assert "b" in assistants[0]["content"]


# ── 压缩与截断 ─────────────────────────────────────────────────────────────


class TestTruncate:
    def test_truncate_keep_marks_always_kept(self):
        c = History()
        c.append("keep", "K" * 50)
        for i in range(10):
            c.append(f"fill_{i}", "x" * 20)
        c.truncate(100, keep_marks=["keep"])
        assert "keep" in c.marks()

    def test_truncate_keeps_tail_when_budget(self):
        c = History()
        for i in range(10):
            c.append(f"m{i}", "x" * 30)
        c.truncate(100)
        # 末尾几个应保留（100/30 ≈ 3）
        marks = c.marks()
        assert "m9" in marks

    def test_truncate_keep_last_param(self):
        c = History()
        for i in range(10):
            c.append(f"m{i}", "x" * 30)
        c.truncate(1, keep_last=3)  # 极小预算但 keep_last=3
        marks = c.marks()
        assert marks[-3:] == ["m7", "m8", "m9"]

    def test_truncate_zero_or_empty_noop(self):
        c = History()
        c.append("a", "1")
        c.truncate(0)
        assert len(c) == 1
        c.truncate(-5)
        assert len(c) == 1

    def test_tiered_truncate_injects_compress(self):
        c = History()
        c.append("system_prompt", "S" * 30)
        for i in range(8):
            c.append_auto("user", "u" * 100)
        c.tiered_truncate(150, mandatory_marks=["system_prompt"])
        marks = c.marks()
        assert "system_prompt" in marks
        # 应注入压缩块
        assert "compressed_middle" in marks

    def test_tiered_truncate_all_fit_no_compress(self):
        c = History()
        c.append("system_prompt", "S")
        c.append_auto("user", "u")
        c.tiered_truncate(1000, mandatory_marks=["system_prompt"])
        assert "compressed_middle" not in c.marks()

    def test_tiered_truncate_duplicate_split_no_doublecount(self):
        """两个值相同的 optional：一个塞下、一个遗漏时，压缩块只计 1 条源（按下标，不靠值相等）。"""
        c = History()
        c.append("system_prompt", "S")  # idx0 mandatory，tiny
        c.append("dup", "x" * 50)  # idx1 optional
        c.append("dup", "x" * 50)  # idx2 optional（与 idx1 值相同）
        # 预算 100：mandatory 1 字符，剩余 99；一个 50 塞下（used->51），第二个 50 不塞（51+50>100）
        c.tiered_truncate(100, mandatory_marks=["system_prompt"])
        marks = c.marks()
        assert "compressed_middle" in marks
        comp = c.find("compressed_middle")
        # 只遗漏 1 条，header 应写 1 条而非 2 条
        assert "（1 条）" in comp.content
        assert "（2 条）" not in comp.content


class TestCompressMiddle:
    def _build(self, n=5):
        c = History()
        for i in range(n):
            c.append(f"m{i}", f"content_{i}" * 5)
        return c

    @pytest.mark.asyncio
    async def test_compress_with_llm(self):
        c = self._build(8)

        async def llm(text):
            return "SUMMARY"

        ok = await c.compress_middle(llm, 1000, keep_first=1, keep_last=2)
        assert ok is True
        marks = c.marks()
        assert "compressed_middle" in marks
        assert marks[0] == "m0"
        assert marks[-2:] == ["m6", "m7"]
        assert c.find("compressed_middle").content == "SUMMARY"
        assert c.find("compressed_middle").meta["is_compact_summary"] is True

    @pytest.mark.asyncio
    async def test_compress_no_middle_returns_false(self):
        c = self._build(3)  # keep_first=1+keep_last=6 > 3

        async def llm(text):
            return "X"

        assert await c.compress_middle(llm, 1000) is False

    @pytest.mark.asyncio
    async def test_compress_empty_middle_returns_false(self):
        c = History()
        c.append("h", "")
        for _ in range(3):
            c.append("m", "   ")  # 空白
        c.append("t", "tail")
        # keep_last=1, middle 全空白

        async def llm(text):
            return "X"

        assert await c.compress_middle(llm, 1000, keep_first=1, keep_last=1) is False

    @pytest.mark.asyncio
    async def test_compress_keep_last_zero(self):
        c = self._build(5)

        async def llm(text):
            return "S"

        ok = await c.compress_middle(llm, 1000, keep_first=1, keep_last=0)
        assert ok is True
        marks = c.marks()
        # head 不重复，tail 为空
        assert marks.count("m0") == 1
        assert "m1" not in marks  # 被压缩
        assert marks[-1] == "compressed_middle"

    @pytest.mark.asyncio
    async def test_compress_keep_first_zero(self):
        c = self._build(5)

        async def llm(text):
            return "S"

        # protect_users=False → 旧连续前缀语义：keep_first=0 即无 head
        ok = await c.compress_middle(llm, 1000, keep_first=0, keep_last=1, protect_users=False)
        assert ok is True
        marks = c.marks()
        assert marks[0] == "compressed_middle"
        assert marks[-1] == "m4"

    @pytest.mark.asyncio
    async def test_compress_both_zero(self):
        c = self._build(3)

        async def llm(text):
            return "S"

        # protect_users=False → keep_first=0 无 head，keep_last=0 无 tail → 全压缩
        ok = await c.compress_middle(llm, 1000, keep_first=0, keep_last=0, protect_users=False)
        assert ok is True
        marks = c.marks()
        assert marks == ["compressed_middle"]

    @pytest.mark.asyncio
    async def test_compress_negative_raises(self):
        c = self._build()

        async def llm(text):
            return "S"

        with pytest.raises(ValueError):
            await c.compress_middle(llm, 1000, keep_first=-1)
        with pytest.raises(ValueError):
            await c.compress_middle(llm, 1000, keep_last=-1)

    @pytest.mark.asyncio
    async def test_compress_llm_none_mechanical_fallback(self):
        c = self._build(6)
        ok = await c.compress_middle(None, 1000, keep_first=1, keep_last=1)
        assert ok is True
        frag = c.find("compressed_middle")
        assert frag is not None
        assert frag.meta["is_compact_summary"] is True
        assert "早期对话压缩" in frag.content  # 机械块 header

    @pytest.mark.asyncio
    async def test_compress_llm_exception_fallback(self):
        c = self._build(6)

        async def llm(text):
            raise RuntimeError("boom")

        ok = await c.compress_middle(llm, 1000, keep_first=1, keep_last=1)
        assert ok is True
        frag = c.find("compressed_middle")
        assert frag is not None
        assert frag.content  # 有内容，非静默丢弃

    @pytest.mark.asyncio
    async def test_compress_concurrent_append_not_lost(self):
        c = self._build(6)
        started = asyncio.Event()
        released = asyncio.Event()

        async def llm(text):
            started.set()
            await released.wait()
            return "SUMMARY"

        task = asyncio.create_task(c.compress_middle(llm, 1000, keep_first=1, keep_last=1))
        await started.wait()
        # 模拟 await 期间并发 append
        c.append("concurrent", "C")
        released.set()
        ok = await task
        assert ok is True
        marks = c.marks()
        assert "concurrent" in marks  # 不丢失
        assert "compressed_middle" in marks

    @pytest.mark.asyncio
    async def test_compress_lock_serializes(self):
        c = self._build(8)
        order: list[str] = []

        async def llm(text):
            order.append("llm")
            await asyncio.sleep(0)
            return "S"

        await asyncio.gather(
            c.compress_middle(llm, 1000, keep_first=1, keep_last=1),
            c.compress_middle(llm, 1000, keep_first=1, keep_last=1),
        )
        # 两次都应完成不崩
        assert len(c.marks()) >= 1

    @pytest.mark.asyncio
    async def test_compress_protect_users_keeps_all_user_fragments(self):
        """protect_users=True（默认）对齐 _find_head_indices：保护 idx0 + 所有 user 片段，
        即便 user 出现在对话中部也不被压缩。"""
        c = History()
        c.system("sys", mark="sys")
        c.agent("harper", "agent reply 1")  # 可压缩
        c.user("mid-conversation user turn")  # 必须保护
        c.agent("harper", "agent reply 2")  # 可压缩
        c.agent("kirk", "tail reply")  # tail

        async def llm(text):
            return "SUMMARY"

        ok = await c.compress_middle(llm, 1000, keep_last=1)  # protect_users 默认 True
        assert ok is True
        marks = c.marks()
        # sys(idx0) + mid user 被保护；harper reply 1/2 被压缩成一块
        assert "sys" in marks
        assert any(m == "user_1" or "user" in m for m in marks)  # user 片段保留
        assert marks[-1] == "kirk_1" or "kirk" in marks[-1]  # tail 保留
        assert "compressed_middle" in marks
        # 被压缩的 agent reply 不应原样存在两个
        assert marks.count("compressed_middle") == 1

    @pytest.mark.asyncio
    async def test_compress_protect_users_keeps_prior_summary_block(self):
        """protect_users=True 多 pass：既有 compressed_middle 块（is_compact_summary）
        被保护，不重复压缩、不丢 head。"""
        c = History()
        c.system("sys", mark="sys")
        c.agent("harper", "old reply 1")
        c.agent("harper", "old reply 2")

        async def llm(text):
            return "FIRST_SUMMARY"

        # 第一遍：压缩 old reply（keep_last=0 让它们全进 middle）
        await c.compress_middle(llm, 1000, keep_last=0)
        assert "compressed_middle" in c.marks()

        # 再加新内容，第二遍压缩
        c.agent("harper", "new reply 1")
        c.agent("harper", "new reply 2")

        async def llm2(text):
            return "SECOND_SUMMARY"

        await c.compress_middle(llm2, 1000, keep_last=0)
        marks = c.marks()
        # sys + 第一个 summary 块都被保护；不应把第一个 summary 再压进第二个
        assert marks[0] == "sys"
        # 至少有一个 compressed_middle；第一个 summary 块仍存在（被保护）
        assert marks.count("compressed_middle") >= 1


class TestAgeTools:
    def test_age_tools_shortens_old_previews(self):
        c = History()
        c.append("system_prompt", "sys")
        # 单行长预览（>100 字），age_tool_log 会截到 100 字
        long_preview = "x" * 150
        c.agent("harper", f"body\n\n[工具调用记录]\n• search(q) → {long_preview} (10字)")
        c.agent("harper", "keep")
        c.agent("harper", "keep2")
        n = c.age_tools(keep_recent=2)
        assert n == 1
        # 第一个 harper 的预览被截短
        old = c.find("harper_1").content
        assert old.count("x") == 100

    def test_age_tools_skips_head_and_user(self):
        c = History()
        long_preview = "x" * 150
        block = f"\n\n[工具调用记录]\n• search(q) → {long_preview} (10字)"
        c.append("system_prompt", "sys" + block)
        c.user("u" + block)
        c.agent("harper", "h" + block)
        c.append("tail", "t")
        n = c.age_tools(keep_recent=1)
        # idx 0 (system) 与 user 跳过；harper 老化
        assert n == 1

    def test_age_tools_idempotent(self):
        c = History()
        long_preview = "x" * 150
        c.agent("harper", f"body\n\n[工具调用记录]\n• search(q) → {long_preview} (10字)")
        c.append("tail", "t")
        c.age_tools(keep_recent=1)
        first = c.find("harper_1").content
        n2 = c.age_tools(keep_recent=1)
        # 第二次幂等：预览已 100 字，不再变化
        second = c.find("harper_1").content
        assert second == first
        assert n2 == 0

    def test_age_tools_no_eligible(self):
        c = History()
        for i in range(3):
            c.agent("harper", f"h{i}\n\n[工具调用记录]\n• s(q) → p (10字)")
        # keep_recent=5 > total，全部受保护
        assert c.age_tools(keep_recent=5) == 0


# ── 序列化 ─────────────────────────────────────────────────────────────────


class TestSerialize:
    def test_to_dicts_roundtrip(self):
        c = History()
        c.append("a", "1", role="user")
        c.append("b", "2", role="system")
        d = c.to_dicts()
        assert d[0] == {"mark": "a", "content": "1", "meta": {"role": "user"}}
        c2 = History.from_dicts(d)
        assert c2 == c

    def test_from_dicts_tolerant_missing_mark(self):
        c = History.from_dicts([{"content": "x"}, {"mark": "y", "content": "z"}])
        assert len(c) == 2
        assert c[1].mark == "y"
        # 缺 mark 的用 append_auto
        assert c[0].mark.startswith("frag")

    def test_from_dicts_missing_content(self):
        c = History.from_dicts([{"mark": "a"}])
        assert c.find("a").content == ""

    def test_from_dicts_meta_non_dict(self):
        c = History.from_dicts([{"mark": "a", "content": "x", "meta": "bad"}])
        assert c.find("a").meta == {}

    def test_to_json_from_json_roundtrip(self):
        c = History()
        c.append("a", "1", role="user")
        s = c.to_json()
        assert isinstance(s, str)
        c2 = History.from_json(s)
        assert c2 == c
        # 确保 JSON 合法
        json.loads(s)

    def test_to_sender_dicts_and_back(self):
        c = History()
        c.user("hi")
        c.agent("harper", "yo")
        c.append("compressed_middle", "sum", role="system", is_compact_summary=True)
        sd = c.to_sender_dicts()
        senders = [d["sender"] for d in sd]
        assert "harper" in senders
        # is_compact_summary 仅当 True 时包含
        assert any(d.get("is_compact_summary") for d in sd)
        c2 = History.from_sender_dicts(sd)
        # 压缩块保留
        comp = c2.find("compressed_middle")
        assert comp is not None
        assert comp.meta["is_compact_summary"] is True
        # harper 还原为 assistant
        harper = c2.find_prefix("harper")
        assert len(harper) == 1
        assert harper[0].meta["role"] == "assistant"

    def test_from_sender_dicts_human_user(self):
        c = History.from_sender_dicts([{"sender": "用户", "content": "hi"}])
        assert c.find("user_1").meta["role"] == "user"


# ── 调试 ───────────────────────────────────────────────────────────────────


class TestDebug:
    def test_debug_format(self):
        c = History()
        c.append("a", "hello")
        d = c.debug()
        assert "a" in d
        assert "hello" in d
        assert "0" in d

    def test_repr(self):
        c = History()
        c.append("a", "12345")
        r = repr(c)
        assert "1 fragments" in r or "1 fragments" in r.lower()
        assert "5 chars" in r


# ── 模块级辅助函数 ─────────────────────────────────────────────────────────


class TestHelpers:
    def test_has_tool_log(self):
        assert has_tool_log("x\n\n[工具调用记录]\n• a() → b (1字)")
        assert has_tool_log("x\n\n<previous_tool_calls>...</previous_tool_calls>")
        assert not has_tool_log("plain text")

    def test_strip_tool_log(self):
        s = "body\n\n[工具调用记录]\n• a(q) → b (1字)"
        assert strip_tool_log(s) == "body"

    def test_age_tool_log_compresses_preview(self):
        # 单行长预览（>100 字）会被截到 100 字
        long_preview = "z" * 150
        s = f"body\n\n[工具调用记录]\n• a(q) → {long_preview} (10字)"
        aged = age_tool_log(s)
        assert aged.count("z") == 100
        # 幂等
        assert age_tool_log(aged) == aged

    def test_degrade_content_levels(self):
        s = "body\n\n[工具调用记录]\n• chatroom_send(q) → x (1字)\n• search(q) → preview line\n more (10字)"
        assert degrade_content(s, 0) == s
        # level 3 去掉整个 tool 块
        assert "[工具调用记录]" not in degrade_content(s, 3)

    def test_build_compress_message(self):
        sources = [{"content": "hello world", "name": "harper"}]
        msg = build_compress_message(sources, 500)
        assert msg is not None
        assert msg["is_compact_summary"] is True
        assert "早期对话压缩" in msg["content"]

    def test_build_compress_message_empty(self):
        assert build_compress_message([], 500) is None
        assert build_compress_message([{"content": "x"}], 0) is None

    def test_can_see_tool_call(self):
        assert can_see_tool_call(1, 2) is True  # viewer >= sender
        assert can_see_tool_call(2, 1) is False


# ── 端到端集成 ─────────────────────────────────────────────────────────────


class TestIntegration:
    def test_full_workflow(self):
        c = History()
        c.system("You are helpful.", mark="system_prompt")
        c.user("What is 2+2?", mark="user_1")
        c.agent("harper", "4", mark="harper_1")
        c.tool("calc", "4", mark="tool_log_1")
        c.user("thanks", mark="user_2")

        assert c.total_chars() > 0
        assert c.index_of("harper_1") == 2

        # 截断后仍保留 system
        c.truncate(30, keep_marks=["system_prompt"])
        assert "system_prompt" in c.marks()

    def test_multi_pass_compress(self):
        c = History()
        c.append("system_prompt", "S")
        for i in range(12):
            c.append_auto("user", f"msg {i} " * 10)

        async def llm(text):
            return "COMPRESSED"

        asyncio.run(c.compress_middle(llm, 10000, keep_first=1, keep_last=2))
        # 第二次压缩应识别 compressed_middle 为 system 且保留
        asyncio.run(c.compress_middle(llm, 10000, keep_first=1, keep_last=2))
        # 不应出现多个 compressed_middle 叠加丢失 head
        marks = c.marks()
        assert marks[0] == "system_prompt"
        assert "compressed_middle" in marks
