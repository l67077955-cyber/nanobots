"""History 上下文库 — 带标记的 Fragment 列表，像字符串一样操作但可定位。

设计理念
--------
History 是唯一实体，内部是带标记 (mark) 的 Fragment 列表。所有操作垂直、线性，
不嵌套、不层层抽象：直接操作 list/dict，不搞 Message 对象或 Transformer。

agent 不存在为对象 —— agent 只是配置字典；"busy/idle" 是请求在飞的状态，不归
History 管。本库是纯数据层，不持有 provider/state，持久化由外部 hook 承担。

返回值约定（must 审视 #3）
---------------------------
- 结构性写入（append / extend / insert_at / insert_after/insert_before 的新片段）
  返回受影响的 mark(str)。
- 就地修改（replace / replace_prefix / replace_all / prepend / append_to /
  update_meta / insert_after/insert_before 的 bool 返回）返回 bool 表示是否命中。
- delete 系列：delete 返回 bool，delete_all/delete_before/delete_after/
  delete_between/delete_by_meta 返回 int（受影响数）。

mark 唯一性（must 审视 #4）
---------------------------
append 不强制 mark 唯一。所有按 mark 的查找/删除/修改作用于 **首个匹配**，
在各自 docstring 里写明。keep_marks / truncate 等保留语义保住 **所有同名匹配**。
调用方需保证唯一性时自行约束，或使用 append_auto 的自动编号。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from loguru import logger

# ── Role / Message 类型 ────────────────────────────────────────────────────
# should 审视：用 Literal/TypedDict 替换裸 dict，让 role 映射可形式化。

try:  # Python 3.11+ TypedDict
    from typing import TypedDict

    class Message(TypedDict, total=False):
        role: str
        content: str
        name: str

except ImportError:  # pragma: no cover
    Message = dict  # type: ignore[assignment,misc]


# ── 工具日志文本块处理（自包含，不依赖 groupchat） ───────────────────────
# must 审视（集成）：age_tools/degrade 操作的是 Fragment.content 内的文本块，
# 不是独立 tool Fragment。这里复刻 message_converter 的文本块规则，使本库独立。

_TOOL_LOG_RE = re.compile(
    r"(• \w+\([^)]*\) → )"
    r"([^\n]*?)"  # preview text bounded to single line (non-greedy, no cross-line)
    r"(\(\d[\d,]*字\))",  # trailing char count
)
_TOOL_LOG_BLOCK_RE = re.compile(
    r"\n*(?:\[工具调用记录\]|<previous_tool_calls>[\s\S]*?</previous_tool_calls>).*$",
    re.DOTALL,
)
_TOOL_LINE_RE = re.compile(r"^• (\w+)\(", re.MULTILINE)

# Lowest retention: group-chat / coordination tools.
CHATROOM_TOOL_NAMES = frozenset(
    {
        "chatroom_send",
        "wait",
        "quote_message",
        "list_messages",
        "manage_agent",
        "end_discussion",
        "transfer_credits",
        "clear_context",
    }
)

_TEAMMATE_PREFIX_RE = re.compile(r"^\[([^\]]+)\]: ")

_COMPRESS_HEADER = "[早期对话压缩"
_LINE_CAP = 280


def has_tool_log(content: str) -> bool:
    """content 是否包含工具调用记录文本块。"""
    return "[工具调用记录]" in content or "<previous_tool_calls>" in content


def age_tool_log(content: str) -> str:
    """把工具调用预览压缩为首行摘要（幂等）。"""
    if not has_tool_log(content):
        return content

    def _replace(m: re.Match) -> str:
        preview = m.group(2).strip()
        first_line = preview.split("\n")[0][:100]
        if first_line:
            return f"{m.group(1)}{first_line} {m.group(3)}"
        return m.group(1) + m.group(3)

    return _TOOL_LOG_RE.sub(_replace, content)


def strip_tool_log(content: str) -> str:
    """删除整段工具调用记录块，保留 agent 正文。"""
    return _TOOL_LOG_BLOCK_RE.sub("", content).rstrip()


def split_text_and_tool_log(content: str) -> tuple[str, str]:
    if not has_tool_log(content):
        return content, ""
    m = _TOOL_LOG_BLOCK_RE.search(content)
    if not m:
        return content, ""
    return content[: m.start()].rstrip(), content[m.start() :]


def strip_chatroom_tool_lines(content: str) -> str:
    """丢弃协调类工具行，保留实质性工具行 + 正文。"""
    if not has_tool_log(content):
        return content
    text, tool_block = split_text_and_tool_log(content)
    if not tool_block:
        return content
    kept: list[str] = []
    for line in tool_block.splitlines():
        m = _TOOL_LINE_RE.match(line)
        if m and m.group(1) in CHATROOM_TOOL_NAMES:
            continue
        kept.append(line)
    if "<previous_tool_calls>" in tool_block:
        body = [
            ln for ln in kept if ln.strip() and not ln.startswith("<") and not ln.startswith("</")
        ]
        if not body:
            return text.rstrip()
        rebuilt = "\n".join(["<previous_tool_calls>", *body, "</previous_tool_calls>"])
        return (text + "\n\n" + rebuilt).strip() if text else rebuilt
    body = [ln for ln in kept if ln.strip() and ln != "[工具调用记录]"]
    if not body:
        return text.rstrip()
    return (text + "\n\n[工具调用记录]\n" + "\n".join(body)).strip()


def degrade_content(content: str, level: int) -> str:
    """分层降级：0=full / 1=strip 协调工具行 / 2=age 预览 / 3=strip 全 tool 块。"""
    if level <= 0:
        return content
    if level == 1:
        return strip_chatroom_tool_lines(content)
    if level == 2:
        return age_tool_log(strip_chatroom_tool_lines(content))
    return strip_tool_log(content)


def can_see_tool_call(sender_rank: int, viewer_rank: int) -> bool:
    """默认可见性：viewer rank >= sender rank 才可见对方工具调用。"""
    return viewer_rank >= sender_rank


def _sender_of(frag: Fragment) -> str:
    """从 meta 取 sender 标识（agent 优先，其次 sender）。"""
    return str(frag.meta.get("agent") or frag.meta.get("sender") or frag.meta.get("role") or "?")


def _is_human_sender(sender: str) -> bool:
    return sender in ("用户", "User", "user")


# Alias (message_converter canonical name) — used by the dict-level trim
# functions below. Same body as _is_human_sender above.
_is_human_user_sender = _is_human_sender


def _merge_consecutive_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge back-to-back assistant messages into one (LLM APIs reject consecutive same-role)."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if out and msg.get("role") == "assistant" and out[-1].get("role") == "assistant":
            prev = out[-1]
            prev_text = prev.get("content") or ""
            cur_text = msg.get("content") or ""
            prev["content"] = f"{prev_text}\n\n{cur_text}".strip()
        else:
            out.append(msg)
    return out


def _message_char_len(msg: dict[str, Any]) -> int:
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(block.get("text", "")) for block in content if isinstance(block, dict))
    return 0


def _is_human_user_llm(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    if not isinstance(content, str):
        return True
    m = _TEAMMATE_PREFIX_RE.match(content)
    if not m:
        return True
    return _is_human_user_sender(m.group(1))


def _message_label(msg: dict[str, Any]) -> str:
    if "sender" in msg:
        return str(msg.get("sender") or "?")
    role = msg.get("role", "?")
    content = msg.get("content", "")
    if role == "user" and isinstance(content, str):
        m = _TEAMMATE_PREFIX_RE.match(content)
        if m:
            return m.group(1)
    return str(role)


def _compress_sources_text(sources: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in sources:
        raw = msg.get("content", "")
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = degrade_content(raw, 3).strip()
        if not text:
            continue
        if len(text) > _LINE_CAP:
            text = text[: _LINE_CAP - 1] + "…"
        lines.append(f"[{_message_label(msg)}] {text}")
    return "\n".join(lines)


def build_compress_message(
    sources: list[dict[str, Any]],
    max_chars: int,
    *,
    sender_format: bool = False,
) -> dict[str, Any] | None:
    """Merge dropped/overflow messages into one compressed summary block."""
    if not sources or max_chars <= 0:
        return None
    body = _compress_sources_text(sources)
    if not body:
        return None
    header = f"{_COMPRESS_HEADER}（{len(sources)} 条）]\n"
    available = max_chars - len(header)
    if available <= 0:
        return None
    if len(body) > available:
        body = body[: available - 1] + "…"
    content = header + body
    if sender_format:
        return {"sender": "系统", "content": content, "is_compact_summary": True}
    return {"role": "system", "content": content, "is_compact_summary": True}


def _merge_chronological_with_compress(
    messages: list[dict[str, Any]],
    mandatory: set[int],
    optional_indices: set[int],
    included: dict[int, tuple[int, str]],
    compress_msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild list in chronological order; compress block replaces first omitted slot."""
    out: list[dict[str, Any]] = []
    compress_placed = False
    for i, msg in enumerate(messages):
        if i in mandatory:
            out.append(msg)
            continue
        if i not in optional_indices:
            continue
        slot = included.get(i)
        if slot is not None:
            level, content = slot
            if level == 0:
                out.append(msg)
            else:
                out.append({**msg, "content": content})
        elif not compress_placed:
            out.append(compress_msg)
            compress_placed = True
    if not compress_placed:
        out.append(compress_msg)
    return out


