#!/usr/bin/env python3
"""
以撒闪避模拟器 - 测试脚本
运行模拟并生成分析报告
"""

import sys
import argparse
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.isaac_simulator import (
    SimulationConfig,
    PlannerConfig,
    run_simulation,
    run_batch,
    save_result,
    generate_report,
)


def test_basic_dodge():
    """基础闪避测试"""
    print("=" * 50)
    print("测试1: 基础闪避 (启用规划器)")
    print("=" * 50)

    config = SimulationConfig(
        total_frames=1800,  # 30秒
        num_enemies=2,
        planner_enabled=True,
        record_history=True,
    )

    result = run_simulation(config)
    print(generate_report(result))
    return result


def test_no_planner():
    """无规划器对比测试"""
    print("=" * 50)
    print("测试2: 无规划器对比")
    print("=" * 50)

    config = SimulationConfig(
        total_frames=1800,
        num_enemies=2,
        planner_enabled=False,
        record_history=True,
    )

    result = run_simulation(config)
    print(generate_report(result))
    return result


def test_dense_projectiles():
    """密集弹幕测试"""
    print("=" * 50)
    print("测试3: 密集弹幕 (4个射击怪)")
    print("=" * 50)

    config = SimulationConfig(
        total_frames=1800,
        num_enemies=4,
        enemy_types=["shooter", "shooter"],
        planner_enabled=True,
        record_history=True,
    )

    planner_config = PlannerConfig(
        lookahead_frames=32,
        collision_penalty=15000,
        near_miss_reward=250,
    )
    config.planner_config = planner_config

    result = run_simulation(config)
    print(generate_report(result))
    return result


def test_tracking_enemies():
    """追踪敌人测试"""
    print("=" * 50)
    print("测试4: 追踪敌人")
    print("=" * 50)

    config = SimulationConfig(
        total_frames=1800,
        num_enemies=3,
        enemy_types=["basic"],  # 基础敌人会追踪玩家
        planner_enabled=True,
        record_history=True,
    )

    planner_config = PlannerConfig(
        kite_distance=120,
        kite_pressure_per_px=1.5,
    )
    config.planner_config = planner_config

    result = run_simulation(config)
    print(generate_report(result))
    return result


def test_comparison():
    """对比测试: 规划器 vs 无规划器"""
    print("=" * 50)
    print("测试5: 对比测试")
    print("=" * 50)

    configs = [
        SimulationConfig(
            total_frames=1800,
            num_enemies=3,
            planner_enabled=True,
            seed=42,
        ),
        SimulationConfig(
            total_frames=1800,
            num_enemies=3,
            planner_enabled=False,
            seed=42,  # 相同种子保证公平对比
        ),
    ]

    results = run_batch(configs)

    print("\n对比结果:")
    print("-" * 40)
    print(f"{'指标':<20} {'规划器':>10} {'无规划器':>10}")
    print("-" * 40)
    print(f"{'闪避率':<20} {results[0].dodge_rate:>10.1%} {results[1].dodge_rate:>10.1%}")
    print(f"{'受伤次数':<20} {results[0].damage_taken:>10} {results[1].damage_taken:>10}")
    print(f"{'近距离擦过':<20} {results[0].near_misses:>10} {results[1].near_misses:>10}")

    return results


def analyze_failure_patterns(result):
    """分析失败模式"""
    print("\n" + "=" * 50)
    print("失败模式分析")
    print("=" * 50)

    if not result.failure_analysis:
        print("无失败案例")
        return

    # 统计失败类型
    type_counts = {}
    for failure in result.failure_analysis:
        t = failure['type']
        type_counts[t] = type_counts.get(t, 0) + 1

    print("\n失败类型分布:")
    for t, count in type_counts.items():
        print(f"  - {t}: {count}")

    # 分析规划器激活状态
    active_when_failed = sum(1 for f in result.failure_analysis if f['decision_active'])
    print(f"\n规划器激活时失败: {active_when_failed}/{len(result.failure_analysis)}")

    # 分析最小间隙
    clearances = [f['min_clearance'] for f in result.failure_analysis if f['min_clearance'] < 100]
    if clearances:
        avg_clearance = sum(clearances) / len(clearances)
        print(f"平均最小间隙: {avg_clearance:.1f}px")


def main():
    parser = argparse.ArgumentParser(description='以撒闪避模拟器测试')
    parser.add_argument('--test', '-t', type=str, default='all',
                        choices=['all', 'basic', 'no_planner', 'dense', 'tracking', 'compare'],
                        help='运行特定测试')
    parser.add_argument('--frames', '-f', type=int, default=1800,
                        help='模拟帧数')
    parser.add_argument('--enemies', '-e', type=int, default=2,
                        help='敌人数量')
    parser.add_argument('--seed', '-s', type=int, default=None,
                        help='随机种子')
    parser.add_argument('--output', '-o', type=str, default=None,
                        help='输出文件路径')
    args = parser.parse_args()

    results = []

    if args.test == 'all':
        results.append(test_basic_dodge())
        results.append(test_no_planner())
        results.append(test_dense_projectiles())
        results.append(test_tracking_enemies())
        results.extend(test_comparison())
    elif args.test == 'basic':
        results.append(test_basic_dodge())
    elif args.test == 'no_planner':
        results.append(test_no_planner())
    elif args.test == 'dense':
        results.append(test_dense_projectiles())
    elif args.test == 'tracking':
        results.append(test_tracking_enemies())
    elif args.test == 'compare':
        results.extend(test_comparison())

    # 分析失败模式
    for r in results:
        if hasattr(r, 'failure_analysis') and r.failure_analysis:
            analyze_failure_patterns(r)

    # 保存结果
    if args.output and results:
        save_result(results[0], args.output)
        print(f"\n结果已保存到: {args.output}")


if __name__ == '__main__':
    main()
