"""Tests for scripts/analyze_compression_cache.py (plan.md 批次 C2.1).

Pure computation-logic tests over **synthetic** fixtures written to tmp_path —
no real ~/.nanobot data is read, copied, or produced.  The script is imported
by file path (scripts/ is not a package).
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_compression_cache.py"
_spec = importlib.util.spec_from_file_location("analyze_compression_cache", _SCRIPT)
acc = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("analyze_compression_cache", acc)
_spec.loader.exec_module(acc)


# ── fixture helpers ────────────────────────────────────────────────────────

def make_entry(
    ts: str = "2026-06-01 12:00:00",
    *,
    status: str = "ok",
    mode: str | None = "broadcast",
    model: str = "openrouter/z-ai/glm-5.1",
    msg_count: int = 5,
    prompt: int = 1000,
    completion: int = 100,
    content: str = "普通消息",
    cache_ratio: int | None = 80,
    cacheable_chars: int | None = 3200,
    cost: float | None = None,
    cache_tokens: int | None = None,
) -> dict:
    msgs = [{"role": "user", "content": content}]
    while len(msgs) < msg_count:
        msgs.append({"role": "assistant", "content": "x" * 10})
    probe = None
    if cache_ratio is not None:
        probe = {
            "x-cache-ratio-pct": cache_ratio,
            "x-cache-cacheable-chars": cacheable_chars or 0,
        }
    usage: dict = {"prompt": prompt, "completion": completion, "total": prompt + completion}
    if cache_tokens is not None:
        usage["cache_tokens"] = cache_tokens
    rec = {
        "ts": ts,
        "agent": None, "session": None, "topic": None, "mode": mode,
        "model": model, "max_tokens": 4096, "stream": False,
        "tools_count": 1, "messages": msgs, "total_chars": 10 * msg_count,
        "msg_count": msg_count, "latency": 1.0,
        "status": status, "usage": usage,
    }
    if probe:
        rec["cache_probe"] = probe
    if cost is not None:
        rec["cost"] = cost
    return rec


def summary_call(ts: str, n: int, *, prompt: int = 3000, completion: int = 800,
                 model: str = "openrouter/z-ai/glm-5.1", mode: str | None = None) -> dict:
    content = (
        "以下是群聊的一段中期历史记录（共 %d 条）。\n请用简洁的中文摘要这些内容。\n\n[x]: 内容" % n
    )
    return make_entry(ts, mode=mode, model=model, msg_count=1,
                      prompt=prompt, completion=completion, content=content)


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


GW_LINE = (
    "2026-09-08 00:27:49.747 | INFO     | nanobot.groupchat.history.context:"
    "_compress_view:528 - HistoryContext: compressed 44 → summary"
)


# ── parsing / attribution ──────────────────────────────────────────────────

class TestParsing:
    def test_marker_detected_with_fullwidth_parens_and_count(self):
        e = acc.slim_entry(summary_call("2026-06-01 10:00:00", 17))
        assert e is not None and e.middle_count == 17

    def test_marker_ascii_parens_variant(self):
        rec = summary_call("2026-06-01 10:00:00", 5)
        rec["messages"][0]["content"] = "中期历史记录(共 5 条)\n..."
        e = acc.slim_entry(rec)
        assert e is not None and e.middle_count == 5

    def test_no_marker_no_attribution(self):
        e = acc.slim_entry(make_entry(msg_count=1))
        assert e is not None and e.middle_count is None

    def test_multi_message_entry_never_attributed(self):
        rec = summary_call("2026-06-01 10:00:00", 9)
        rec["msg_count"] = 2
        rec["messages"].append({"role": "user", "content": "追加"})
        e = acc.slim_entry(rec)
        assert e is not None and e.middle_count is None

    def test_error_entries_not_counted_as_ok_stream(self):
        rec = summary_call("2026-06-01 10:00:00", 9)
        rec["status"] = "error"
        e = acc.slim_entry(rec)
        assert e is not None and e.status == "error"

    def test_c0_fields_carried_through(self):
        e = acc.slim_entry(make_entry(cost=0.0123, cache_tokens=900))
        assert e is not None and e.cost == 0.0123 and e.cache_tokens == 900

    def test_bad_lines_skipped_by_stream(self, tmp_path):
        p = write_jsonl(tmp_path / "2026-06-01.jsonl", [
            summary_call("2026-06-01 10:00:00", 3),
            {"no_ts": True},
            make_entry(ts="2026-06-01 10:01:00"),
        ])
        (tmp_path / "broken.jsonl").write_text("not json\n", encoding="utf-8")
        entries = list(acc.iter_request_log_entries(tmp_path.glob("2026-06-01.jsonl")))
        assert len(entries) == 2

    def test_gateway_line_parsing(self):
        events = acc.parse_gateway_lines([
            GW_LINE,
            "2026-09-08 00:27:50.001 | WARNING | unrelated",
            "garbage",
        ])
        assert len(events) == 1
        assert events[0].middle_count == 44
        assert events[0].dt == datetime(2026, 9, 8, 0, 27, 49)

    def test_gateway_rotated_gz_loaded(self, tmp_path):
        (tmp_path / "gateway.log.2.gz").parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(tmp_path / "gateway.log.2.gz", "wt", encoding="utf-8") as f:
            f.write(GW_LINE.replace("2026-09-08", "2026-09-07") + "\n")
        (tmp_path / "gateway.log").write_text(GW_LINE + "\n", encoding="utf-8")
        events = acc.load_gateway_events(tmp_path)
        assert [e.middle_count for e in events] == [44, 44]
        assert events[0].dt < events[1].dt


# ── segmentation ───────────────────────────────────────────────────────────

def _events_from_ns(day_ns: list[tuple[str, list[int]]]) -> list:
    out = []
    for day, ns in day_ns:
        for k, n in enumerate(ns):
            out.append(acc.slim_entry(summary_call(f"{day} 1{k % 10}:00:00", n)))
    return [e for e in out if e is not None]


class TestSegmentation:
    def test_three_regimes_split_with_labels(self):
        # Regime 1: small-N frequent (local-style 30/0.7/20, N≈5, ≥5/day)
        # quiet gap → Regime 2: mid-N sparse (50/0.8/6-style, N≈30, <2/day)
        # shift within block → Regime 3: large-N (200/0.8/20-style, N≈150)
        evs = _events_from_ns([
            ("2026-05-01", [4, 5, 6, 5, 4, 6, 5, 5]),
            ("2026-05-02", [5, 6, 4, 5]),
            # 20+ day quiet gap
            ("2026-06-01", [28, 30, 32]),
            ("2026-06-04", [29]),
            ("2026-06-07", [31, 30]),
            ("2026-06-10", [30, 29]),
            ("2026-06-13", [31]),
            ("2026-06-18", [150, 148, 152, 149, 151, 150, 147, 153, 150, 149, 151, 150]),
        ])
        segs = acc.infer_segments(evs)
        assert len(segs) == 3
        acc.label_segment(segs[0]); acc.label_segment(segs[1]); acc.label_segment(segs[2])
        assert "Rlocal30" in segs[0].label
        assert segs[0].confidence == "medium"
        assert "R50" in segs[1].label
        assert "R200" in segs[2].label
        # boundaries respect the quiet gap
        assert segs[0].end < datetime(2026, 5, 3)
        assert segs[1].start >= datetime(2026, 6, 1)

    def test_ambiguous_overlapping_bands_get_low_confidence(self):
        # N≈25 falls into both R50 band (15-48) and Rlocal30 band (1-60),
        # and the mid rate (3/active day) cannot disambiguate → low confidence.
        evs = _events_from_ns([
            ("2026-06-01", [24, 25, 26]),
            ("2026-06-05", [25, 26, 24]),
        ])
        segs = acc.infer_segments(evs)
        assert len(segs) == 1
        acc.label_segment(segs[0])
        assert segs[0].confidence == "low"
        assert "歧义" in segs[0].label

    def test_empty_data_yields_no_segments(self):
        assert acc.infer_segments([]) == []

    def test_few_events_single_low_confidence_segment(self):
        evs = _events_from_ns([("2026-06-01", [30, 31])])
        segs = acc.infer_segments(evs)
        assert len(segs) == 1
        acc.label_segment(segs[0])
        assert segs[0].confidence == "low"
        assert "样本不足" in segs[0].label


# ── economics ──────────────────────────────────────────────────────────────

class TestEconomics:
    def _stream(self, tmp_path, prompts):
        recs = []
        base = datetime(2026, 6, 1, 12, 0, 0)
        for i, p in enumerate(prompts):
            ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
            recs.append(make_entry(ts, prompt=p, completion=50))
        return write_jsonl(tmp_path / "2026-06-01.jsonl", recs)

    def test_net_benefit_positive_when_middle_large_and_summary_small(self, tmp_path):
        # Prompt hovers at 20k then drops to 5k after the compression event.
        prompts = [20000, 20000, 20000] + [5000] * 10
        self._stream(tmp_path, prompts)
        entries = sorted(acc.iter_request_log_entries([tmp_path / "2026-06-01.jsonl"]), key=lambda e: e.dt)
        stream = [e for e in entries if e.status == "ok" and e.mode == "broadcast"]
        event = acc.slim_entry(
            summary_call("2026-06-01 12:12:30", n=20, prompt=15000, completion=500)
        )
        eco = acc.event_economics(event, stream, [e.dt for e in stream], None)
        assert eco.m_tokens == 15000 and eco.s_tokens == 500
        # all 10 post-event calls stay below the 20k pre median → all counted
        assert eco.calls_in_window == 10
        assert eco.savings_tokens == 10 * (15000 - 500)
        assert eco.value_saved_full > 0
        # upper bound positive, lower bound (cache price) small but same sign
        assert eco.value_saved_full > eco.value_saved_cached
        assert eco.cache_loss > 0
        assert eco.prefix_source == "cache_probe"
        assert eco.summary_cost_measured is False

    def test_bloat_event_negative_savings_without_prices(self, tmp_path):
        # Summary longer than the middle it replaces (S > M) — negative savings
        # regardless of any price assumption.  Post prompts sit just below the
        # pre median so the whole 5-call window is counted.
        prompts = [20000, 20000, 20000] + [19000] * 5
        self._stream(tmp_path, prompts)
        entries = sorted(acc.iter_request_log_entries([tmp_path / "2026-06-01.jsonl"]), key=lambda e: e.dt)
        stream = [e for e in entries if e.status == "ok" and e.mode == "broadcast"]
        event = acc.slim_entry(
            summary_call("2026-06-01 12:12:30", n=2, prompt=800, completion=2000)
        )
        eco = acc.event_economics(event, stream, [e.dt for e in stream], None)
        assert eco.savings_tokens == 5 * (800 - 2000) < 0
        assert eco.value_saved_full < 0 and eco.value_saved_cached < 0

    def test_real_c0_cost_preferred_over_estimate(self, tmp_path):
        prompts = [9000, 9000, 4000, 4000]
        self._stream(tmp_path, prompts)
        entries = sorted(acc.iter_request_log_entries([tmp_path / "2026-06-01.jsonl"]), key=lambda e: e.dt)
        stream = [e for e in entries if e.status == "ok" and e.mode == "broadcast"]
        rec = summary_call("2026-06-01 12:12:30", n=10, prompt=5000, completion=400)
        rec["cost"] = 0.00123  # real C0 cost
        event = acc.slim_entry(rec)
        eco = acc.event_economics(event, stream, [e.dt for e in stream], None)
        assert eco.summary_cost_measured is True
        assert eco.summary_cost == pytest.approx(0.00123)

    def test_window_stops_at_regrowth(self, tmp_path):
        prompts = [20000, 20000, 5000, 8000, 20000, 20000]
        self._stream(tmp_path, prompts)
        entries = sorted(acc.iter_request_log_entries([tmp_path / "2026-06-01.jsonl"]), key=lambda e: e.dt)
        stream = [e for e in entries if e.status == "ok" and e.mode == "broadcast"]
        event = acc.slim_entry(
            summary_call("2026-06-01 12:12:30", n=15, prompt=12000, completion=300)
        )
        eco = acc.event_economics(event, stream, [e.dt for e in stream], None)
        # 5000, 8000 counted; the 20000 call ends the window (regrowth)
        assert eco.calls_in_window == 2

    def test_prefix_fallback_without_probe(self, tmp_path):
        prompts = [10000, 10000, 4000, 4000]
        recs = []
        base = datetime(2026, 6, 1, 12, 0, 0)
        for i, p in enumerate(prompts):
            ts = (base + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
            recs.append(make_entry(ts, prompt=p, completion=50,
                                   cache_ratio=None, cacheable_chars=None))
        write_jsonl(tmp_path / "2026-06-01.jsonl", recs)
        entries = sorted(acc.iter_request_log_entries([tmp_path / "2026-06-01.jsonl"]), key=lambda e: e.dt)
        stream = [e for e in entries if e.status == "ok" and e.mode == "broadcast"]
        event = acc.slim_entry(
            summary_call("2026-06-01 12:12:30", n=10, prompt=6000, completion=500)
        )
        eco = acc.event_economics(event, stream, [e.dt for e in stream], None)
        assert eco.prefix_source.startswith("fallback")
        assert eco.cacheable_prefix_tokens == 10000


class TestPriceLookup:
    def test_prefix_stripped_and_matched(self):
        assert acc.price_for("openrouter/z-ai/glm-5.1") == acc.price_for("z-ai/glm-5.1")
        assert acc.price_for("openrouter/openai/gpt-4.1-nano") == (0.10, 0.40)

    def test_unknown_model_uses_fallback(self):
        assert acc.price_for("totally-unknown-model") == (0.50, 1.50)

    def test_override_table(self):
        prices = {"glm-5.1": (1.0, 4.0), "*": (2.0, 8.0)}
        assert acc.price_for("openrouter/z-ai/glm-5.1", prices) == (1.0, 4.0)
        assert acc.price_for("mystery", prices) == (2.0, 8.0)


# ── aggregate analysis / report rendering ──────────────────────────────────

def _synthetic_day(tmp_path, *, with_c0: bool = False) -> Path:
    recs = [
        make_entry("2026-06-01 12:00:00", prompt=20000, completion=100),
        make_entry("2026-06-01 12:05:00", prompt=20000, completion=100),
        summary_call("2026-06-01 12:10:00", n=20, prompt=15000, completion=600),
        make_entry("2026-06-01 12:15:00", prompt=6000, completion=100),
        make_entry("2026-06-01 12:20:00", prompt=7000, completion=100),
    ]
    if with_c0:
        for r in recs:
            r["usage"]["cache_tokens"] = 3000 if r["status"] == "ok" else 0
            r["cost"] = 0.001
    return write_jsonl(tmp_path / "2026-06-01.jsonl", recs)


class TestRunAnalysisAndReport:
    def test_zero_c0_data_handled_with_note(self, tmp_path):
        _synthetic_day(tmp_path, with_c0=False)
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], [], None)
        assert result["c0_fields"]["entries_with_cache_tokens"] == 0
        assert result["c0_fields"]["entries_with_cost"] == 0
        report = acc.render_report(result, None)
        assert "自网关下次重启起积累" in report
        assert "marker 启发式" in report

    def test_with_c0_fields_reported(self, tmp_path):
        _synthetic_day(tmp_path, with_c0=True)
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], [], None)
        assert result["c0_fields"]["entries_with_cache_tokens"] == 5
        assert result["c0_fields"]["entries_with_cost"] == 5
        report = acc.render_report(result, None)
        assert "usage.cache_tokens: 5 条" in report

    def test_empty_input_renders_graceful_report(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        result = acc.run_analysis(list(empty_dir.glob("*.jsonl")), [], None)
        assert result["entries_total"] == 0
        assert result["segments"] == []
        report = acc.render_report(result, None)
        assert "无压缩事件可分段" in report

    def test_report_has_three_conclusion_columns(self, tmp_path):
        _synthetic_day(tmp_path)
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], [], None)
        report = acc.render_report(result, None)
        assert "已证实的结论" in report
        assert "估计 (含置信度)" in report
        assert "必须等 C0 真实数据才能下结论的问题" in report

    def test_gateway_mismatch_reported(self, tmp_path):
        _synthetic_day(tmp_path)
        gw = acc.parse_gateway_lines([
            GW_LINE.replace("2026-09-08 00:27:49", "2026-06-01 12:10:01"),
            GW_LINE.replace("2026-09-08 00:27:49", "2026-06-01 12:10:02"),
        ])
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], gw, None)
        assert result["gateway_lines_total"] == 2
        assert result["gateway_mismatches"], "2 lines vs 1 attributed call must be flagged"
        report = acc.render_report(result, None)
        assert "不一致" in report

    def test_current_regime_display_and_noop_note(self, tmp_path):
        settings = tmp_path / "history_settings.json"
        settings.write_text(json.dumps({
            "history": {
                "max_messages": 30, "compress_ratio": 0.7,
                "compress_max_summary_tokens": 2000,
            },
            "tool_results": {"summarize_model": "z-ai/glm-5.1"},
        }), encoding="utf-8")
        regime = acc.load_current_regime(settings)
        assert regime is not None
        assert regime["threshold"] == 21
        # keep_recent absent → default 20 kicks in via history_settings defaults
        assert regime["compression_keep_recent"] == 20
        _synthetic_day(tmp_path)
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], [], regime)
        report = acc.render_report(result, regime)
        assert "阈值 21" in report
        assert "近乎 no-op" in report

    def test_json_payload_roundtrip(self, tmp_path):
        _synthetic_day(tmp_path)
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], [], None)
        text = json.dumps(result, ensure_ascii=False)
        back = json.loads(text)
        assert back["entries_ok"] == result["entries_ok"]
        assert back["segments"][0]["events"] == result["segments"][0]["events"]

    def test_net_columns_present_in_report(self, tmp_path):
        _synthetic_day(tmp_path)
        result = acc.run_analysis([tmp_path / "2026-06-01.jsonl"], [], None)
        seg = result["segments"][0]
        assert seg["net_upper_usd"] >= seg["net_lower_usd"]
        assert seg["m_tokens_sum"] == 15000
        assert seg["s_tokens_sum"] == 600
