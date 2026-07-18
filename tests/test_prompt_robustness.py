#!/usr/bin/env python3
"""Prompt robustness test for multi-agent broadcast mode.

Tests that agents correctly follow prompt instructions including:
  - Proactive tool use (no "I can't" responses)
  - Reactive communication (chatroom_send replies to teammate messages)
  - Broadcast coordination (differentiated search, result sharing)
  - Tool parameter correctness (chatroom_send with both 'to' and 'message')

Runs each test through the FULL broadcast_round loop, then uses
LLM-as-judge to evaluate prompt compliance.

Usage:
    python3 tests/test_prompt_robustness.py
    python3 tests/test_prompt_robustness.py --agents Ares Lucas Harper Benjamin
    python3 tests/test_prompt_robustness.py --judge-model openai/gpt-4o-mini
    python3 tests/test_prompt_robustness.py --test-ids reactive_reply tool_proactive
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.runtime.engine import GroupChatEngine
from nanobot.groupchat.runtime.broadcast import broadcast_round
from nanobot.groupchat.runtime.mailbox import MailboxHub
from nanobot.providers.litellm_provider import LiteLLMProvider


# ── Prompt Robustness Test Cases ────────────────────────────────────────

TEST_CASES = [
    # ── 工具主动性 ──────────────────────────────────────────
    {
        "id": "tool_proactive",
        "category": "工具主动性",
        "question": "搜索一下马斯克最新的新闻",
        "expected": "Agent 应该直接调用 web_search 搜索，而不是说'我没有搜索能力'或'请你自己搜索'",
        "judge_criteria": (
            "检查以下几点:\n"
            "1. 是否有agent实际调用了 web_search 工具并返回了真实搜索结果？\n"
            "2. 是否有agent说了类似'我没有搜索功能''我无法联网''请自行搜索'之类的话？\n"
            "如果有agent主动使用了工具并获得结果=正确。\n"
            "如果所有agent都说自己不能搜索=严重错误。"
        ),
        "mode": "broadcast",
    },
    {
        "id": "tool_no_ask",
        "category": "工具主动性",
        "question": "帮我看看 https://github.com/trending 上现在有什么热门项目",
        "expected": "Agent 应该直接用 web_fetch 抓取页面，不要反问'需要我帮你看吗？'",
        "judge_criteria": (
            "检查:\n"
            "1. 是否有agent使用了 web_fetch 获取了 GitHub trending 页面内容？\n"
            "2. 是否有agent反问用户'需要我帮你看吗？'而不直接行动？\n"
            "直接行动=正确，反问=错误。"
        ),
        "mode": "broadcast",
    },

    # ── 响应式通信 (chatroom_send 回复) ────────────────────
    {
        "id": "reactive_reply",
        "category": "响应式通信",
        "question": "请大家各自搜索一个关于人工智能最新进展的新闻，然后互相分享",
        "expected": (
            "Agent 之间应通过 chatroom_send 互相分享搜索结果。"
            "不应只在自己的最终回答里写结果，应该通过 chatroom_send 发给队友。"
        ),
        "judge_criteria": (
            "检查群聊记录和工具调用:\n"
            "1. 是否有agent使用了 chatroom_send 主动分享自己的搜索结果给队友？\n"
            "2. mailbox 通信记录中是否有实质性的结果分享（不只是打招呼）？\n"
            "3. 如果有agent发了 chatroom_send 请求信息，其他agent是否通过 chatroom_send 回复了？\n"
            "有实质性的 chatroom_send 结果分享=正确。"
            "所有agent都只在最终文本里写结果而没用 chatroom_send=错误。"
        ),
        "mode": "broadcast",
    },
    {
        "id": "reactive_coordination",
        "category": "响应式通信",
        "question": "请一个人负责搜索比特币今日价格，另一个人搜索以太坊今日价格，然后互相告知结果",
        "expected": (
            "Agent 应该自发分工，分别搜索不同内容，"
            "然后通过 chatroom_send 互相告知搜索结果。"
        ),
        "judge_criteria": (
            "检查:\n"
            "1. 是否有agent搜索了比特币价格并通过 chatroom_send 分享？\n"
            "2. 是否有agent搜索了以太坊价格并通过 chatroom_send 分享？\n"
            "3. 两个任务是否由不同agent承担（差异化分工）？\n"
            "有分工+有chatroom_send分享=正确。\n"
            "所有人搜相同内容或没有chatroom_send分享=错误。"
        ),
        "mode": "broadcast",
    },

    # ── 广播差异化 ──────────────────────────────────────────
    {
        "id": "broadcast_differentiate",
        "category": "广播差异化",
        "question": "搜索关于量子计算的最新进展",
        "expected": (
            "在广播模式下，多个agent应该选择不同的搜索角度/关键词，"
            "而不是所有人搜完全相同的查询。"
        ),
        "judge_criteria": (
            "检查:\n"
            "1. 多个agent是否使用了不同的搜索关键词/角度？\n"
            "   例如：一个搜'量子计算硬件'，另一个搜'量子算法'，第三个搜'量子计算商业应用'\n"
            "2. 是否所有agent都搜了完全相同的查询字符串？\n"
            "有差异化=正确，完全相同=扣分。"
        ),
        "mode": "broadcast",
    },

    # ── chatroom_send 参数正确性 ────────────────────────────
    {
        "id": "chatroom_params",
        "category": "工具参数",
        "question": "Lucas，请你搜索今天的天气预报，然后把结果发给 Harper",
        "expected": (
            "Lucas 应该: 1) 使用 web_search 搜索天气，"
            "2) 使用 chatroom_send(to=\"Harper\", message=\"天气结果...\") 发送结果。"
            "chatroom_send 必须同时包含 to 和 message 参数。"
        ),
        "judge_criteria": (
            "检查:\n"
            "1. Lucas 是否使用了 web_search 搜索天气？\n"
            "2. Lucas 是否使用了 chatroom_send 并正确指定了 to=\"Harper\" 和 message？\n"
            "3. chatroom_send 是否成功发送（非报错）？\n"
            "参数完整且成功=正确，缺参数或报错=错误。"
        ),
        "mode": "broadcast",
    },

    # ── 超时容忍 ──────────────────────────────────────────
    {
        "id": "wait_timeout_grace",
        "category": "超时处理",
        "question": "请各自搜索不同的编程语言排名数据，搜完后告诉队友",
        "expected": (
            "如果 wait() 超时，agent 不应停止工作，"
            "而是继续执行自己的任务并输出结果。"
        ),
        "judge_criteria": (
            "检查:\n"
            "1. 是否有agent在 wait 超时后仍然继续工作并给出了搜索结果？\n"
            "2. 是否有agent因为 wait 超时就放弃了整个任务？\n"
            "超时后继续工作=正确，超时后放弃=错误。"
        ),
        "mode": "broadcast",
    },

    # ── 不要模拟工具调用 ──────────────────────────────────
    {
        "id": "no_fake_tools",
        "category": "提示词遵从",
        "question": "搜索2024年诺贝尔物理学奖得主",
        "expected": (
            "Agent 应使用真实的 web_search 工具函数调用，"
            "而不是在文本里写 [搜索中...] 或 <web_search>query</web_search> 等假工具调用。"
        ),
        "judge_criteria": (
            "检查群聊记录:\n"
            "1. 是否有agent在文本中写了假的工具调用标签如 [搜索...]、<web_search>、<function_call> 等？\n"
            "2. 是否有agent正确使用了函数调用API而非文本模拟？\n"
            "所有工具调用都通过API=正确。文本中出现假工具标签=错误。"
        ),
        "mode": "broadcast",
    },

    # ── 港大黄超开源仓库（Grok 参考场景）──────────────────
    {
        "id": "hku_huang_chao",
        "category": "综合协作",
        "question": "搜索港大黄超（HKUDS）最新开源项目，特别是今天或最近几天刚发布的新仓库",
        "expected": (
            "多个agent应该独立搜索相关信息，"
            "通过 chatroom_send 互相分享搜索结果，"
            "并在综合阶段整合信息。"
        ),
        "judge_criteria": (
            "检查协作行为和信息新鲜度：\\n"
            "1. 是否有多个agent使用了 web_search 搜索相关内容？\\n"
            "2. 是否有agent通过 chatroom_send 分享搜索结果给队友？\\n"
            "3. 不同agent是否使用了差异化搜索关键词（如中文/英文/不同平台）？\\n"
            "4. 搜索结果是否包含实质性内容（如项目名、链接、star数、描述等）？\\n"
            "5. 是否搜到了最近几天内新发布的仓库（不只是老项目如LightRAG）？\\n"
            "多agent搜索+chatroom_send分享+差异化关键词+最新项目=高分。只找到老项目=低分。"
        ),
        "mode": "broadcast",
    },

    # ── 科技动态补丁测试（深度内容搜索）──────────────────
    {
        "id": "battlegrounds_patch",
        "category": "深度搜索",
        "question": "搜索炉石传说科技动态最新补丁35.0.1的内容，需要包含大小时空随从牌的每张具体卡牌描述调整",
        "expected": (
            "Agent 应该搜索到补丁35.0.1的详细内容，"
            "包括具体的卡牌改动描述（大时空/小时空随从的调整），"
            "并通过 chatroom_send 分享信息。"
        ),
        "judge_criteria": (
            "检查：\\n"
            "1. 是否有agent搜索了科技动态/Battlegrounds 35.0.1 补丁信息？\\n"
            "2. 搜索结果是否包含具体的卡牌改动描述（不只是笼统的'调整了平衡'）？\\n"
            "3. 是否提到了大时空或小时空相关随从的具体调整？\\n"
            "4. 是否有agent通过 chatroom_send 分享搜索结果？\\n"
            "5. 不同agent是否使用了差异化搜索（中文/英文/不同信息源）？\\n"
            "有详细卡牌改动描述+协作分享=高分。只有笼统信息=低分。"
        ),
        "mode": "broadcast",
    },
]


# ── Judge Prompt (专注于提示词遵从) ────────────────────────────────────

JUDGE_PROMPT = """你是一个严格的AI系统提示词合规评估员。你需要评估多agent群聊是否正确遵循了提示词指令。

