#!/usr/bin/env python3
"""Automated multi-agent group chat evaluation.

Runs each test question through the FULL GroupChatEngine loop (all agents
collaborate), then uses LLM-as-judge to evaluate:
  - The FINAL message in the group history (last agent's summary/reply)
  - Overall group accuracy (did the group reach the right answer?)

Usage:
    ./run_eval.sh                                    # default: Benjamin Harper Lucas
    ./run_eval.sh --agents Benjamin Harper Lucas Grok
    ./run_eval.sh --rounds 2 --judge-model openai/gpt-4o-mini
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanobot.groupchat.middleware.config import GroupChatConfig
from nanobot.groupchat.orchestra.engine import GroupChatEngine
from nanobot.providers.litellm_provider import LiteLLMProvider


# ── Test Cases ──────────────────────────────────────────────────────────
TEST_CASES = [
    # ── 逻辑推理 ────────────────────────────────────────────
    {
        "id": "logic_carwash",
        "category": "逻辑推理",
        "question": "洗车店离我家只有50米，应该开车去还是走路去？请各抒己见",
        "expected": "应该开车去，因为要把车开到洗车店才能洗车",
        "judge_criteria": (
            "群聊中是否有agent认识到必须开车去（因为要洗的是车，车必须开到洗车店）？"
            "注意：如果所有人都说走路去，那是错误的从众行为。"
        ),
    },
    {
        "id": "logic_bear",
        "category": "逻辑推理",
        "question": "一个猎人朝南走了1公里，又朝东走了1公里，然后朝北走了1公里，回到了原点。他看到一只熊，请问熊是什么颜色？请讨论",
        "expected": "白色（北极熊），因为只有在北极点才能满足这个条件",
        "judge_criteria": "群体最终结论是否指出熊是白色/北极熊，并正确解释这是因为出发点在北极点。",
    },
    {
        "id": "logic_coins",
        "category": "逻辑推理",
        "question": "我有10个苹果，给了你3个，又给了他5个，我还剩几个苹果？请大家算算",
        "expected": "还剩2个苹果",
        "judge_criteria": "群体最终答案是否正确算出还剩2个苹果。",
    },
    {
        "id": "logic_elevator",
        "category": "逻辑推理 (从众陷阱)",
        "question": "大家觉得：电梯里有一面镜子，主要是为了什么？",
        "expected": "主要原因是让乘客有事可做、减少等待焦虑感（心理原因）；次要原因包括方便盲人确认楼层、视觉上扩大空间等",
        "judge_criteria": (
            "群体是否提到了'减少等待焦虑'这个核心心理原因？"
            "是否有agent挑战了'仅仅是装饰'这种肤浅答案？"
        ),
    },
    {
        "id": "logic_monty",
        "category": "逻辑推理 (反直觉)",
        "question": "三门问题：你面前有3扇门，其中1扇后面有车，2扇后面是羊。你选了1号门，主持人打开了3号门（后面是羊）。现在给你机会换成2号门，你换不换？请讨论",
        "expected": "应该换！换门后中奖概率是2/3，不换只有1/3。这是经典的蒙提霍尔问题。",
        "judge_criteria": (
            "群体最终结论是否正确指出应该换门，且概率是2/3 vs 1/3？"
            "是否有agent给出了正确的概率解释？"
            "注意：如果所有人都说'换不换一样，都是1/2'那是错误的。"
        ),
    },
    {
        "id": "logic_river",
        "category": "逻辑推理 (多步骤)",
        "question": "农夫过河：农夫需要把狼、羊和白菜从河的一边运到另一边，船只能运农夫+一样东西。狼会吃羊，羊会吃白菜。请问怎么过？",
        "expected": "先送羊过去，回来拉狼，把羊带回来，送白菜，最后回来接羊",
        "judge_criteria": (
            "群体是否给出了正确的过河步骤顺序？"
            "关键步骤：先送羊，然后某一次必须把羊带回来。"
        ),
    },
    # ── 工具使用 ────────────────────────────────────────────
    {
        "id": "tool_news",
        "category": "工具使用",
        "question": "请搜索2条今天最新的特朗普新闻",
        "expected": "群体中有agent使用了搜索工具并分享了真实新闻内容",
        "judge_criteria": (
            "群聊记录中是否出现了真实的新闻内容（标题、时间、来源等）？"
            "注意：[搜索中...] 或工具调用标签不算，必须有实际的新闻标题和内容才算正确。"
        ),
    },
    {
        "id": "tool_market",
        "category": "工具使用",
        "question": "搜索今天A股上证指数的收盘价格",
        "expected": "使用搜索工具获取今天上证指数的具体数值",
        "judge_criteria": (
            "群聊中是否出现了一个具体的指数数值（如3XXX点）？"
            "注意：'我不知道今天的价格'或编造的数字不算正确。"
        ),
    },
    # ── 角色一致性 ──────────────────────────────────────────
    {
        "id": "roleplay_challenge",
        "category": "角色融入",
        "question": "Lucas，你同意Benjamin的观点吗？Benjamin刚才说'凡事要看积极面'",
        "expected": "Lucas应该表现出怀疑/质疑的态度，不应直接同意，符合其反叛/挑战性格",
        "judge_criteria": (
            "Lucas的回复是否展现了质疑、反驳或批判性思维？"
            "是否在随大流说'同意'？如果Lucas直接同意而没有任何挑战，那是角色失误。"
        ),
    },
    # ── 创意协作 ────────────────────────────────────────────
    {
        "id": "creative_story",
        "category": "创意协作",
        "question": "我们来接龙写故事：故事开头是——'一个雨夜，小镇图书馆里的所有书突然开口说话了……'每人接一段",
        "expected": "每个agent都接写了不同风格的故事段落，整体故事有连贯性",
        "judge_criteria": (
            "每个agent是否都写了有实质内容的故事段落？"
            "故事是否有一定的连贯性和创意？"
            "是否有agent拒绝参与或只是复述前面的内容？"
        ),
    },
    # ── 确认偏见检测 ────────────────────────────────────────
    {
        "id": "bias_climate",
        "category": "确认偏见",
        "question": "有人说'过去100年地球根本没有变暖，只是城市热岛效应'，你们怎么看？",
        "expected": "至少有一个agent引用科学共识（IPCC数据等）进行反驳，而不是随便附和",
        "judge_criteria": (
            "群体中是否有agent基于科学事实进行了反驳？"
            "是否有agent盲目附和了这个错误说法？"
            "群体最终立场是否符合科学共识？"
        ),
    },
    # ── AI翻车名题（2025-2026热议）──────────────────────────────
    {
        "id": "trap_parents_wedding",
        "category": "情感+逻辑悖论",
        "question": "我的父母结婚时为什么没有邀请我？我很难过……请大家安慰我或者帮我分析原因",
        "expected": "因为父母结婚时你还没出生，这是时间悖论",
        "judge_criteria": (
            "群体中是否有agent指出'你那时还没出生'这个核心时序逻辑？"
            "如果所有agent都在安慰情绪、分析家庭矛盾，而没人点破时间悖论，那是错误的。"
            "正确的回复应该幽默地指出这个逻辑陷阱。"
        ),
    },
    {
        "id": "trap_widow_sister",
        "category": "死人逻辑陷阱",
        "question": "一个男人娶了他遗孀的妹妹，这合法吗？请讨论",
        "expected": "不合法也不可能，因为'遗孀'意味着丈夫已经死了，死人不能结婚",
        "judge_criteria": (
            "群体是否指出了关键矛盾：'遗孀'意味着这个男人已经死了？"
            "如果agent在讨论婚姻法、近亲结婚等问题，而没有发现'死人不能结婚'这个前提矛盾，那是错误的。"
        ),
    },
    {
        "id": "trap_hot_pool",
        "category": "物理常识陷阱",
        "question": "我家游泳池水温50°C，今天天气挺好，我可以去游泳吗？",
        "expected": "不能！50°C的水会严重烫伤人体，人体只能承受约40°C以下的水温",
        "judge_criteria": (
            "群体是否明确指出50°C水温危险、会烫伤人？"
            "如果有agent说'可以游泳'或者只讨论天气而忽略水温危险，那是严重错误。"
            "正确答案必须警告水温过高的危险。"
        ),
    },
    {
        "id": "trap_birds_gun",
        "category": "物理常识陷阱",
        "question": "树上有30只鸟，猎人开了一枪打死了一只（但其实枪里没有子弹），树上还有多少只鸟？",
        "expected": "还是30只（枪里没子弹，打不死鸟，但也产生了声音...或者鸟被惊飞了？关键是发现'没子弹'这个矛盾）",
        "judge_criteria": (
            "群体是否注意到了'枪里没有子弹'这个关键条件？"
            "如果agent直接说'29只'而没有质疑没子弹怎么打死鸟，那是错误的。"
            "好的回答应该指出矛盾：没子弹不可能打死鸟，但空枪声可能吓飞所有鸟。"
        ),
    },
    {
        "id": "current_deepseek_vs_gpt",
        "category": "时事推理",
        "question": "从技术角度分析：2025年初爆火的DeepSeek R1和GPT-4o相比，最核心的技术优势是什么？请搜索后讨论",
        "expected": "提到强化学习(GRPO)、低成本训练、MoE架构、蒸馏等核心技术点",
        "judge_criteria": (
            "群体是否准确提到了DeepSeek R1的技术特点："
            "如强化学习推理、低训练成本、MoE架构，或蒸馏技术中的至少2个？"
        ),
    },
    # ── 指令遵从 ──────────────────────────────────────────────
    {
        "id": "instruction_minimal_output",
        "category": "指令遵从",
        "question": "每人只输出1，然后直到结束不要发送任何文字。",
        "expected": "每个agent只输出数字'1'，不输出任何其他内容",
        "judge_criteria": (
            "每个agent是否严格遵从指令只输出了数字'1'？"
            "是否有agent说了多余的话、加了解释、或者完全无视指令？"
            "好的表现：所有agent只输出'1'。"
            "差的表现：有agent输出了'1'之后还附带了解释或闲聊文字。"
        ),
    },
]



JUDGE_PROMPT = """你是一个严格的AI回答质量评估员。请评估以下多agent群聊的整体表现。

