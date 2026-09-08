#!/usr/bin/env python3
"""Offline economics analyzer: history compression vs prompt cache (plan.md 批次 C2.1).

Quantifies whether ``HistoryContext`` view compression pays for itself, using
only already-existing offline data — this is a **read-only measurement**
batch ("只测不改"): the script never writes to ~/.nanobot and never touches the
gateway.

Data sources
------------
1. ``~/.nanobot/request_logs/YYYY-MM-DD.jsonl`` — full LLM request log.
   Compression summary calls are identified by the heuristic
   ``msg_count == 1`` + the summary-prompt marker ``中期历史记录（共 N 条）``
   (context.py ``_compress_view`` prompt, stable since 2026-04-27, git
   b677a0101).  Entries written after C0 (``83b4b555a``) may additionally
   carry ``mode == "history_compress"``, real ``usage.cache_tokens`` and
   ``cost`` — the script prefers those when present and reports how many
   entries actually have them (the production gateway had not been restarted
   at analysis time, so they can be zero; the report must say so).
2. ``<logs>/gateway.log`` (+ rotations ``gateway.log.1`` … ``gateway.log.N.gz``)
   — ``HistoryContext: compressed N → summary`` lines give compression
   event counts independent of request_logs.
3. ``~/.nanobot/history_settings.json`` — the *current* regime, for context.
   Historical regimes are NOT known (the file only holds the present values),
   so report segments are inferred from data features and labelled against
   candidate regimes with explicit confidence + evidence.

Method (all monetary numbers are estimates — see the assumptions box)
---------------------------------------------------------------------
* Cost side: per summary call, input/output tokens are real data; price comes
  from an ASSUMED table (override with ``--prices <json>``).  When the C0
  ``cost`` field exists it is used verbatim.
* Cache side (historical, pre-C0): no ``cache_tokens`` exists, so two proxies
  are used and labelled as such — (a) the ``cache_probe.x-cache-ratio-pct``
  estimate on the calls immediately before each compression, and (b) the
  prompt_tokens drop across the compression boundary.
* Net benefit per event:
    savings   = (M - S) tokens × C subsequent calls     [M = summary prompt
               tokens ≈ middle size, S = summary completion tokens, C = calls
               until the prompt regrows to its pre-compression level]
    cost      = the summary call itself
    cache loss= cacheable-prefix tokens × (1 - discount) × input price, i.e.
               the next call re-reads the rewritten prefix at full price
               instead of the discounted cache-read price.  Assumption: the
               rewrite point sits early in the history (after the small
               protected head), so essentially the whole cacheable prefix is
               invalidated.  Discount default 0.1 (Anthropic-style cache-read
               price ≈ 10% of input; DeepSeek similar).
  Savings is valued twice — at full input price (upper bound) and at the
  discounted cache-read price (lower bound) — because pre-compression those
  tokens were largely cache hits; a conclusion is only reported as robust
  when both bounds agree in sign.

Usage::

    python3 scripts/analyze_compression_cache.py                 # human report
    python3 scripts/analyze_compression_cache.py --json out.json # + machine copy
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# ── Markers / regexes ──────────────────────────────────────────────────────

#: Summary prompt marker written by context.py::_compress_view.  The exact
#: wording has been stable since 2026-04-27 (git b677a0101 → today):
#: "以下是群聊的一段中期历史记录（共 {n} 条）。"  Accept ASCII parens too so a
#: future prompt tweak only breaks one of the two anchors.
MARKER_RE = re.compile(r"中期历史记录[（(]共\s*(\d+)\s*条[）)]")

#: loguru line: "2026-09-08 00:27:49.747 | INFO | ...:_compress_view:528 - HistoryContext: compressed 44 → summary"
GATEWAY_COMPRESSED_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s*\|.*?compressed (\d+)\s*→\s*summary"
)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Assumed prices (USD per 1M tokens, (input, output)) ────────────────────
# These are ASSUMPTIONS, not measurements: several models below have no
# published 2026 price this script could verify offline.  Every dollar figure
# in the report is derived from this table — override with ``--prices file``
# (JSON: {"model-substring": [in, out], "*": [in, out]}) and treat direction,
# not magnitude, as the finding.  Token counts next to every dollar figure
# are real data.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1-nano": (0.10, 0.40),       # OpenAI published list price
    "glm-5-turbo": (0.30, 1.20),
    "glm-5": (0.60, 2.20),
    "glm-4.7": (0.60, 2.20),
    "hy4-preview": (0.50, 2.00),
    "kimi-k2": (0.60, 2.50),
    "minimax-m2": (0.30, 1.20),
    "deepseek-v4-pro": (0.28, 0.42),
    "deepseek-v4-flash": (0.07, 0.28),
    "deepseek-v3": (0.28, 0.42),
    "claude-sonnet": (3.00, 15.00),     # Anthropic list price
    "openrouter/free": (0.0, 0.0),
    "*": (0.50, 1.50),                  # fallback for unrecognised models
}

#: Fraction of the input price charged for a cache READ (Anthropic ~0.1,
#: DeepSeek automatic context caching ~0.1-0.15).  CLI: --cache-discount.
DEFAULT_CACHE_DISCOUNT = 0.10

# ── Candidate regimes for labelling (annotation only — segments themselves
#    are detected from data; see infer_segments) ────────────────────────────
# n_band = expected range of the middle-message count N on the summary prompt
# "共 N 条", derived from  N ≈ view_len - protected_head - keep_recent  with
# view_len ≥ max_messages × compress_ratio.
REGIME_CANDIDATES: list[dict[str, Any]] = [
    {
        "id": "R50/0.8/6",
        "params": "max_messages=50, compress_ratio=0.8 → 阈值40, keep_recent=6",
        "source": "仓库默认 ≤2026-05-21 (git 6f6e8357f 的父代)",
        "n_band": (15, 48),
    },
    {
        "id": "R200/0.8/20",
        "params": "max_messages=200, compress_ratio=0.8 → 阈值160, keep_recent=20",
        "source": "仓库默认 2026-05-21 → 今 (git 6f6e8357f)",
        "n_band": (100, 200),
    },
    {
        "id": "Rlocal30/0.7/20",
        "params": "max_messages=30, compress_ratio=0.7 → 阈值21, keep_recent=20",
        "source": "本机 ~/.nanobot/history_settings.json (mtime 2026-08-06; 更早取值未知)",
        "n_band": (1, 60),
        "note": "阈值21 与尾部保护20 几乎重合: 视图有界时每次可压中段≈0-1 条(近乎 no-op);"
                "视图无界(网关未重启到 C1.1 前)时每轮触发, N≈本轮新增消息数, 呈高频小N特征",
    },
]


def price_for(model: str | None, prices: dict[str, tuple[float, float]] | None = None) -> tuple[float, float]:
    """Return (input, output) USD per 1M tokens for a model string.

    Matches by substring after stripping routing prefixes, so
    ``openrouter/z-ai/glm-5.1`` and ``z-ai/glm-5.1`` resolve the same.
    Longest matching key wins; ``*`` is the fallback.
    """
    table = prices or DEFAULT_PRICES
    m = (model or "").lower()
    for prefix in ("openrouter/", "openai/", "anthropic/", "z-ai/", "tencent/", "moonshotai/", "minimax/", "deepseek/"):
        m = m.replace(prefix, "")
    best: tuple[int, tuple[float, float]] | None = None
    for key, val in table.items():
        if key == "*":
            continue
        if key in m and (best is None or len(key) > best[0]):
            best = (len(key), val)
    if best:
        return best[1]
    return table.get("*", (0.5, 1.5))


# ── Slim records (memory: 122 days ≈ 64k entries; raw entries with full
#    message bodies do not fit in memory — project immediately) ─────────────


@dataclass
class Entry:
    ts: str
    dt: datetime
    model: str | None
    mode: str | None
    status: str
    msg_count: int
    prompt_tokens: int
    completion_tokens: int
    cache_ratio_pct: int | None      # cache_probe.x-cache-ratio-pct (estimate)
    cacheable_chars: int | None      # cache_probe.x-cache-cacheable-chars
    cost: float | None               # C0 field; absent on pre-C0 entries
    cache_tokens: int | None         # C0 field; absent on pre-C0 entries
    middle_count: int | None = None  # marker "共 N 条" when present


@dataclass
class GatewayEvent:
    dt: datetime
    middle_count: int
    source: str  # file the line came from


@dataclass
class Economics:
    """Per-event economics; all USD figures are estimates unless noted."""
    m_tokens: int                    # summary-call prompt tokens (≈ middle size)
    s_tokens: int                    # summary completion tokens
    calls_in_window: int             # subsequent calls until prompt regrew
    pre_prompt_med: int | None       # median prompt tokens just before
    post_prompt_med: int | None      # median prompt tokens just after
    observed_drop: int | None        # pre - post (PROXY; confounded by other trimming)
    cacheable_prefix_tokens: int | None  # from cache_probe (estimate) or None
    prefix_source: str               # "cache_probe" | "fallback" | "none"
    summary_cost: float              # USD (real C0 cost when available)
    summary_cost_measured: bool
    savings_tokens: int              # C × (M - S), signed (negative = bloat)
    value_saved_full: float          # USD at full input price (upper bound)
    value_saved_cached: float        # USD at cache-read price (lower bound)
    cache_loss: float                # USD
    window_model: str | None         # dominant model of the paying stream


@dataclass
class Segment:
    start: datetime
    end: datetime
    events: list[Entry] = field(default_factory=list)
    economics: list[Economics] = field(default_factory=list)
    label: str = ""
    confidence: str = ""
    evidence: str = ""


# ── Parsing ────────────────────────────────────────────────────────────────


def slim_entry(raw: dict[str, Any]) -> Entry | None:
    """Project one raw jsonl record to a slim Entry (drops message bodies)."""
    ts = raw.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.strptime(str(ts)[:19], TS_FORMAT)
    except ValueError:
        return None
    usage = raw.get("usage") or {}
    probe = raw.get("cache_probe") or {}
    msgs = raw.get("messages") or []
    middle = None
    if raw.get("msg_count") == 1 and msgs:
        content = msgs[0].get("content") or ""
        if isinstance(content, str):
            m = MARKER_RE.search(content)
            if m:
                middle = int(m.group(1))
    return Entry(
        ts=str(ts),
        dt=dt,
        model=raw.get("model"),
        mode=raw.get("mode"),
        status=str(raw.get("status") or "?"),
        msg_count=int(raw.get("msg_count") or 0),
        prompt_tokens=int(usage.get("prompt") or 0),
        completion_tokens=int(usage.get("completion") or 0),
        cache_ratio_pct=probe.get("x-cache-ratio-pct"),
        cacheable_chars=probe.get("x-cache-cacheable-chars"),
        cost=raw.get("cost"),
        cache_tokens=(usage.get("cache_tokens") if isinstance(usage, dict) else None),
        middle_count=middle,
    )


def iter_request_log_entries(paths: Iterable[Path]) -> Iterable[Entry]:
    """Stream slim entries from request-log jsonl files (bad lines skipped)."""
    for path in sorted(paths):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    entry = slim_entry(raw)
                    if entry is not None:
                        yield entry
        except OSError:
            continue


def parse_gateway_lines(lines: Iterable[str], source: str = "gateway.log") -> list[GatewayEvent]:
    """Extract ``compressed N → summary`` events from loguru text lines."""
    out: list[GatewayEvent] = []
    for line in lines:
        m = GATEWAY_COMPRESSED_RE.match(line)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1), TS_FORMAT)
        except ValueError:
            continue
        out.append(GatewayEvent(dt=dt, middle_count=int(m.group(2)), source=source))
    return out


def load_gateway_events(logs_dir: Path, log_name: str = "gateway.log") -> list[GatewayEvent]:
    """Load compression lines from the live log plus every rotation (.1, .N.gz)."""
    events: list[GatewayEvent] = []
    if not logs_dir.is_dir():
        return events
    candidates = sorted(logs_dir.glob(f"{log_name}*"), reverse=True)
    for path in candidates:
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                    events.extend(parse_gateway_lines(f, source=path.name))
            else:
                with open(path, encoding="utf-8", errors="replace") as f:
                    events.extend(parse_gateway_lines(f, source=path.name))
        except OSError:
            continue
    events.sort(key=lambda e: e.dt)
    return events


def load_current_regime(settings_path: Path) -> dict[str, Any] | None:
    """Read the *current* local regime from history_settings.json (context only)."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    hist = data.get("history") or {}
    tool = data.get("tool_results") or {}
    max_messages = hist.get("max_messages")
    ratio = hist.get("compress_ratio")
    # Absent key falls back to the runtime default (history_settings.py: 20).
    keep = hist.get("compression_keep_recent")
    if keep is None:
        keep = 20
    regime: dict[str, Any] = {
        "max_messages": max_messages,
        "compress_ratio": ratio,
        "compression_keep_recent": keep,
        "compress_max_summary_tokens": hist.get("compress_max_summary_tokens"),
        "summarize_model": hist.get("summarize_model") or tool.get("summarize_model"),
        "settings_mtime": datetime.fromtimestamp(settings_path.stat().st_mtime),
    }
    if isinstance(max_messages, (int, float)) and isinstance(ratio, (int, float)):
        threshold = int(max_messages * ratio)
        regime["threshold"] = threshold
        if isinstance(keep, int):
            # With bounded views the compressible middle is what remains after
            # head + tail protection once the trigger fires.
            regime["compressible_middle_typical"] = max(0, threshold - keep - 1)
    return regime