def _replace_optional_with_compress(
    messages: list[dict[str, Any]],
    mandatory: set[int],
    optional_indices: list[int],
    compress_msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep mandatory only; replace all optional messages with one compress block."""
    out: list[dict[str, Any]] = []
    compress_placed = False
    for i, msg in enumerate(messages):
        if i in mandatory:
            out.append(msg)
            continue
        if i not in optional_indices:
            continue
        if not compress_placed:
            out.append(compress_msg)
            compress_placed = True
    if not compress_placed:
        out.append(compress_msg)
    return out


def fit_messages_to_tier_budget(
    messages: list[dict[str, Any]],
    max_chars: int,
    *,
    is_mandatory: Callable[[dict[str, Any], int], bool],
    length_fn: Callable[[dict[str, Any]], int] | None = None,
    sender_format: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Fit messages into *max_chars* with tiered retention.

    Escalation ladder:
      1. Keep human user messages at full fidelity
      2. Include agent messages from newest backward
      3. Degrade in-message: chatroom tools → age tools → strip tools
      4. Drop optional messages that still do not fit
      5. Compress dropped/overflow into one summary block; if still over
         budget, compress **all** optional messages into that single block
    """
    if max_chars <= 0 or not messages:
        return list(messages), 0

    measure = length_fn or _message_char_len

    mandatory = {i for i, m in enumerate(messages) if is_mandatory(m, i)}
    mandatory_chars = sum(measure(messages[i]) for i in mandatory)
    budget = max(0, max_chars - mandatory_chars)

    optional_indices = [i for i in range(len(messages)) if i not in mandatory]
    optional_set = set(optional_indices)
    included: dict[int, tuple[int, str]] = {}

    # Pass 1 — newest optional first; degrade before skipping.
    for i in reversed(optional_indices):
        raw = messages[i].get("content", "")
        if not isinstance(raw, str) or not raw.strip():
            continue
        for level in range(4):
            degraded = degrade_content(raw, level)
            cost = len(degraded)
            if cost <= 0:
                continue
            if budget >= cost:
                included[i] = (level, degraded)
                budget -= cost
                break

    omitted_indices = [i for i in optional_indices if i not in included]

    def _build_partial_result(active_included: dict[int, tuple[int, str]]) -> list[dict[str, Any]]:
        partial: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i in mandatory:
                partial.append(msg)
                continue
            slot = active_included.get(i)
            if slot is None:
                continue
            level, content = slot
            if level == 0:
                partial.append(msg)
            else:
                partial.append({**msg, "content": content})
        return partial

    result = _build_partial_result(included)
    total = sum(measure(m) for m in result)
    needs_compress = bool(omitted_indices) or total > max_chars

    if needs_compress and optional_indices:
        # Pass 2 — summarize omitted messages; shrink included oldest if needed.
        working_included = dict(included)
        omitted_set = list(omitted_indices)
        compress_msg = None
        for _ in range(len(working_included) + 1):
            partial = _build_partial_result(working_included)
            compress_budget = max(0, max_chars - sum(measure(m) for m in partial))
            compress_msg = build_compress_message(
                [messages[i] for i in omitted_set],
                compress_budget,
                sender_format=sender_format,
            )
            if compress_msg or not working_included:
                break
            oldest = min(working_included)
            omitted_set.append(oldest)
            del working_included[oldest]

        if compress_msg:
            included = working_included
            result = _merge_chronological_with_compress(
                messages,
                mandatory,
                optional_set,
                included,
                compress_msg,
            )
            total = sum(measure(m) for m in result)

        # Pass 3 — still over budget: all optional → one block.
        if total > max_chars:
            compress_budget = max(0, max_chars - mandatory_chars)
            compress_msg = build_compress_message(
                [messages[i] for i in optional_indices],
                compress_budget,
                sender_format=sender_format,
            )
            if compress_msg:
                result = _replace_optional_with_compress(
                    messages,
                    mandatory,
                    optional_indices,
                    compress_msg,
                )
                total = sum(measure(m) for m in result)

        # Pass 4 — truncate compress body if mandatory alone almost fills budget.
        if total > max_chars:
            for m in result:
                if m.get("content", "").startswith(_COMPRESS_HEADER):
                    overhead = total - max_chars
                    content = m.get("content", "")
                    if isinstance(content, str) and len(content) > overhead:
                        m["content"] = content[: len(content) - overhead]
                    break

    skipped = len(optional_indices) - len(included)
    if needs_compress and optional_indices:
        skipped = len(optional_indices)
    return result, skipped


def trim_sender_history(
    history: list[dict[str, str]],
    max_chars: int,
    *,
    protected_indices: set[int] | None = None,
    length_fn: Callable[[dict[str, Any]], int] | None = None,
) -> list[dict[str, str]]:
    """Tiered trim for persisted groupchat history (sender format)."""
    protected = protected_indices or set()

    def _mandatory(msg: dict[str, Any], index: int) -> bool:
        if index in protected:
            return True
        return _is_human_user_sender(msg.get("sender", ""))

    trimmed, _ = fit_messages_to_tier_budget(
        history,
        max_chars,
        is_mandatory=_mandatory,
        length_fn=length_fn,
        sender_format=True,
    )
    return trimmed


def trim_llm_messages(
    messages: list[dict[str, Any]],
    max_chars: int,
    *,
    protect_index_zero: bool = True,
    length_fn: Callable[[dict[str, Any]], int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Tiered trim for LLM-role messages from history_to_messages."""

    def _mandatory(msg: dict[str, Any], index: int) -> bool:
        if protect_index_zero and index == 0:
            return True
        return _is_human_user_llm(msg)

    return fit_messages_to_tier_budget(
        messages,
        max_chars,
        is_mandatory=_mandatory,
        length_fn=length_fn,
        sender_format=False,
    )


# ── Fragment ───────────────────────────────────────────────────────────────


@dataclass
class Fragment:
    """带标记的内容片段。

    Attributes:
        mark: 标记名，如 "system_prompt" / "user_1" / "harper_2"。
        content: 文本内容。
        meta: 可选元信息（role / agent / tool / is_compact_summary 等）。
    """

    mark: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:  # len(content)
        return len(self.content)

    def __bool__(self) -> bool:  # bool(content.strip())
        return bool(self.content.strip())

    def __repr__(self) -> str:
        preview = self.content[:30].replace("\n", " ")
        return f"Fragment(mark={self.mark!r}, len={len(self)}, preview={preview!r})"


# ── History ────────────────────────────────────────────────────────────────


class History:
    """带标记的 Fragment 列表，像字符串一样操作但可通过 mark 定位。

    纯数据层：不持有 provider/state，持久化由外部 hook 承担。所有按 mark 的
    查找/删除/修改作用于首个匹配（见模块 docstring 的 mark 唯一性约定）。
    """

    __slots__ = ("_fragments", "_lock", "_compress_active")

    def __init__(self, fragments: list[Fragment] | None = None) -> None:
        self._fragments: list[Fragment] = list(fragments) if fragments else []
        self._lock = asyncio.Lock()
        self._compress_active: bool = False

    # ── 基本信息量 ──────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._fragments)

    def __bool__(self) -> bool:
        return bool(self._fragments)

    def __iter__(self):  # type: ignore[override]
        return iter(self._fragments)

    def __getitem__(self, idx: int) -> Fragment:
        return self._fragments[idx]

    def __contains__(self, mark: object) -> bool:
        """``mark in ctx`` —— 是否存在该 mark（首个匹配即算存在）。"""
        if not isinstance(mark, str):
            return False
        return any(f.mark == mark for f in self._fragments)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, History):
            return NotImplemented
        return self._fragments == other._fragments

    def __hash__(self) -> int:  # 不可哈希（含可变 list），显式禁用以避免误用
        raise TypeError("History is mutable, unhashable")

    def total_chars(self) -> int:
        """所有 fragment content 字符总数。"""
        return sum(len(f) for f in self._fragments)

    def marks(self) -> list[str]:
        """按出现顺序返回所有 mark。"""
        return [f.mark for f in self._fragments]

    def count(self, prefix: str = "") -> int:
        """统计 mark 以 prefix 开头的 fragment 数（非破坏性，对称 delete_all）。"""
        if prefix == "":
            return len(self._fragments)
        return sum(1 for f in self._fragments if f.mark.startswith(prefix))

    # ── 内部辅助 ────────────────────────────────────────────────────────

    def _first_idx(self, mark: str) -> int:
        """返回首个匹配 mark 的下标，缺失返回 -1。"""
        for i, f in enumerate(self._fragments):
            if f.mark == mark:
                return i
        return -1

    def _next_auto_number(self, prefix: str) -> int:
        """取已存在同前缀编号的最大值 +1，从 1 起（nice 审视）。"""
        best = 0
        plen = len(prefix)
        for f in self._fragments:
            m = f.mark
            if m.startswith(prefix + "_"):
                tail = m[plen + 1 :]
                if tail.isdigit():
                    n = int(tail)
                    if n > best:
                        best = n
        return best + 1

    # ── 追加（结构性写入，返回 mark） ──────────────────────────────────

    def append(self, mark: str, content: str, **meta: Any) -> str:
        """追加一个 fragment，返回 mark。不强制 mark 唯一。"""
        self._fragments.append(Fragment(mark=mark, content=content, meta=dict(meta)))
        return mark

    def append_auto(self, prefix: str, content: str, **meta: Any) -> str:
        """自动编号 ``prefix_N`` 追加，返回 mark。编号取 max(已存在同前缀)+1，从 1 起。"""
        mark = f"{prefix}_{self._next_auto_number(prefix)}"
        self._fragments.append(Fragment(mark=mark, content=content, meta=dict(meta)))
        return mark

    def extend(self, fragments: Sequence[Fragment]) -> None:
        """批量追加 fragments。"""
        self._fragments.extend(fragments)

    # 语义化追加：role 写进 meta；mark 为 None 时自动编号。
    def system(self, content: str, mark: str | None = None) -> str:
        return self._semantic_append("system", content, mark, role="system")

    def user(self, content: str, mark: str | None = None) -> str:
        return self._semantic_append("user", content, mark, role="user")

    def agent(self, name: str, content: str, mark: str | None = None) -> str:
        """meta: role='assistant', agent=name。mark 为 None 时用 name 作 prefix 自动编号。"""
        return self._semantic_append(name, content, mark, role="assistant", agent=name)

    def tool(self, name: str, result: str, mark: str | None = None) -> str:
        """meta: role='tool', tool=name。mark 为 None 时用 'tool_log' 作 prefix 自动编号。

        注意（must 集成审视）：groupchat 模式下工具日志通常嵌在 agent Fragment.content
        内的文本块里，而非独立 tool Fragment；独立的 role=tool Fragment 仅用于单 agent
        场景。age_tools/degrade 操作的是 content 内文本块，不是此处的 tool Fragment。
        """
        return self._semantic_append("tool_log", result, mark, role="tool", tool=name)

    def _semantic_append(self, prefix: str, content: str, mark: str | None, **meta: Any) -> str:
        if mark is None:
            return self.append_auto(prefix, content, **meta)
        return self.append(mark, content, **meta)

    # ── 查找 ────────────────────────────────────────────────────────────
    # must 审视 #1：find 系列正交化。mark 匹配针对 mark 字段；content 子串另设方法。

    def find(self, mark: str) -> Fragment | None:
        """精确 mark 匹配，返回首个（或 None）。"""
        idx = self._first_idx(mark)
        return self._fragments[idx] if idx >= 0 else None

    def find_prefix(self, prefix: str) -> list[Fragment]:
        """mark 以 prefix 开头的全部 fragment（按出现顺序）。"""
        return [f for f in self._fragments if f.mark.startswith(prefix)]

    # find_all 是 find_prefix 的旧名（spec 兼容），保留为别名。
    def find_all(self, prefix: str) -> list[Fragment]:
        """``find_prefix`` 的别名（spec 原名）。"""
        return self.find_prefix(prefix)

    def find_suffix(self, suffix: str) -> list[Fragment]:
        """mark 以 suffix 结尾的全部 fragment。"""
        return [f for f in self._fragments if f.mark.endswith(suffix)]

    def find_mark_contains(self, substr: str) -> list[Fragment]:
        """mark 包含 substr 的全部 fragment。"""
        return [f for f in self._fragments if substr in f.mark]

    def find_content_contains(self, substr: str) -> list[Fragment]:
        """content 包含 substr 的全部 fragment。"""
        return [f for f in self._fragments if substr in f.content]

    def find_contains(self, substr: str) -> list[Fragment]:
        """``find_content_contains`` 的别名（spec 原名 find_contains 按 content 子串）。"""
        return self.find_content_contains(substr)

    def find_before(self, mark: str) -> Fragment | None:
        """首个 mark 的前一个 fragment（或 None）。"""
        idx = self._first_idx(mark)
        if idx <= 0:
            return None
        return self._fragments[idx - 1]

    def find_after(self, mark: str) -> Fragment | None:
        """首个 mark 的后一个 fragment（或 None）。"""
        idx = self._first_idx(mark)
        if idx < 0 or idx >= len(self._fragments) - 1:
            return None
        return self._fragments[idx + 1]

    def index_of(self, mark: str) -> int:
        """首个 mark 的下标，缺失返回 -1（find 的派生便捷方法）。"""
        return self._first_idx(mark)

    def first(self, prefix: str = "") -> Fragment | None:
        """首个 mark 以 prefix 开头的 fragment（或 None）。"""
        for f in self._fragments:
            if prefix == "" or f.mark.startswith(prefix):
                return f
        return None

    def last(self, prefix: str = "") -> Fragment | None:
        """最后一个 mark 以 prefix 开头的 fragment（或 None）。"""
        result: Fragment | None = None
        for f in self._fragments:
            if prefix == "" or f.mark.startswith(prefix):
                result = f
        return result

    # ── 删除 ────────────────────────────────────────────────────────────

    def delete(self, mark: str) -> bool:
        """删除首个匹配 mark 的 fragment，返回是否命中。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        del self._fragments[idx]
        return True

    def delete_all(self, prefix: str) -> int:
        """删除所有 mark 以 prefix 开头的 fragment，返回删除数。

        prefix='' 时清空全部（与 clear() 等价）。
        """
        if prefix == "":
            n = len(self._fragments)
            self._fragments.clear()
            return n
        keep = [f for f in self._fragments if not f.mark.startswith(prefix)]
        removed = len(self._fragments) - len(keep)
        self._fragments = keep
        return removed

    # delete_prefix 是 delete_all 的更准确命名（正交于 find_prefix）。
    def delete_prefix(self, prefix: str) -> int:
        """``delete_all`` 的正交命名别名。"""
        return self.delete_all(prefix)

    def delete_before(self, mark: str) -> int:
        """删除首个 mark 之前的所有 fragment（不含该 mark），返回删除数。"""
        idx = self._first_idx(mark)
        if idx <= 0:
            return 0
        self._fragments = self._fragments[idx:]
        return idx

    def delete_after(self, mark: str) -> int:
        """删除首个 mark 之后的所有 fragment（不含该 mark），返回删除数。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return 0
        n_after = len(self._fragments) - (idx + 1)
        if n_after <= 0:
            return 0
        self._fragments = self._fragments[: idx + 1]
        return n_after

    def delete_between(self, start: str, end: str) -> int:
        """删除首个 start 与首个 end 之间的 fragment（不含边界），返回删除数。

        边界缺失或 start 在 end 之后时返回 0（安全空操作）。
        """
        si = self._first_idx(start)
        ei = self._first_idx(end)
        if si < 0 or ei < 0 or si >= ei - 1:
            return 0
        removed = ei - si - 1
        self._fragments = self._fragments[: si + 1] + self._fragments[ei:]
        return removed

    def delete_by_meta(self, key: str, value: Any, *, keep_last: int = 0) -> int:
        """按 meta[key]==value 过滤删除，保留该条件匹配的最后 keep_last 条，返回删除数。

        must 集成审视：``clear_for_agent(agent, keep_last)`` 即
        ``delete_by_meta('agent', agent, keep_last=keep_last)``。走同一 rebuild 路径。
        """
        matching_idx = [i for i, f in enumerate(self._fragments) if f.meta.get(key) == value]
        if not matching_idx:
            return 0
        keep = set(matching_idx[-keep_last:]) if keep_last > 0 else set()
        to_delete = set(matching_idx) - keep
        self._fragments = [f for i, f in enumerate(self._fragments) if i not in to_delete]
        return len(to_delete)

    def clear(self) -> None:
        """清空所有 fragment。"""
        self._fragments.clear()

    # ── 修改（就地，返回 bool 表示命中） ────────────────────────────────

    def replace(self, mark: str, content: str | None = None, **meta: Any) -> bool:
        """修改首个匹配 mark 的 fragment。

        must 审视 #2：content 为 None 表示不改 content；meta 覆盖式更新（合并）。
        返回是否命中。
        """
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        frag = self._fragments[idx]
        if content is not None:
            frag.content = content
        if meta:
            frag.meta.update(meta)
        return True

    def replace_prefix(self, prefix: str, fn: Callable[[Fragment], Fragment | None]) -> int:
        """对每个 mark 以 prefix 开头的 fragment 调用 fn。

        fn 返回新 Fragment 替换原位置；返回 None 表示删除该项。返回受影响数。
        must 审视 #2：替代危险的 replace_all(prefix, list[str])，支持改 content+meta。
        """
        new_list: list[Fragment] = []
        affected = 0
        for f in self._fragments:
            if f.mark.startswith(prefix):
                result = fn(f)
                affected += 1
                if result is not None:
                    new_list.append(result)
            else:
                new_list.append(f)
        self._fragments = new_list
        return affected

    def replace_all(self, prefix: str, contents: Sequence[str]) -> int:
        """按顺序用 contents 替换每个前缀匹配项的 content。

        匹配数与 len(contents) 不等时按 min(匹配数, len(contents)) 截断；
        多出的 contents 被忽略，未配对的匹配项保留原 content。返回实际替换数。
        """
        replaced = 0
        it = iter(contents)
        for f in self._fragments:
            if f.mark.startswith(prefix):
                try:
                    c = next(it)
                except StopIteration:
                    break
                f.content = c
                replaced += 1
        return replaced

    def prepend(self, mark: str, text: str) -> bool:
        """在首个匹配 mark 的 content 前追加 text，返回是否命中。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        self._fragments[idx].content = text + self._fragments[idx].content
        return True

    def append_to(self, mark: str, text: str) -> bool:
        """在首个匹配 mark 的 content 后追加 text，返回是否命中。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        self._fragments[idx].content = self._fragments[idx].content + text
        return True

    def update_meta(self, mark: str, **meta: Any) -> bool:
        """覆盖式更新首个匹配 mark 的 meta，返回是否命中。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        self._fragments[idx].meta.update(meta)
        return True

    # ── 插入 ────────────────────────────────────────────────────────────

    def insert_after(self, mark: str, new_mark: str, content: str, **meta: Any) -> bool:
        """在首个匹配 mark 之后插入新 fragment，返回是否命中（未找到 mark 则不插入）。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        self._fragments.insert(idx + 1, Fragment(mark=new_mark, content=content, meta=dict(meta)))
        return True

    def insert_before(self, mark: str, new_mark: str, content: str, **meta: Any) -> bool:
        """在首个匹配 mark 之前插入新 fragment，返回是否命中。"""
        idx = self._first_idx(mark)
        if idx < 0:
            return False
        self._fragments.insert(idx, Fragment(mark=new_mark, content=content, meta=dict(meta)))
        return True

    def insert_at(self, idx: int, mark: str, content: str, **meta: Any) -> str:
        """在下标 idx 处插入新 fragment，返回 mark（结构性写入）。"""
        self._fragments.insert(idx, Fragment(mark=mark, content=content, meta=dict(meta)))
        return mark

    # ── 切片 ────────────────────────────────────────────────────────────
    # should 审视 #4：slice/slice_between/take_* 是便捷糖，文档标注边界差异。

    def slice(self, start: str, end: str) -> list[Fragment]:
        """返回首个 start 到首个 end 的片段（含边界），返回新 list。

        start/end 均为 mark。边界缺失或 start 在 end 之后时返回空。
        """
        si = self._first_idx(start)
        ei = self._first_idx(end)
        if si < 0 or ei < 0 or si > ei:
            return []
        return list(self._fragments[si : ei + 1])

    def slice_between(self, start: str, end: str) -> list[Fragment]:
        """返回首个 start 与首个 end 之间的片段（不含边界），返回新 list。"""
        si = self._first_idx(start)
        ei = self._first_idx(end)
        if si < 0 or ei < 0 or si >= ei - 1:
            return []
        return list(self._fragments[si + 1 : ei])

    def slice_prefix(self, prefix: str) -> list[Fragment]:
        """返回所有 mark 以 prefix 开头的 fragment（新 list）。"""
        return [f for f in self._fragments if f.mark.startswith(prefix)]

    def take_first(self, n: int) -> list[Fragment]:
        """返回前 n 个 fragment（新 list）。n<=0 返回空（should 审视 #3：与 take_last 对齐）。"""
        if n <= 0:
            return []
        return list(self._fragments[:n])

    def take_last(self, n: int) -> list[Fragment]:
        """返回后 n 个 fragment（新 list）。n<=0 返回空。"""
        if n <= 0:
            return []
        return list(self._fragments[-n:])

    # ── 构建 ────────────────────────────────────────────────────────────

    def build_string(self, sep: str = "\n\n") -> str:
        """把所有 fragment content 用 sep 拼接成单个字符串。"""
        return sep.join(f.content for f in self._fragments)

    def build_for_llm(self, current_agent: str | None = None) -> list[dict[str, Any]]:
        """按 meta.role 映射 role；assistant 带 name=current_agent。

        should 审视：参数名统一为 current_agent（原 agent=''），默认 None。
        role 映射：meta.role 缺省时按 'user' 处理；assistant 输出 name=meta.agent。
        """
        out: list[dict[str, Any]] = []
        for f in self._fragments:
            role = str(f.meta.get("role") or "user")
            msg: dict[str, Any] = {"role": role, "content": f.content}
            if role == "assistant":
                name = f.meta.get("agent") or current_agent
                if name:
                    msg["name"] = str(name).replace(" ", "_")
            out.append(msg)
        return out

    def build_for_groupchat(
        self,
        current_agent: str | None = None,
        *,
        agent_ranks: dict[str, int] | None = None,
        relevant_agents: set[str] | None = None,
        max_chars: int = 0,
        can_see: Callable[[int, int], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """sender-based → role-based 映射，带可见性过滤与构建期截断。

        - 用户/user → role=user
        - 系统/system → role=system
        - current_agent → role=assistant + name（current_agent 的发言）
        - 其他 agent → role=user，content 前缀 ``[sender]: ``（must 集成审视）
        - tool → role=tool（注：groupchat 实践中工具日志嵌在 agent content 内）
        - compressed_middle (is_compact_summary) → role=system

        可见性（must 集成审视）：
          - relevant_agents 非 None 时，仅保留 system/user/current_agent 及
            ∈ relevant_agents 的 agent Fragment，其余整条丢弃。
          - agent_ranks + can_see 提供时，对其他 agent 的 Fragment，若
            viewer_rank < sender_rank，对 content 执行 strip_tool_log。
        - max_chars>0 时构建期调用 truncate 做软截断。
        - 映射后强制跑 _merge_consecutive_assistant（合并连续 assistant）。
        """
        see = can_see or can_see_tool_call
        viewer_rank = 0
        if current_agent and agent_ranks:
            viewer_rank = agent_ranks.get(current_agent, 0)

        out: list[dict[str, Any]] = []
        for f in self._fragments:
            role = str(f.meta.get("role") or "user")
            sender = _sender_of(f)
            is_compact = bool(f.meta.get("is_compact_summary"))

            # 压缩摘要块 → system
            if is_compact or role == "system":
                out.append({"role": "system", "content": f.content})
                continue

            if role == "user":
                # 人类用户发言
                out.append({"role": "user", "content": f.content})
                continue

            if role == "tool":
                out.append({"role": "tool", "content": f.content, "name": sender.replace(" ", "_")})
                continue

            # assistant：区分 current_agent vs 其他 agent
            if role == "assistant":
                agent_name = str(f.meta.get("agent") or sender)
                if current_agent is not None and agent_name == current_agent:
                    out.append(
                        {
                            "role": "assistant",
                            "content": f.content,
                            "name": agent_name.replace(" ", "_"),
                        }
                    )
                    continue
                # 其他 agent —— relevant_agents 白名单过滤
                if relevant_agents is not None and agent_name not in relevant_agents:
                    continue
                # 可见性：不可见对方工具调用时 strip
                content = f.content
                if agent_ranks is not None and agent_name in agent_ranks:
                    sender_rank = agent_ranks[agent_name]
                    if not see(sender_rank, viewer_rank):
                        content = strip_tool_log(content)
                out.append(
                    {
                        "role": "user",
                        "content": f"[{agent_name}]: {content}",
                        "name": agent_name.replace(" ", "_"),
                    }
                )
                continue

            # 未知 role 兜底为 user
            out.append({"role": "user", "content": f.content})

        # 构建期软截断：仅当总量超 max_chars 时跑分层截断。不超时直接返回 out
        # （保留 role/name）。超时调 trim_llm_messages（与 truth 的 history_to_messages
        # 同函数、同 mandatory 判定：protect_index_zero + _is_human_user_llm）——
        # map→trim→merge 与 truth (message_converter:503-512) 逐字节对齐。
        # 绝不重建 Fragment：重建会把已映射 dict 塞进空 meta Fragment 再用
        # build_for_llm 重映射，导致 role 退化成 user、name 丢失（曾致 SHADOW MISMATCH）。
        if max_chars > 0:
            total = sum(len(str(m.get("content") or "")) for m in out)
            if total > max_chars:
                out, _skipped = trim_llm_messages(out, max_chars, protect_index_zero=True)

        # 合并连续 assistant（LLM API 拒绝连续同 role）
        out = _merge_consecutive_assistant(out)
        return out

    # ── 压缩与截断 ──────────────────────────────────────────────────────

    def truncate(
        self,
        max_chars: int,
        keep_marks: Sequence[str] = (),
        keep_last: int = 0,
    ) -> None:
        """best-effort 软上限截断。

        - keep_marks：保住所有同名匹配项（must 审视 #4），不受 max_chars 约束。
        - keep_last：保住最后 N 个 fragment（不受 max_chars 约束）。
        - 剩余预算内从末尾向前放入能塞下的 fragment。
        - should 审视 #7：max_chars 是包含 keep 项在内的总预算（软上限），
          keep 项优先于硬字符上限。超限时可能略超 max_chars。
        """
        if max_chars <= 0 or not self._fragments:
            return

        keep_set = set(keep_marks)
        kept: list[Fragment] = []
        used = 0

        # 先放 keep_marks（所有同名）+ is_compact_summary
        rest: list[Fragment] = []
        for f in self._fragments:
            if f.mark in keep_set or f.meta.get("is_compact_summary"):
                kept.append(f)
                used += len(f)
            else:
                rest.append(f)

        # keep_last：从 rest 末尾取 N 个（先放，优先级高）
        if keep_last > 0 and rest:
            tail = rest[-keep_last:]
            for f in tail:
                kept.append(f)
                used += len(f)
            rest = rest[: max(0, len(rest) - keep_last)]

        # 从末尾向前放能塞下的
        added: list[Fragment] = []
        for f in reversed(rest):
            if used + len(f) <= max_chars:
                added.append(f)
                used += len(f)
            else:
                break
        added.reverse()

        # 按原顺序重组
        kept_ids = {id(f) for f in kept}
        added_ids = {id(f) for f in added}
        self._fragments = [f for f in self._fragments if id(f) in kept_ids or id(f) in added_ids]

    def tiered_truncate(
        self,
        max_chars: int,
        *,
        mandatory_marks: Sequence[str] = (),
        keep_marks: Sequence[str] = (),
    ) -> None:
        """分层降级截断（复刻 fit_messages_to_tier_budget 的能力）。

        must 集成审视：简单 truncate 无分层降级，本方法补齐：
        - mandatory_marks + is_compact_summary + idx 0 永远留（mandatory）。
        - 对非 mandatory Fragment 从新到旧跑 degrade_content 0→3 试图塞进预算。
        - 仍塞不下的合并成单个 compressed_middle Fragment（机械降级）。
        """
        if max_chars <= 0 or not self._fragments:
            return

        mandatory_set = set(mandatory_marks) | set(keep_marks)
        mandatory: list[Fragment] = []
        optional: list[Fragment] = []
        for i, f in enumerate(self._fragments):
            if f.mark in mandatory_set or f.meta.get("is_compact_summary") or i == 0:
                mandatory.append(f)
            else:
                optional.append(f)

        used = sum(len(f) for f in mandatory)

        # 从新到旧尝试 degrade 0→3
        included: list[tuple[int, Fragment]] = []  # (orig_index_in_optional, frag)
        omitted_idx: set[int] = set()  # 按下标跟踪，避免 Fragment 值相等导致重复计数
        # optional 按原顺序，从新到旧 = 反向
        for idx in range(len(optional) - 1, -1, -1):
            f = optional[idx]
            placed = None
            for level in range(0, 4):
                degraded = degrade_content(f.content, level)
                if used + len(degraded) <= max_chars:
                    placed = Fragment(mark=f.mark, content=degraded, meta=dict(f.meta))
                    used += len(degraded)
                    break
            if placed is not None:
                included.append((idx, placed))
            else:
                omitted_idx.add(idx)

        if not omitted_idx:
            # 全部 optional 都塞下了，按原顺序重建
            placed_map = {idx: frag for idx, frag in included}
            self._fragments = mandatory + [
                placed_map[i] for i in range(len(optional)) if i in placed_map
            ]
            return

        # 有遗漏：合并成 compressed_middle
        # omitted 按原顺序（按下标，不靠值相等）
        omitted_ordered = [optional[i] for i in sorted(omitted_idx)]
        sources = [
            {"content": f.content, "name": str(f.meta.get("agent") or f.mark)}
            for f in omitted_ordered
        ]
        avail = max_chars - used
        compress = build_compress_message(sources, max(0, avail))
        placed_map = {idx: frag for idx, frag in included}
        new_optional = [placed_map[i] for i in range(len(optional)) if i in placed_map]
        if compress is not None:
            new_optional.append(
                Fragment(
                    mark="compressed_middle",
                    content=compress["content"],
                    meta={"role": "system", "is_compact_summary": True},
                )
            )
        self._fragments = mandatory + new_optional

    async def compress_middle(
        self,
        llm: Callable[[str], Any] | None,
        max_chars: int,
        keep_first: int = 1,
        keep_last: int = 6,
        *,
        protect_users: bool = True,
    ) -> bool:
        """AI/机械摘要中间片段。

        - ``protect_users=True``（默认，保护用户消息）：
          protected head = idx 0 + 所有 ``meta.role=="user"`` + 所有
          ``is_compact_summary`` 片段（多 pass 保护既有摘要块）。
          ``keep_first`` 在此模式下被忽略。
        - ``protect_users=False``：退化为旧语义，head = 前 ``keep_first`` 个片段
          （连续前缀，便于单元测试）。
        - ``keep_last``：protected tail = 末 ``keep_last`` 个片段（**fragment 计**）。
        - 负值抛 ValueError；``keep_last<=0`` → tail 为空。
        - asyncio.Lock 保护；snapshot 后 await llm，期间并发 append 的片段
          靠末尾重附加保留（``self._fragments[total_len:]``，对齐
          compress_middle 的 race-safety）。
        - llm 为 None 或失败时走机械降级 fallback（build_compress_message），
          绝不静默丢弃。中间内容为空时返回 False。
        - 重建：snapshot 内 protected 片段按序保留，摘要块插在首个可压缩槽，
          其余 middle 全丢弃；末尾附加 await 期间新 append 的片段。
        """
        if keep_first < 0 or keep_last < 0:
            raise ValueError("keep_first/keep_last must be non-negative")

        async with self._lock:
            snapshot = list(self._fragments)
            total_len = len(snapshot)
            if total_len == 0:
                return False

            # ── protected head ──
            protected_head: set[int] = set()
            if protect_users:
                protected_head.add(0)
                for i, f in enumerate(snapshot):
                    if str(f.meta.get("role")) == "user":
                        protected_head.add(i)
                    if f.meta.get("is_compact_summary"):
                        protected_head.add(i)
            else:
                protected_head = set(range(min(keep_first, total_len)))

            # ── protected tail ──
            protected_tail: set[int] = (
                set(range(max(0, total_len - keep_last), total_len)) if keep_last > 0 else set()
            )
            all_protected = protected_head | protected_tail

            middle = [snapshot[i] for i in range(total_len) if i not in all_protected]
            if not middle:
                return False  # 没有可压缩中间

            # 中间内容为空 → 不压缩
            if not any(f.content.strip() for f in middle):
                return False

            # age 工具日志预览（非变异：建新 Fragment，不动 snapshot 原片，
            # 与 maybe_compress 一致——AI 失败回退也不 corrupt 原数据）
            aged_middle: list[Fragment] = []
            for f in middle:
                aged = age_tool_log(f.content)
                if aged != f.content:
                    aged_middle.append(Fragment(mark=f.mark, content=aged, meta=dict(f.meta)))
                else:
                    aged_middle.append(f)
            middle = aged_middle

            history_text = "\n".join(f"[{_sender_of(f)}]: {f.content}" for f in middle)

            self._compress_active = True
            try:
                content: str | None = None
                if llm is not None:
                    try:
                        result = await llm(history_text)  # type: ignore[misc]
                        if isinstance(result, str) and result.strip():
                            content = result.strip()
                    except Exception as exc:
                        logger.warning("compress_middle llm failed, mechanical fallback: {}", exc)
                        content = None

                if content is None:
                    # 机械降级 fallback（与 maybe_compress 的 build_compress_message 对齐）
                    sources = [
                        {"content": f.content, "name": str(f.meta.get("agent") or f.mark)}
                        for f in middle
                    ]
                    compress = build_compress_message(sources, max_chars)
                    if compress is not None:
                        content = str(compress["content"])
                    else:
                        # 兜底：截断中间文本，绝不静默丢弃
                        content = history_text[:500] + "…"

                summary_frag = Fragment(
                    mark="compressed_middle",
                    content=content,
                    meta={"role": "system", "is_compact_summary": True},
                )

                # 重建：snapshot 内 protected 按序保留，摘要插在首个可压缩槽
                rebuilt: list[Fragment] = []
                inserted = False
                for i, f in enumerate(snapshot):
                    if i in all_protected:
                        rebuilt.append(f)
                    elif not inserted:
                        rebuilt.append(summary_frag)
                        inserted = True
                if not inserted:
                    rebuilt.append(summary_frag)

                # race-safety：附加 await 期间新 append 的片段（在 snapshot 之外）
                current = self._fragments
                if len(current) > total_len:
                    rebuilt.extend(current[total_len:])

                self._fragments = rebuilt
                return True
            finally:
                self._compress_active = False

    def age_tools(self, keep_recent: int = 6) -> int:
        """老化旧工具日志（缩短 content 内文本块预览，幂等）。

        - keep_recent：保留最近 N 个 fragment 不动（**fragment 计**，should 审视 #7）。
        - eligible = fragments[:max(0, total-keep_recent)]。
        - 跳过 meta.role in {system,user} 与 index 0（head 保护）。
        - _compress_active 时 no-op（must 集成审视）。
        - 操作对象是 Fragment.content 内的文本块，不是独立 tool Fragment。
        """
        if self._compress_active:
            return 0
        total = len(self._fragments)
        if total == 0:
            return 0
        eligible_end = max(0, total - keep_recent)
        if eligible_end == 0:
            return 0
        changed = 0
        for i in range(eligible_end):
            f = self._fragments[i]
            if i == 0:
                continue
            role = str(f.meta.get("role") or "")
            if role in ("system", "user"):
                continue
            if has_tool_log(f.content):
                new_content = age_tool_log(f.content)
                if new_content != f.content:
                    f.content = new_content
                    changed += 1
        return changed

    # ── 序列化 ──────────────────────────────────────────────────────────

    # ── 读取访问器（主逻辑轻量化用：engine/广播/工具不直碰 _fragments） ─────

    def last_sender(self) -> str | None:
        """末个片段的 sender 标识（agent 优先），空列表返回 None。"""
        if not self._fragments:
            return None
        return _sender_of(self._fragments[-1])

    def latest_user_content(self, *, max_len: int = 300) -> str:
        """末个真实 user 片段的 content（跳过压缩摘要），截断 max_len。无则空串。

        对齐 broadcast 旧 ``reversed(sender scan)``：role=="user" 片段即人类用户
        发言（其他 agent 在 History 里是 role=assistant，不混入）。
        """
        for f in reversed(self._fragments):
            if str(f.meta.get("role")) != "user":
                continue
            content = f.content
            if content.startswith("["):
                continue  # 跳过压缩摘要（legacy 防御）
            return content[:max_len]
        return ""

    def has_system_message(self) -> bool:
        """是否存在 role==system 片段（系统 prompt / 话题 banner / 压缩摘要）。"""
        return any(str(f.meta.get("role")) == "system" for f in self._fragments)

    def count_by_meta(self, key: str, value: Any) -> int:
        """统计 meta[key]==value 的片段数（ClearContextTool 按 agent 计数用）。"""
        return sum(1 for f in self._fragments if f.meta.get(key) == value)

    def _semantic_add_from_sender(self, sender: str, content: str) -> str:
        """sender→role 分派追加（与 from_sender_dicts 同语义）。

        - 人类 sender → role=user，mark=user_N，meta.sender=sender
        - 系统 sender → role=system，mark=system_N，meta.sender=sender
        - 其他 → role=assistant，mark=<sender>_N，meta.agent=sender（使
          ``delete_by_meta('agent', sender)`` 命中，对齐 clear_for_agent）

        engine._add_message 走此方法，sender 字符串原样保留进 meta.sender，
        使 to_sender_dicts 落盘格式与既有 chat_history.json 一致。
        """
        if _is_human_sender(sender):
            return self._semantic_append("user", content, None, role="user", sender=sender)
        if sender in ("系统", "System", "system", ""):
            return self._semantic_append(
                "system", content, None, role="system", sender=sender or "系统"
            )
        return self._semantic_append(sender, content, None, role="assistant", agent=sender)

    def format(self) -> str:
        """可读字符串：``[sender]: content`` 用空行拼接。"""
        return "\n\n".join(f"[{_sender_of(f)}]: {f.content}" for f in self._fragments)

    def to_dicts(self) -> list[dict[str, Any]]:
        """导出为 dict 列表（mark/content/meta 无损）。"""
        return [
            {"mark": f.mark, "content": f.content, "meta": dict(f.meta)} for f in self._fragments
        ]

    def to_sender_dicts(self) -> list[dict[str, Any]]:
        """兼容视图：``{sender, content, is_compact_summary}`` 列表。

        must 集成审视：engine._history 等读 list[dict] 含 sender/content，迁移期
        由本视图过渡。sender 取 meta.agent / meta.sender / role；压缩摘要标
        is_compact_summary（仅当 True 时包含）。
        """
        out: list[dict[str, Any]] = []
        for f in self._fragments:
            sender = _sender_of(f)
            item: dict[str, Any] = {"sender": sender, "content": f.content}
            if f.meta.get("is_compact_summary"):
                item["is_compact_summary"] = True
            out.append(item)
        return out

    @classmethod
    def from_dicts(cls, items: Sequence[dict[str, Any]]) -> History:
        """容错导入：缺 mark 用 append_auto('frag')，缺 content 视为空串。

        should 边界审视 #4：反序列化外部数据不应崩。如需严格模式自行校验。
        """
        ctx = cls()
        for item in items:
            mark = item.get("mark")
            content = item.get("content", "")
            meta = item.get("meta", {})
            if not isinstance(meta, dict):
                meta = {}
            if mark is None:
                mark = ctx.append_auto("frag", content if isinstance(content, str) else "", **meta)
            else:
                ctx.append(str(mark), content if isinstance(content, str) else "", **meta)
        return ctx

    @classmethod
    def from_sender_dicts(cls, items: Sequence[dict[str, Any]]) -> History:
        """从 ``{sender, content, is_compact_summary}`` 列表构建（迁移 _restore_chat_state）。

        must 集成审视：sender 映射到 meta.agent/role。sender 为用户/User/user →
        role=user；系统 → role=system；其余 → role=assistant, agent=sender。
        is_compact_summary → meta，mark=compressed_middle。
        """
        ctx = cls()
        for item in items:
            sender = str(item.get("sender") or "")
            content = item.get("content", "")
            if not isinstance(content, str):
                content = ""
            is_compact = bool(item.get("is_compact_summary"))
            if is_compact:
                ctx.append(
                    "compressed_middle",
                    content,
                    role="system",
                    is_compact_summary=True,
                    agent=sender,
                )
                continue
            if _is_human_sender(sender):
                ctx.append_auto("user", content, role="user", sender=sender)
            elif sender in ("系统", "System", "system", ""):
                ctx.append_auto("system", content, role="system", sender=sender or "系统")
            else:
                ctx.append_auto(sender, content, role="assistant", agent=sender)
        return ctx

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.to_dicts(), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> History:
        """从 JSON 字符串构建。"""
        return cls.from_dicts(json.loads(s))

    # ── 调试 ────────────────────────────────────────────────────────────

    def debug(self) -> str:
        """每行: idx | mark | len | content_preview。"""
        lines: list[str] = []
        for i, f in enumerate(self._fragments):
            preview = f.content[:40].replace("\n", " ")
            lines.append(f"{i:3d} | {f.mark:<20} | {len(f):5d} | {preview}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"History({len(self)} fragments, {self.total_chars()} chars)"