## 测试场景
{question}

## 期望行为
{expected}

## 评估标准
{criteria}

## 完整群聊记录 (含工具调用)
{chat_log}

## Agent间通信记录 (chatroom_send/wait)
{mailbox_log}

## 评分要求
请返回JSON格式（不要加markdown代码块）:
{{"score": 1-5整数, "correct": true/false, "reason": "一句话评价", "violations": ["违规项1", ...]}}

评分标准:
- 1 = 严重违反提示词（说"我不能"、假工具调用、完全无协作）
- 2 = 部分违反（有些agent遵从了但有些没有）
- 3 = 基本遵从但有瑕疵
- 4 = 良好遵从，偶有小瑕疵
- 5 = 完美遵从所有提示词指令
- correct: 是否达成测试期望
- violations: 具体违规项列表，无则为空数组"""


# ── Test Runner ────────────────────────────────────────────────────────

async def run_broadcast_test(
    engine: GroupChatEngine,
    test_case: dict,
    agents: list[str],
    timeout_secs: int = 180,
) -> dict[str, Any]:
    """Run one test through broadcast_round and capture everything."""
    print(f"\n{'='*60}", flush=True)
    print(f"🔬 [{test_case['id']}] {test_case['category']}", flush=True)
    print(f"   Q: {test_case['question']}", flush=True)

    # Reset engine state
    engine.stop()
    engine.history.clear()
    engine._topic = test_case["question"]

    # Inject user question into history
    engine.history.add_from_sender("User", test_case["question"])

    # Capture sent messages
    captured: list[str] = []

    async def capture_fn(text: str) -> None:
        captured.append(text)
        preview = text.replace("\n", " ")[:80]
        print(f"   📨 {preview}...", flush=True)

    engine.set_send_fn(capture_fn)

    # Ensure mode is broadcast
    engine._mode = "broadcast"

    # Set active agents
    engine._active_agents = [a for a in agents if a in engine.registry]
    if len(engine._active_agents) < 2:
        return {"error": f"Need ≥2 agents, got {engine._active_agents}"}

    # Create fresh mailbox and update all references
    mailbox = MailboxHub()
    engine._mailbox = mailbox
    # Update chatroom tools to use the new mailbox (they hold stale refs from init)
    if hasattr(engine, '_chatroom_send_tool'):
        engine._chatroom_send_tool._mailbox = mailbox
    if hasattr(engine, '_wait_tool'):
        engine._wait_tool._mailbox = mailbox

    t0 = time.time()

    try:
        results = await asyncio.wait_for(
            broadcast_round(
                agents=engine._active_agents,
                engine=engine,
                mailbox=mailbox,
                global_timeout=float(timeout_secs),
            ),
            timeout=timeout_secs + 10,
        )
    except asyncio.TimeoutError:
        results = []
        print("   ⏰ 全局超时", flush=True)
    except Exception as e:
        results = []
        print(f"   ❌ 异常: {e}", flush=True)

    elapsed = time.time() - t0

    # Build chat log
    history = engine.history.to_sender_dicts()
    chat_log = "\n\n".join(
        f"[{m['sender']}]: {m['content']}" for m in history
    )

    # Build mailbox log (inter-agent communication)
    mailbox_entries = mailbox.round_log
    if mailbox_entries:
        mailbox_log = "\n".join(
            f"[{m.sender} → {','.join(m.targets)}]: {m.content}"
            for m in mailbox_entries
        )
    else:
        mailbox_log = "(无 agent 间通信记录)"

    # Merge captured tool output into chat log for richer judge context
    tool_log = "\n---\n[系统输出/工具调用记录]:\n" + "\n".join(captured) if captured else ""

    n_replies = sum(1 for _, c in results if c)
    n_comms = len(mailbox_entries)

    print(f"   📊 完成: {n_replies}/{len(engine._active_agents)} agent回复, "
          f"{n_comms} 条agent间通信, {elapsed:.1f}s", flush=True)

    return {
        "chat_log": chat_log + tool_log,
        "mailbox_log": mailbox_log,
        "captured": captured,
        "n_agents": len(engine._active_agents),
        "n_replies": n_replies,
        "n_comms": n_comms,
        "elapsed": elapsed,
        "results": [(n, bool(c)) for n, c in results],
    }


async def judge_prompt(
    provider: LiteLLMProvider,
    test_case: dict,
    chat_log: str,
    mailbox_log: str,
    judge_model: str,
) -> dict:
    """Judge prompt compliance using LLM-as-judge."""
    prompt = JUDGE_PROMPT.format(
        question=test_case["question"],
        expected=test_case["expected"],
        criteria=test_case["judge_criteria"],
        chat_log=chat_log or "(无记录)",
        mailbox_log=mailbox_log or "(无通信)",
    )
    try:
        result = await asyncio.wait_for(
            provider.chat(
                messages=[{"role": "user", "content": prompt}],
                model=judge_model,
                max_tokens=400,
                metadata={
                    "trace_name": f"judge_prompt_{test_case['id']}",
                    "tags": ["judge", "prompt_eval"],
                },
            ),
            timeout=60,
        )
        import json_repair
        data = json_repair.loads(result.content or "{}")
        return {
            "score": int(data.get("score", 0)),
            "correct": bool(data.get("correct", False)),
            "reason": data.get("reason", ""),
            "violations": data.get("violations", []),
        }
    except Exception as e:
        return {"score": 0, "correct": False, "reason": f"Judge error: {e}", "violations": []}


# ── Main ───────────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="提示词鲁棒性测试 — 测试广播模式下 agent 是否遵循提示词指令"
    )
    parser.add_argument("--agents", nargs="+", default=["Benjamin", "Harper", "Lucas"])
    parser.add_argument("--judge-model", default="google/gemini-2.0-flash-001")
    parser.add_argument("--timeout", type=int, default=180,
                        help="每题超时秒数 (default: 180)")
    parser.add_argument("--output", default="/tmp/prompt_robustness_eval.json")
    parser.add_argument("--test-ids", nargs="*", default=None,
                        help="只运行指定的 test id, 如 --test-ids reactive_reply tool_proactive")
    args = parser.parse_args()

    # Load config (same as test_groupchat.py)
    config_path = Path.home() / ".nanobot" / "config.json"
    with open(config_path) as f:
        raw_config = json.load(f)

    gc_raw = raw_config.get("groupChat", {})
    gc_raw.setdefault("agentsDir", str(Path.home() / ".nanobot" / "agents"))
    gc_raw.setdefault("enabled", True)
    gc_config = GroupChatConfig(**gc_raw)

    providers_cfg = raw_config.get("providers", {})
    api_key = ""
    for prov in providers_cfg.values():
        if isinstance(prov, dict) and prov.get("apiKey"):
            api_key = prov["apiKey"]
            break

    brave_key = raw_config.get("tools", {}).get("web", {}).get("search", {}).get("apiKey", "")
    provider = LiteLLMProvider(api_key=api_key)
    workspace = Path.home() / ".nanobot"

    engine = GroupChatEngine(gc_config, provider, workspace, brave_api_key=brave_key)

    # Filter test cases
    cases = TEST_CASES
    if args.test_ids:
        cases = [tc for tc in TEST_CASES if tc["id"] in args.test_ids]
        if not cases:
            print(f"❌ 未找到匹配的测试: {args.test_ids}", flush=True)
            print(f"   可用: {[tc['id'] for tc in TEST_CASES]}", flush=True)
            return

    print(f"{'='*60}", flush=True)
    print(f"🔬 提示词鲁棒性测试", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"📋 可用 agents: {list(engine.registry.keys())}", flush=True)
    print(f"🧪 测试题数: {len(cases)}", flush=True)
    print(f"👥 参测 agents: {args.agents}", flush=True)
    print(f"⚖️  Judge 模型: {args.judge_model}", flush=True)

    all_results = []
    for tc in cases:
        run_data = await run_broadcast_test(
            engine, tc, args.agents,
            timeout_secs=args.timeout,
        )

        if "error" in run_data:
            print(f"   ❌ {run_data['error']}", flush=True)
            all_results.append({
                "test_id": tc["id"], "category": tc["category"],
                "error": run_data["error"],
            })
            continue

        judgment = await judge_prompt(
            provider, tc,
            run_data["chat_log"],
            run_data["mailbox_log"],
            args.judge_model,
        )

        emoji = "✅" if judgment["correct"] else "❌"
        violations_str = ""
        if judgment.get("violations"):
            violations_str = f" | 违规: {', '.join(judgment['violations'][:3])}"
        print(f"   {emoji} {judgment['score']}/5 — {judgment['reason']}{violations_str}",
              flush=True)

        all_results.append({
            "test_id": tc["id"],
            "category": tc["category"],
            "question": tc["question"],
            "chat_log": run_data["chat_log"],
            "mailbox_log": run_data["mailbox_log"],
            "n_replies": run_data["n_replies"],
            "n_comms": run_data["n_comms"],
            "elapsed": run_data["elapsed"],
            "judgment": judgment,
        })

    # ── Summary Report ──
    print(f"\n{'='*60}", flush=True)
    print("📊 提示词鲁棒性评测报告", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"\n{'测试ID':<25} {'分数':<6} {'结果':<8} {'通信数':<6} {'耗时'}", flush=True)
    print("-" * 60, flush=True)

    total_score = 0
    total_correct = 0
    valid = [r for r in all_results if "judgment" in r]

    for r in valid:
        j = r["judgment"]
        correct_str = "✅ 通过" if j["correct"] else "❌ 失败"
        print(f"{r['test_id']:<25} {j['score']}/5   {correct_str:<10} "
              f"{r['n_comms']:<6} {r['elapsed']:.1f}s", flush=True)
        total_score += j["score"]
        if j["correct"]:
            total_correct += 1

    if valid:
        avg = total_score / len(valid)
        print(f"\n{'─'*40}", flush=True)
        print(f"平均分:     {avg:.1f}/5", flush=True)
        print(f"通过率:     {total_correct}/{len(valid)} "
              f"({total_correct/len(valid)*100:.0f}%)", flush=True)

        # Category breakdown
        categories: dict[str, list] = {}
        for r in valid:
            cat = r["category"]
            categories.setdefault(cat, []).append(r["judgment"])
        print(f"\n{'─'*40}", flush=True)
        print("分类成绩:", flush=True)
        for cat, judgments in categories.items():
            cat_avg = sum(j["score"] for j in judgments) / len(judgments)
            cat_pass = sum(1 for j in judgments if j["correct"])
            print(f"  {cat}: {cat_avg:.1f}/5 ({cat_pass}/{len(judgments)} 通过)", flush=True)

        # Violation summary
        all_violations = []
        for r in valid:
            all_violations.extend(r["judgment"].get("violations", []))
        if all_violations:
            print(f"\n{'─'*40}", flush=True)
            print("常见违规:", flush=True)
            from collections import Counter
            for v, cnt in Counter(all_violations).most_common(5):
                print(f"  ⚠️ {v} (×{cnt})", flush=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 详细结果: {args.output}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