# ── Segmentation (pure; data-driven change-point detection) ────────────────


def _median(vals: list[int]) -> float:
    return statistics.median(vals) if vals else 0.0


def infer_segments(
    events: list[Entry],
    *,
    quiet_gap_days: int = 14,
    min_events: int = 3,
    median_shift: int = 8,
    window: int = 8,
    max_splits: int = 8,
) -> list[Segment]:
    """Split compression events into regimes inferred from the data itself.

    Two signals, in priority order:
      1. **quiet gap** — a pause ≥ *quiet_gap_days* separates usage periods
         (deployment gaps), each becoming its own segment;
      2. **median-N shift** — inside a block, a two-window scan splits where
         the median middle-message count moves by ≥ *median_shift* with at
         least *min_events* on each side (trigger-threshold or keep-recent
         changes move exactly this statistic).
    Splits are found greedily (largest shift first) up to *max_splits*.
    """
    evs = sorted(events, key=lambda e: e.dt)
    if not evs:
        return []

    # 1. quiet-gap blocks
    blocks: list[list[Entry]] = [[evs[0]]]
    for prev, cur in zip(evs, evs[1:]):
        if (cur.dt - prev.dt) >= timedelta(days=quiet_gap_days):
            blocks.append([cur])
        else:
            blocks[-1].append(cur)

    # 2. median-shift splits within each block (greedy recursive bisection)
    segments: list[Segment] = []
    for block in blocks:
        queue = [block]
        splits = 0
        while queue:
            chunk = queue.pop(0)
            if len(chunk) < 2 * min_events or splits >= max_splits:
                segments.append(_make_segment(chunk))
                continue
            best: tuple[float, int] | None = None  # (shift, index)
            step = max(1, len(chunk) // 60)
            for i in range(min_events, len(chunk) - min_events + 1, step):
                left = [e.middle_count or 0 for e in chunk[max(0, i - window): i]]
                right = [e.middle_count or 0 for e in chunk[i: i + window]]
                if not left or not right:
                    continue
                shift = abs(_median(right) - _median(left))
                if shift >= median_shift and (best is None or shift > best[0]):
                    best = (shift, i)
            if best is None:
                segments.append(_make_segment(chunk))
                continue
            idx = best[1]
            queue.insert(0, chunk[idx:])
            queue.insert(0, chunk[:idx])
            splits += 1
        segments.sort(key=lambda s: s.start)
    return segments


def _make_segment(chunk: list[Entry]) -> Segment:
    return Segment(start=chunk[0].dt, end=chunk[-1].dt, events=chunk)


def label_segment(seg: Segment, current_regime: dict[str, Any] | None = None) -> None:
    """Attach a candidate-regime label + confidence + evidence to a segment.

    Purely derived from the segment's own statistics; the candidate table and
    the current-regime anchor are annotation inputs, not ground truth.
    """
    ns = [e.middle_count or 0 for e in seg.events]
    n_med = _median(ns)
    days = max((seg.end - seg.start).days, 1)
    active_days = len({e.dt.date() for e in seg.events})
    rate = len(seg.events) / max(active_days, 1)

    matches = [
        c for c in REGIME_CANDIDATES
        if c["n_band"][0] <= n_med <= c["n_band"][1]
    ]
    evidence = (
        f"N(每次压缩的中段条数) 中位={n_med:g} 范围=[{min(ns)}, {max(ns)}], "
        f"{len(seg.events)} 次 / {active_days} 个活跃日 (≈{rate:.1f} 次/活跃日)"
    )

    if len(seg.events) < 3:
        seg.label = "样本不足"
        seg.confidence = "low"
    elif len(matches) == 1:
        seg.label = f"疑似 {matches[0]['id']}"
        seg.confidence = "medium"
        evidence += f"; N 中位只落入候选 {matches[0]['id']} 的预期带 {matches[0]['n_band']}"
    elif len(matches) > 1:
        # Band overlap (Rlocal band covers most of R50's) is resolved with the
        # trigger-rate signal: a high threshold (50/200 defaults) fires rarely,
        # while a threshold ≈ keep_recent + view growth fires almost every
        # round.  Mid rates (2-5/active day) are consistent with both and stay
        # ambiguous.
        has_local = any(c["id"] == "Rlocal30/0.7/20" for c in matches)
        non_local = [c for c in matches if c["id"] != "Rlocal30/0.7/20"]
        if has_local and rate >= 5:
            seg.label = "疑似 Rlocal30/0.7/20 (高频触发特征)"
            seg.confidence = "medium"
            evidence += "; 高频(≥5次/活跃日)是 阈值≈尾部保护/视图无界 regime 的特征"
        elif rate < 2 and len(non_local) == 1:
            seg.label = f"疑似 {non_local[0]['id']} (低频触发特征)"
            seg.confidence = "medium"
            evidence += f"; 低频(<2次/活跃日)指向高阈值 regime; N 中位同时落入的候选带: " \
                        f"{' / '.join(c['id'] for c in matches)}"
        else:
            ids = " / ".join(c["id"] for c in matches)
            seg.label = f"歧义: {ids}"
            seg.confidence = "low"
            evidence += f"; N 中位同时落入多个候选带 ({ids}) 且触发频率({rate:.1f}/活跃日)不足以消歧"
    else:
        seg.label = "未匹配到已知候选 (本机历史取值可能不在 git 默认轨迹上)"
        seg.confidence = "low"
        evidence += "; N 中位不在任何候选带内"

    if current_regime and current_regime.get("settings_mtime"):
        mtime: datetime = current_regime["settings_mtime"]
        if seg.start >= mtime:
            evidence += f"; 段起点晚于本机 settings mtime {mtime:%Y-%m-%d} (当前 regime 锚点)"
    seg.evidence = evidence


# ── Per-event economics ────────────────────────────────────────────────────


def _dominant_model(entries: list[Entry]) -> str | None:
    counts: dict[str, int] = {}
    for e in entries:
        if e.model:
            counts[e.model] = counts.get(e.model, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _median_of(vals: list[int | None]) -> int | None:
    real = [v for v in vals if v]
    return int(_median(real)) if real else None


def event_economics(
    event: Entry,
    stream: list[Entry],
    stream_dts: list[datetime],
    next_event_dt: datetime | None,
    *,
    prices: dict[str, tuple[float, float]] | None = None,
    cache_discount: float = DEFAULT_CACHE_DISCOUNT,
    horizon: timedelta = timedelta(hours=24),
    pre_post_k: int = 3,
) -> Economics:
    """Compute one compression event's economics against the paying stream.

    *stream* is the sorted list of ok broadcast-mode entries (the calls whose
    prompts contain the compressed views).  Memory-safe: uses bisect on
    precomputed datetimes.
    """
    i = bisect_left(stream_dts, event.dt)
    pre = stream[max(0, i - pre_post_k): i]
    post = stream[i: i + pre_post_k]

    pre_med = _median_of([e.prompt_tokens for e in pre])
    post_med = _median_of([e.prompt_tokens for e in post])
    observed_drop = (pre_med - post_med) if (pre_med and post_med) else None

    m_tok = event.prompt_tokens
    s_tok = event.completion_tokens
    p_in, p_out = price_for(event.model, prices)

    # Real C0 cost when present, else token-based estimate.
    measured = event.cost is not None
    summary_cost = float(event.cost) if measured else (m_tok * p_in + s_tok * p_out) / 1e6

    # Regrowth window: subsequent calls until prompt_tokens regain the
    # pre-event median (or the next compression event / horizon, whichever
    # comes first).
    limit = min(
        [d for d in [next_event_dt, event.dt + horizon] if d is not None],
        default=event.dt + horizon,
    )
    j = i
    calls = 0
    window_entries: list[Entry] = []
    while j < len(stream_dts) and stream_dts[j] < limit and calls < 1000:
        pt = stream[j].prompt_tokens
        window_entries.append(stream[j])
        calls += 1
        if pre_med and pt >= pre_med:
            break
        j += 1

    # Cacheable prefix right before the event, from cache_probe when present.
    probe_pre = [e for e in pre if e.cacheable_chars]
    if probe_pre:
        prefix_tokens = max(e.cacheable_chars or 0 for e in probe_pre) // 4
        prefix_source = "cache_probe"
    elif pre_med:
        prefix_tokens = pre_med
        prefix_source = "fallback(pre prompt_tokens, 无 probe)"
    else:
        prefix_tokens = None
        prefix_source = "none"

    window_model = _dominant_model(window_entries) or (_dominant_model(pre))
    w_in, _ = price_for(window_model, prices)

    savings_tokens = calls * (m_tok - s_tok)  # signed; negative = summary bigger than middle
    value_saved_full = savings_tokens * w_in / 1e6
    value_saved_cached = savings_tokens * w_in * cache_discount / 1e6
    cache_loss = (
        prefix_tokens * w_in * (1 - cache_discount) / 1e6
        if prefix_tokens
        else 0.0
    )

    return Economics(
        m_tokens=m_tok,
        s_tokens=s_tok,
        calls_in_window=calls,
        pre_prompt_med=pre_med,
        post_prompt_med=post_med,
        observed_drop=observed_drop,
        cacheable_prefix_tokens=prefix_tokens,
        prefix_source=prefix_source,
        summary_cost=summary_cost,
        summary_cost_measured=measured,
        savings_tokens=savings_tokens,
        value_saved_full=value_saved_full,
        value_saved_cached=value_saved_cached,
        cache_loss=cache_loss,
        window_model=window_model,
    )


# ── Aggregation + report ───────────────────────────────────────────────────


def _fmt_usd(v: float) -> str:
    return f"${v:,.3f}"


def aggregate_segment(seg: Segment, *, cache_discount: float = DEFAULT_CACHE_DISCOUNT) -> dict[str, Any]:
    eco = seg.economics
    ns = [e.middle_count or 0 for e in seg.events]
    out = {
        "start": seg.start.strftime("%Y-%m-%d"),
        "end": seg.end.strftime("%Y-%m-%d"),
        "label": seg.label,
        "confidence": seg.confidence,
        "evidence": seg.evidence,
        "events": len(seg.events),
        "n_median": _median(ns),
        "n_min": min(ns) if ns else None,
        "n_max": max(ns) if ns else None,
        "m_tokens_sum": sum(e.m_tokens for e in eco),
        "s_tokens_sum": sum(e.s_tokens for e in eco),
        "bloat_events": sum(1 for e in eco if e.s_tokens > e.m_tokens),
        "summary_cost_usd": sum(e.summary_cost for e in eco),
        "measured_cost_events": sum(1 for e in eco if e.summary_cost_measured),
        "savings_tokens_sum": sum(e.savings_tokens for e in eco),
        "value_saved_full_usd": sum(e.value_saved_full for e in eco),
        "value_saved_cached_usd": sum(e.value_saved_cached for e in eco),
        "cache_loss_usd": sum(e.cache_loss for e in eco),
        "observed_drop_median": _median_of([e.observed_drop for e in eco]),
        "cacheable_prefix_median": _median_of([e.cacheable_prefix_tokens for e in eco]),
        "calls_in_window_median": _median_of([e.calls_in_window for e in eco]),
    }
    out["net_upper_usd"] = out["value_saved_full_usd"] - out["summary_cost_usd"] - out["cache_loss_usd"]
    out["net_lower_usd"] = out["value_saved_cached_usd"] - out["summary_cost_usd"] - out["cache_loss_usd"]
    if out["cache_loss_usd"] > 0:
        # Discount d* at which net(upper) crosses zero:
        #   net(d) = A - L_full × (1-d)   →   d* = 1 - A/L_full
        # cache_loss was valued at *cache_discount*, so undo it to get L_full.
        a = out["value_saved_full_usd"] - out["summary_cost_usd"]
        l_full = out["cache_loss_usd"] / max(1e-9, (1 - cache_discount))
        d_star = 1 - a / l_full if l_full else None
        out["breakeven_cache_discount"] = round(d_star, 3) if d_star is not None and 0 <= d_star <= 1 else None
    else:
        out["breakeven_cache_discount"] = None
    return out


def run_analysis(
    request_log_paths: Iterable[Path],
    gateway_events: list[GatewayEvent],
    current_regime: dict[str, Any] | None,
    *,
    prices: dict[str, tuple[float, float]] | None = None,
    cache_discount: float = DEFAULT_CACHE_DISCOUNT,
) -> dict[str, Any]:
    """Full analysis → dict suitable for both the human report and --json."""
    entries = sorted(iter_request_log_entries(request_log_paths), key=lambda e: e.dt)
    ok_entries = [e for e in entries if e.status == "ok"]
    stream = [e for e in ok_entries if e.mode == "broadcast" and e.prompt_tokens]
    stream_dts = [e.dt for e in stream]

    # C0 real-field coverage (may legitimately be zero until gateway restart).
    with_cache_field = [e for e in ok_entries if e.cache_tokens is not None]
    with_cache_hits = [e for e in with_cache_field if (e.cache_tokens or 0) > 0]
    with_cost = [e for e in ok_entries if e.cost is not None]

    # Compression events: prefer C0 metadata, else the marker heuristic.
    by_mode = [e for e in ok_entries if e.mode == "history_compress"]
    by_marker = [e for e in ok_entries if e.middle_count is not None]
    event_ids = {id(e) for e in by_mode}
    events = list(by_mode) + [e for e in by_marker if id(e) not in event_ids]
    events.sort(key=lambda e: e.dt)

    # Candidates that look like summary calls but lack the marker (unconfirmed).
    sum_models = {e.model for e in events if e.model}
    unconfirmed = [
        e for e in ok_entries
        if e.middle_count is None and e.mode != "history_compress"
        and e.msg_count == 1 and e.model in sum_models
        and e.prompt_tokens > 2000
    ]

    segments = infer_segments(events)
    for seg in segments:
        label_segment(seg, current_regime)
        seg_events = seg.events
        for idx, ev in enumerate(seg_events):
            nxt = seg_events[idx + 1].dt if idx + 1 < len(seg_events) else None
            seg.economics.append(
                event_economics(
                    ev, stream, stream_dts, nxt,
                    prices=prices, cache_discount=cache_discount,
                )
            )
    seg_rows = [aggregate_segment(s, cache_discount=cache_discount) for s in segments]

    # Gateway-log cross-check: per-day lines vs request-log attributed calls.
    gw_by_day: dict[str, int] = {}
    for g in gateway_events:
        key = g.dt.strftime("%Y-%m-%d")
        gw_by_day[key] = gw_by_day.get(key, 0) + 1
    rl_by_day: dict[str, int] = {}
    for e in events:
        key = e.dt.strftime("%Y-%m-%d")
        rl_by_day[key] = rl_by_day.get(key, 0) + 1
    overlap_days = sorted(set(gw_by_day) & set(rl_by_day))
    mismatches = [
        {"day": d, "gateway_lines": gw_by_day.get(d, 0), "request_log_calls": rl_by_day.get(d, 0)}
        for d in sorted(set(gw_by_day) | set(rl_by_day))
        if gw_by_day.get(d, 0) != rl_by_day.get(d, 0)
    ]

    total = {
        "entries_total": len(entries),
        "entries_ok": len(ok_entries),
        "broadcast_stream": len(stream),
        "window": (
            f"{entries[0].dt:%Y-%m-%d} → {entries[-1].dt:%Y-%m-%d}" if entries else "n/a"
        ),
        "c0_fields": {
            "entries_with_cache_tokens": len(with_cache_field),
            "entries_with_cache_hits": len(with_cache_hits),
            "entries_with_cost": len(with_cost),
        },
        "events": {
            "attributed": len(events),
            "via_c0_mode": len(by_mode),
            "via_marker_heuristic": len([e for e in by_marker if id(e) not in {id(x) for x in by_mode}]),
            "unconfirmed_candidates_excluded": len(unconfirmed),
        },
        "gateway_lines_total": len(gateway_events),
        "gateway_window": (
            f"{gateway_events[0].dt:%Y-%m-%d} → {gateway_events[-1].dt:%Y-%m-%d}"
            if gateway_events else "n/a"
        ),
        "gateway_overlap_days": len(overlap_days),
        "gateway_mismatches": mismatches,
        "segments": seg_rows,
        "assumptions": {
            "prices": "ASSUMED table (see --prices to override); token counts are real data",
            "cache_discount": cache_discount,
            "cache_loss_model": "rewrite invalidates essentially the whole cacheable prefix; "
                                "next call re-reads it at full price (one-time)",
            "savings_model": "per-call savings = M - S (summary prompt ≈ middle size, S = summary "
                             "tokens kept); counted over calls until prompt regrows (≤24h / next event)",
            "proxies": [
                "cache side pre-C0: cache_probe ratio (char-based estimate) + prompt_tokens drop",
                "observed prompt drop is confounded by same-round tool-result trimming",
            ],
        },
    }
    return total


# ── Rendering ──────────────────────────────────────────────────────────────


def render_report(analysis: dict[str, Any], current_regime: dict[str, Any] | None) -> str:
    L: list[str] = []
    ap = L.append
    ap("━" * 74)
    ap("历史压缩 vs Prompt 缓存 经济性离线分析 (plan.md 批次 C2.1, 只测不改)")
    ap("━" * 74)
    tot = analysis
    ap(f"数据窗口(request_logs): {tot['window']}   条目: {tot['entries_total']} (ok {tot['entries_ok']}, "
       f"broadcast 流 {tot['broadcast_stream']})")
    ap(f"gateway.log 压缩行: {tot['gateway_lines_total']} ({tot['gateway_window']}, 含轮转; "
       f"与 request_logs 重叠 {tot['gateway_overlap_days']} 天)")

    if current_regime:
        ap(f"当前本机 regime: max_messages={current_regime.get('max_messages')}, "
           f"compress_ratio={current_regime.get('compress_ratio')} "
           f"(阈值 {current_regime.get('threshold')}), "
           f"keep_recent={current_regime.get('compression_keep_recent')}, "
           f"summarize_model={current_regime.get('summarize_model')}")
        mid = current_regime.get("compressible_middle_typical")
        if mid is not None:
            ap(f"  → 视图有界时每次可压中段 ≈{mid} 条 (阈值-尾部保护-首条), 压缩近乎 no-op;"
               f" 视图无界(网关未重启到 C1.1 前)时每轮触发、N≈本轮新增")
    else:
        ap("当前本机 regime: 未读取到 ~/.nanobot/history_settings.json")

    # C0 coverage
    c0 = tot["c0_fields"]
    ap("")
    ap("── C0 真实字段覆盖 ──")
    if c0["entries_with_cache_tokens"] == 0 and c0["entries_with_cost"] == 0:
        ap("0 条带 usage.cache_tokens / cost 的条目: 生产网关尚未重启到 C0 (83b4b555a) 代码。")
        ap("真实 cache/cost 数据自网关下次重启起积累; 本报告全部成本/缓存数字均为启发式估计。")
    else:
        ap(f"usage.cache_tokens: {c0['entries_with_cache_tokens']} 条 (其中命中>0: {c0['entries_with_cache_hits']})")
        ap(f"cost 字段: {c0['entries_with_cost']} 条")
        if c0["entries_with_cache_hits"] == 0 and c0["entries_with_cost"] == 0:
            ap("字段已出现但命中全为 0、无 cost: 生产网关尚未重启到 C0 (83b4b555a) 代码,")
            ap("带字段条目来自实验/测试进程。真实 cache/cost 数据自网关下次重启起积累,")
            ap("本报告全部成本/缓存数字仍为启发式估计。")

    ev = tot["events"]
    ap("")
    ap("── 压缩事件归因 ──")
    ap(f"request_logs 归因的摘要调用: {ev['attributed']} "
       f"(C0 mode=history_compress: {ev['via_c0_mode']}; "
       f"marker 启发式: {ev['via_marker_heuristic']})")
    ap(f"疑似但未确认(模型匹配+单消息, 无 marker)而排除: {ev['unconfirmed_candidates_excluded']}")
    ap(f"gateway.log 压缩行(含轮转): {tot['gateway_lines_total']}")
    if tot["gateway_mismatches"]:
        worst = max(tot["gateway_mismatches"], key=lambda m: abs(m["gateway_lines"] - m["request_log_calls"]))
        ap(f"两源不一致的天数: {len(tot['gateway_mismatches'])}; 最大差异日 {worst['day']}: "
           f"gateway {worst['gateway_lines']} 行 vs request_logs {worst['request_log_calls']} 次调用")
        ap("  (不一致的可能来源: 实验/测试进程复用 gateway.log 但走假 provider、或未落日志的调用 — 见结论栏)")

    # Segments
    ap("")
    ap("── 分段 (按 settings regime, 数据特征推断) ──")
    if not tot["segments"]:
        ap("无压缩事件可分段。")
    else:
        ap(f"{'段':<4}{'日期范围':<24}{'次数':>5}{'N中位':>7}{'N范围':>12}  标签 [置信度]")
        for i, s in enumerate(tot["segments"], 1):
            nrange = f"[{s['n_min']},{s['n_max']}]"
            ap(f"S{i:<3}{s['start']} → {s['end']:<11}{s['events']:>5}{s['n_median']:>7g}{nrange:>12}  "
               f"{s['label']} [{s['confidence']}]")
        ap("推断依据(每段):")
        for i, s in enumerate(tot["segments"], 1):
            ap(f"  S{i}: {s['evidence']}")

    # Cost side
    ap("")
    ap("── 成本侧 (摘要调用自身) ──")
    for i, s in enumerate(tot["segments"], 1):
        ap(f"S{i}: Σ输入 {s['m_tokens_sum']:,} tok, Σ输出 {s['s_tokens_sum']:,} tok, "
           f"费用≈{_fmt_usd(s['summary_cost_usd'])} "
           f"(实测 cost 条目 {s['measured_cost_events']}/{s['events']})"
           if s["events"] else f"S{i}: 无事件")
    ap("注: 历史条目无 cost 字段 → 按真实 token 数 × 假设价格表估计 (置信度: token 数=实测, 价格=假设)。")

    # Cache side
    ap("")
    ap("── cache 侧 (代理指标, pre-C0 无真实 cache_tokens) ──")
    for i, s in enumerate(tot["segments"], 1):
        if not s["events"]:
            continue
        ap(f"S{i}: 事件前 cacheable 前缀中位 ≈{s['cacheable_prefix_median'] or 0:,} tok "
           f"(来源 cache_probe, 字符/4 估计); 观测 prompt_tokens 跨压缩跳降中位 "
           f"{s['observed_drop_median'] or 0:,} tok (代理: 混入同轮工具结果剪枝, 高估压缩贡献)")
    if c0["entries_with_cache_hits"]:
        ap(f"C0 真实命中: {c0['entries_with_cache_hits']} 条 >0 — 详见 --json。")

    # Net benefit
    ap("")
    ap("── 净收益 (压缩节省 vs 摘要费用+缓存前缀失效) ──")
    ap(f"{'段':<4}{'节省tok':>12}{'节省$(全价)':>13}{'节省$(缓存价)':>14}{'摘要费':>10}{'缓存损失':>10}{'净(上界)':>11}{'净(下界)':>11}")
    for i, s in enumerate(tot["segments"], 1):
        ap(f"S{i:<3}{s['savings_tokens_sum']:>12,}{s['value_saved_full_usd']:>13,.3f}"
           f"{s['value_saved_cached_usd']:>14,.3f}{s['summary_cost_usd']:>10,.3f}"
           f"{s['cache_loss_usd']:>10,.3f}{s['net_upper_usd']:>11,.3f}{s['net_lower_usd']:>11,.3f}")
    ap("方向判定(上界=节省按全价计, 下界=按缓存读价计; 两界同号才算稳健):")
    for i, s in enumerate(tot["segments"], 1):
        if not s["events"]:
            continue
        up, lo = s["net_upper_usd"], s["net_lower_usd"]
        if up >= 0 and lo >= 0:
            d = "正收益(稳健)"
        elif up < 0 and lo < 0:
            d = "负收益(稳健)"
        else:
            d = "方向不定(上下界异号)"
        be = s.get("breakeven_cache_discount")
        be_s = f"; 盈亏平衡缓存折扣={be:.2f}" if be is not None else ""
        ap(f"  S{i}: {d}{be_s} | 膨胀事件(S>M, 摘要比中段还长): {s['bloat_events']}/{s['events']}")

    # Three-column footer
    ap("")
    ap("━" * 74)
    ap("已证实的结论 (直接来自数据, 不依赖价格假设)")
    ap("━" * 74)
    proven: list[str] = []
    n_seg = len(tot["segments"])
    total_events = sum(s["events"] for s in tot["segments"])
    total_bloat = sum(s["bloat_events"] for s in tot["segments"])
    total_savings_tok = sum(s["savings_tokens_sum"] for s in tot["segments"])
    proven.append(f"1. 归因到 {total_events} 次压缩摘要调用 (marker 启发式), 分布在 {n_seg} 个数据分段;")
    proven.append(f"2. 其中 {total_bloat} 次 S>M (摘要输出比被压中段还长) — 这类压缩纯增 prompt 体积, 无需价格假设即可判负;")
    if total_savings_tok < 0:
        proven.append("3. Σ(C×(M−S)) 为负: 即使不谈缓存失效, 压缩在这批数据上也没省下输入 token;")
    else:
        proven.append(f"3. Σ(C×(M−S)) = {total_savings_tok:,} tok: 压缩确实缩小了后续 prompt (token 计量为实测);")
    if c0["entries_with_cache_hits"] == 0 and c0["entries_with_cost"] == 0:
        proven.append("4. 尚无任何真实缓存命中/cost 数据 (网关未重启; 已出现的字段条目命中全为 0) — 见待 C0 栏。")
    for line in proven:
        ap(line)

    ap("")
    ap("━" * 74)
    ap("估计 (含置信度)")
    ap("━" * 74)
    est: list[str] = []
    for i, s in enumerate(tot["segments"], 1):
        if not s["events"]:
            continue
        est.append(f"S{i}: 摘要费用≈{_fmt_usd(s['summary_cost_usd'])} (token 实测×假设价, 置信度:中); "
                   f"缓存失效损失≈{_fmt_usd(s['cache_loss_usd'])} (cache_probe 前缀×(1-折扣)×一次重读, 置信度:低-中); "
                   f"净收益上/下界 {_fmt_usd(s['net_upper_usd'])} / {_fmt_usd(s['net_lower_usd'])}")
    est.append("分段标签置信度: 见分段表 (medium=单一候选带匹配, low=歧义/样本不足); 历史本机 settings 取值无留痕, 只能从 N 分布反推。")
    for line in est:
        ap(line)

    ap("")
    ap("━" * 74)
    ap("必须等 C0 真实数据才能下结论的问题")
    ap("━" * 74)
    ap(f"1. 真实缓存命中率与压缩的因果效应: 需 usage.cache_tokens 在压缩前后条目上对比 (当前命中>0: {c0['entries_with_cache_hits']} 条)。")
    ap("2. 真实费用: 需 cost 字段校准假设价格表 (当前 0 条)。")
    ap("3. 压缩调用的权威归因: 需 mode=history_compress 元数据替代 marker 启发式 (当前 0 条)。")
    ap("4. gateway.log 行数与 request_logs 归因数的差异成因 (实验进程 vs 未落日志调用) 需 C0 元数据区分。")
    ap("   → 网关下次 idle 重启后重新运行本脚本即可自校准。")
    ap("━" * 74)
    return "\n".join(L)


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline compression-vs-cache economics analysis (plan.md C2.1)."
    )
    parser.add_argument("--request-logs-dir", type=Path,
                        default=Path.home() / ".nanobot" / "request_logs")
    parser.add_argument("--gateway-log", type=Path,
                        default=Path.home() / ".nanobot" / "logs" / "gateway.log")
    parser.add_argument("--settings", type=Path,
                        default=Path.home() / ".nanobot" / "history_settings.json")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the full analysis as JSON to this path")
    parser.add_argument("--prices", type=Path, default=None,
                        help="JSON file overriding the assumed price table")
    parser.add_argument("--cache-discount", type=float, default=DEFAULT_CACHE_DISCOUNT,
                        help="cache-read price as a fraction of input price (default 0.10)")
    args = parser.parse_args(argv)

    prices = None
    if args.prices:
        try:
            raw = json.loads(args.prices.read_text(encoding="utf-8"))
            prices = {k: tuple(v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"warning: could not load --prices ({e}); using built-in table", file=sys.stderr)

    paths = list(args.request_logs_dir.glob("*.jsonl"))
    if not paths:
        print(f"note: no request logs found under {args.request_logs_dir}", file=sys.stderr)

    gateway_events = load_gateway_events(args.gateway_log.parent, args.gateway_log.name)
    current_regime = load_current_regime(args.settings)

    analysis = run_analysis(
        paths, gateway_events, current_regime,
        prices=prices, cache_discount=args.cache_discount,
    )
    report = render_report(analysis, current_regime)
    print(report)

    if args.json:
        payload = dict(analysis)
        payload["current_regime"] = (
            {k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v)
             for k, v in current_regime.items()}
            if current_regime else None
        )
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON written to {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