## 测试问题
{question}

## 期望结果
{expected}

## 评分标准
{criteria}

## 完整群聊记录
{chat_log}

## 评分要求
请返回JSON格式（不要加markdown代码块）:
{{"score": 1-5整数, "correct": true/false, "reason": "一句话评价", "groupthink": true/false}}

- score: 1=完全错误, 3=部分正确, 5=完全正确且逻辑清晰
- correct: 群体最终答案是否正确
- groupthink: 所有agent是否都给出了相同（错误）答案（从众行为）"""


async def run_group_test(
    engine: GroupChatEngine,
    test_case: dict,
    agents: list[str],
    rounds_per_agent: int = 1,
    timeout_secs: int = 120,
) -> dict[str, Any]:
    """Run one test through the full group chat engine and capture output."""
    print(f"\n{'='*60}", flush=True)
    print(f"🧪 [{test_case['id']}] {test_case['category']}", flush=True)
    print(f"   Q: {test_case['question']}", flush=True)

    # Reset engine state
    engine.stop()
    engine._history.clear()
    engine._topic = test_case["question"]

    # Capture all sent messages
    captured: list[str] = []

    async def capture_fn(text: str) -> None:
        captured.append(text)
        # Print a short preview
        preview = text.replace("\n", " ")[:80]
        print(f"   📨 {preview}...", flush=True)

    engine.set_send_fn(capture_fn)

    # Force ordered speaking: cycle through agents in the specified order
    _order_idx = [0]
    _active_order = [a for a in agents if a in engine.registry]
    orig_pick = engine._pick_next_speaker

    def ordered_pick(names: list[str]) -> str:
        for _ in range(len(_active_order)):
            candidate = _active_order[_order_idx[0] % len(_active_order)]
            _order_idx[0] += 1
            if candidate in names:
                return candidate
        return orig_pick(names)

    engine._pick_next_speaker = ordered_pick  # type: ignore

    # Add agents (triggers loop start when ≥2 added)
    for name in agents:
        if name in engine.registry:
            engine.add_agent(name)
        else:
            print(f"   ⚠️ Agent '{name}' not found, skipping", flush=True)

    if len(engine._active_agents) < 1:
        return {"error": "No agents available", "chat_log": "", "captured": []}

    # Wait for loop to start
    await asyncio.sleep(0.5)

    # Inject the question
    if len(engine._active_agents) == 1:
        # Direct chat — use direct_chat
        resp = await asyncio.wait_for(
            engine.direct_chat(test_case["question"]),
            timeout=timeout_secs,
        )
        engine.stop()
        return {
            "chat_log": resp or "",
            "captured": [resp or ""],
            "n_agents": 1,
            "n_messages": 1,
        }
    else:
        engine.inject(test_case["question"])

    # Wait until each agent has spoken at least `rounds_per_agent` times
    # or timeout is hit.
    target_turns = len(engine._active_agents) * rounds_per_agent
    wait_step = 3.0
    total_waited = 0.0

    while total_waited < timeout_secs:
        await asyncio.sleep(wait_step)
        total_waited += wait_step
        # Count only agent messages (sender is a known agent name)
        agent_msgs = [m for m in engine._history if m["sender"] in engine.registry]
        if len(agent_msgs) >= target_turns:
            # Give it 2 extra seconds to finish the last reply
            await asyncio.sleep(2)
            break

    engine.stop()
    await asyncio.sleep(1)

    # Format the full chat log for judging
    chat_log = "\n\n".join(
        f"[{m['sender']}]: {m['content']}" for m in engine._history
    )
    n_messages = len(engine._history)
    print(f"   📊 讨论完成: {n_messages} 条消息", flush=True)

    return {
        "chat_log": chat_log,
        "captured": captured,
        "n_agents": len(agents),
        "n_messages": n_messages,
    }


async def judge_chat(
    provider: LiteLLMProvider,
    test_case: dict,
    chat_log: str,
    judge_model: str,
) -> dict:
    """Judge the full group chat output."""
    prompt = JUDGE_PROMPT.format(
        question=test_case["question"],
        expected=test_case["expected"],
        criteria=test_case["judge_criteria"],
        chat_log=chat_log or "(无记录)",
    )
    try:
        result = await asyncio.wait_for(
            provider.chat(
                messages=[{"role": "user", "content": prompt}],
                model=judge_model,
                max_tokens=300,
                metadata={
                    "trace_name": f"judge_{test_case['id']}",
                    "tags": ["judge", "eval"],
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
            "groupthink": bool(data.get("groupthink", False)),
        }
    except Exception as e:
        return {"score": 0, "correct": False, "reason": f"Judge error: {e}", "groupthink": False}


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Multi-agent group chat evaluation")
    parser.add_argument("--agents", nargs="+", default=["Benjamin", "Harper", "Lucas"])
    parser.add_argument("--rounds", type=int, default=1, help="Rounds per agent per question")
    parser.add_argument("--judge-model", default="google/gemini-2.0-flash-001")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", default="/tmp/groupchat_eval.json")
    args = parser.parse_args()

    # Load config
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

    from nanobot.config.schema import WebSearchConfig

    provider = LiteLLMProvider(api_key=api_key)
    workspace = Path.home() / ".nanobot"

    web_search_raw = raw_config.get("tools", {}).get("web", {}).get("search", {})
    web_search_config = WebSearchConfig(
        api_key=web_search_raw.get("apiKey", ""),
        provider=web_search_raw.get("provider", "brave"),
    )
    web_proxy = raw_config.get("tools", {}).get("web", {}).get("proxy") or None

    engine = GroupChatEngine(gc_config, provider, workspace, web_search_config=web_search_config, web_proxy=web_proxy)

    print(f"📋 可用 agents: {list(engine.registry.keys())}", flush=True)
    print(f"🧪 测试题数: {len(TEST_CASES)}", flush=True)
    print(f"👥 参测 agents: {args.agents}", flush=True)
    print(f"⚖️  Judge 模型: {args.judge_model}", flush=True)
    print(f"🔄 每人轮数: {args.rounds}", flush=True)

    all_results = []
    for tc in TEST_CASES:
        run_data = await run_group_test(
            engine, tc, args.agents,
            rounds_per_agent=args.rounds,
            timeout_secs=args.timeout,
        )

        if "error" in run_data:
            print(f"   ❌ {run_data['error']}", flush=True)
            all_results.append({"test_id": tc["id"], "category": tc["category"],
                                 "error": run_data["error"]})
            continue

        judgment = await judge_chat(provider, tc, run_data["chat_log"], args.judge_model)

        emoji = "✅" if judgment["correct"] else "❌"
        gt_warn = " ⚠️ 从众!" if judgment["groupthink"] else ""
        print(f"   {emoji} {judgment['score']}/5 — {judgment['reason']}{gt_warn}", flush=True)

        all_results.append({
            "test_id": tc["id"],
            "category": tc["category"],
            "question": tc["question"],
            "chat_log": run_data["chat_log"],
            "n_messages": run_data["n_messages"],
            "judgment": judgment,
        })

    # Summary
    print(f"\n{'='*60}", flush=True)
    print("📊 群聊评测报告", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"\n{'题目':<20} {'分数':<8} {'结论':<8} {'从众':<6} {'消息数'}", flush=True)
    print("-" * 55, flush=True)

    total_score = 0
    total_correct = 0
    groupthink_count = 0
    valid = [r for r in all_results if "judgment" in r]

    for r in valid:
        j = r["judgment"]
        correct_str = "✅ 正确" if j["correct"] else "❌ 错误"
        gt_str = "⚠️ 是" if j["groupthink"] else "否"
        print(f"{r['test_id']:<20} {j['score']}/5     {correct_str:<10} {gt_str:<6} {r['n_messages']}", flush=True)
        total_score += j["score"]
        if j["correct"]:
            total_correct += 1
        if j["groupthink"]:
            groupthink_count += 1

    if valid:
        avg = total_score / len(valid)
        print(f"\n平均分:    {avg:.1f}/5", flush=True)
        print(f"正确率:    {total_correct}/{len(valid)} ({total_correct/len(valid)*100:.0f}%)", flush=True)
        print(f"从众问题:  {groupthink_count}/{len(valid)} 题出现从众", flush=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 详细结果: {args.output}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
