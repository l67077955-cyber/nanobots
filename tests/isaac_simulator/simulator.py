"""
以撒闪避模拟器主入口
用于测试和分析闪避率
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple
import random
import math
import json
from pathlib import Path

from .room import Room, Vector2, RoomBounds, GridEntity
from .player import PlayerState, PlayerController
from .hazards import Projectile, Enemy, HazardPrimitive, HazardMode
from .dodge_planner import DodgePlanner, PlannerConfig, Decision


@dataclass
class SimulationConfig:
    """模拟配置"""
    # 基础设置
    total_frames: int = 3600  # 总模拟帧数 (60秒 @ 60fps)
    seed: Optional[int] = None  # 随机种子

    # 房间设置
    room_width: float = 472
    room_height: float = 412

    # 玩家设置
    player_move_speed: float = 1.0
    player_can_fly: bool = False

    # 敌人设置
    num_enemies: int = 3
    enemy_types: List[str] = field(default_factory=lambda: ["basic", "shooter"])

    # 子弹设置
    projectile_speed: float = 6.0
    projectile_radius: float = 10.0
    max_projectiles: int = 100

    # 规划器设置
    planner_enabled: bool = True
    planner_config: Optional[PlannerConfig] = None

    # 调试设置
    record_history: bool = True
    history_interval: int = 1  # 记录间隔 (帧)


@dataclass
class FrameResult:
    """单帧结果"""
    frame: int
    player_pos: Vector2
    player_velocity: Vector2
    player_health: float

    # 危险统计
    num_hazards: int
    num_projectiles: int
    num_enemies: int

    # 决策信息
    decision_active: bool
    decision_direction: Optional[Vector2]
    decision_score: float
    decision_min_clearance: float

    # 碰撞信息
    collision_occurred: bool
    near_miss_occurred: bool
    near_miss_distance: float


@dataclass
class SimulationResult:
    """模拟结果"""
    config: SimulationConfig
    total_frames: int
    frames: List[FrameResult]

    # 统计数据
    damage_taken: int
    near_misses: int
    total_collisions: int
    total_frames_with_danger: int

    # 闪避率计算
    dodge_rate: float  # 总闪避率
    near_miss_rate: float  # 近距离擦过率
    damage_rate: float  # 受伤率

    # 分析数据
    decision_stats: Dict[str, Any]
    failure_analysis: List[Dict[str, Any]]


class Simulation:
    """
    以撒闪避模拟器

    模拟房间、玩家、敌人、子弹的交互，测试闪避规划器的效果
    """

    def __init__(self, config: SimulationConfig):
        self.config = config
        self.rng = random.Random(config.seed)

        # 创建房间
        self.room = Room(
            bounds=RoomBounds.standard_room(config.room_width, config.room_height)
        )

        # 创建玩家
        self.player = PlayerState(
            position=Vector2(0, 0),  # 房间中心
            move_speed=config.player_move_speed,
            can_fly=config.player_can_fly
        )
        self.player_controller = PlayerController(self.player, self.room)

        # 创建规划器
        planner_config = config.planner_config or PlannerConfig()
        self.planner = DodgePlanner(planner_config)

        # 实体列表
        self.enemies: List[Enemy] = []
        self.projectiles: List[Projectile] = []

        # 状态
        self.current_frame = 0
        self.hazards: List[HazardPrimitive] = []

        # 结果记录
        self.frame_results: List[FrameResult] = []
        self.damage_events: List[Dict[str, Any]] = []
        self.near_miss_events: List[Dict[str, Any]] = []

    def setup(self):
        """初始化模拟"""
        # 重置状态
        self.current_frame = 0
        self.projectiles.clear()
        self.hazards.clear()
        self.frame_results.clear()
        self.damage_events.clear()
        self.near_miss_events.clear()

        # 重置玩家
        self.player.position = self.room.bounds.center.copy()
        self.player.velocity = Vector2(0, 0)
        self.player.hearts = 3.0
        self.player.damage_taken = 0
        self.player.dodge_count = 0
        self.player.near_miss_count = 0

        # 创建敌人
        self.enemies.clear()
        for i in range(self.config.num_enemies):
            enemy = self._create_enemy(i)
            self.enemies.append(enemy)

    def _create_enemy(self, index: int) -> Enemy:
        """创建敌人"""
        # 在房间边缘随机位置生成
        angle = self.rng.random() * 2 * math.pi
        radius = min(self.config.room_width, self.config.room_height) * 0.35
        pos = Vector2(
            math.cos(angle) * radius,
            math.sin(angle) * radius
        )

        # 随机类型
        enemy_type = self.rng.choice(self.config.enemy_types) if self.config.enemy_types else "basic"

        # 攻击模式
        if enemy_type == "shooter":
            attack_pattern = self.rng.choice(["cardinal", "radial", "tracking"])
            shoot_interval = self.rng.randint(40, 80)
        else:
            attack_pattern = "random"
            shoot_interval = 120

        return Enemy(
            position=pos,
            velocity=Vector2(0, 0),
            radius=14.0,
            enemy_type=0 if enemy_type == "basic" else 1,
            enemy_id=index,
            attack_pattern=attack_pattern,
            shoot_interval_frames=shoot_interval,
            projectile_speed=self.config.projectile_speed,
            projectile_radius=self.config.projectile_radius,
            projectiles_per_shot=self.rng.randint(1, 8) if attack_pattern == "radial" else 1,
            can_move=enemy_type == "basic",
            move_speed=1.5,
            chase_player=enemy_type == "basic"
        )

    def step(self) -> FrameResult:
        """执行一帧模拟"""
        frame = self.current_frame

        # 1. 更新敌人
        for enemy in self.enemies:
            enemy.update(self.player.position)

            # 敌人射击
            new_projectiles = enemy.generate_projectiles(self.player.position)
            for proj in new_projectiles:
                if len(self.projectiles) < self.config.max_projectiles:
                    self.projectiles.append(proj)

            # 敌人身体碰撞检测
            body_prim = enemy.to_body_primitive()
            if body_prim:
                dist = (self.player.position - enemy.position).length()
                if dist < self.player.radius + enemy.radius:
                    self._handle_collision("enemy_body", enemy, frame)

        # 2. 更新子弹
        self.hazards.clear()
        for proj in self.projectiles[:]:  # 使用切片遍历，因为会修改列表
            proj.update(1.0, self.player.position)

            # 子弹碰撞检测
            dist = (self.player.position - proj.position).length()
            if dist < self.player.radius + proj.radius:
                self._handle_collision("projectile", proj, frame)
                self.projectiles.remove(proj)
                continue

            # 子弹出界
            if not self.room.bounds.contains(proj.position, 50):
                self.projectiles.remove(proj)
                continue

            # 添加到危险列表
            self.hazards.append(proj.to_primitive())

            # 近距离检测 (擦过)
            near_distance = self.player.radius + proj.radius + 5
            if dist < near_distance:
                self._record_near_miss(proj, frame, dist)

        # 3. 添加敌人身体危险
        for enemy in self.enemies:
            body_prim = enemy.to_body_primitive()
            if body_prim:
                self.hazards.append(body_prim)

        # 4. 闪避规划
        decision = None
        if self.config.planner_enabled and self.hazards:
            snapshot = self.planner.build_hazard_snapshot(
                self.hazards, self.player, self.room, frame
            )
            decision = self.planner.select_best_direction(
                snapshot, self.player, self.room
            )

        # 5. 执行移动
        if decision and decision.active:
            direction = decision.direction
        else:
            # 模拟玩家输入 (简化: 随机移动或向中心)
            if self.rng.random() < 0.3:
                direction = Vector2(0, 0)  # 停止
            else:
                # 随机方向
                angle = self.rng.random() * 2 * math.pi
                direction = Vector2.from_angle(angle)

        self.player_controller.move(direction)

        # 6. 更新玩家状态
        self.player.update()

        # 7. 记录结果
        result = FrameResult(
            frame=frame,
            player_pos=self.player.position.copy(),
            player_velocity=self.player.velocity.copy(),
            player_health=self.player.hearts,

            num_hazards=len(self.hazards),
            num_projectiles=len(self.projectiles),
            num_enemies=len(self.enemies),

            decision_active=decision.active if decision else False,
            decision_direction=decision.direction if decision else None,
            decision_score=decision.score if decision else 0,
            decision_min_clearance=decision.min_clearance if decision else 9999,

            collision_occurred=False,  # 会在 handle_collision 中设置
            near_miss_occurred=False,
            near_miss_distance=9999
        )

        if self.config.record_history and frame % self.config.history_interval == 0:
            self.frame_results.append(result)

        self.current_frame += 1
        return result

    def _handle_collision(self, collision_type: str, entity: Any, frame: int):
        """处理碰撞"""
        if self.player.invincibility_frames > 0:
            return

        if self.player.take_damage():
            self.damage_events.append({
                'frame': frame,
                'type': collision_type,
                'entity_id': getattr(entity, 'projectile_id', getattr(entity, 'enemy_id', 0)),
                'player_pos': self.player.position.copy(),
                'entity_pos': entity.position.copy()
            })

    def _record_near_miss(self, proj: Projectile, frame: int, distance: float):
        """记录近距离擦过"""
        self.player.near_miss_count += 1
        self.near_miss_events.append({
            'frame': frame,
            'projectile_id': proj.projectile_id,
            'distance': distance,
            'player_pos': self.player.position.copy(),
            'projectile_pos': proj.position.copy()
        })

    def run(self) -> SimulationResult:
        """运行完整模拟"""
        self.setup()

        for _ in range(self.config.total_frames):
            self.step()

            # 检查玩家是否死亡
            if not self.player.is_alive():
                break

        return self._compile_result()

    def _compile_result(self) -> SimulationResult:
        """编译结果"""
        total_frames_with_danger = sum(
            1 for f in self.frame_results if f.num_hazards > 0
        )

        # 计算闪避率
        if total_frames_with_danger > 0:
            # 闪避率 = (危险帧数 - 受伤次数) / 危险帧数
            dodge_rate = 1 - (self.player.damage_taken / total_frames_with_danger * 0.1)
            dodge_rate = max(0, min(1, dodge_rate))
        else:
            dodge_rate = 1.0

        # 近距离擦过率
        if self.player.near_miss_count > 0:
            near_miss_rate = self.player.near_miss_count / max(1, total_frames_with_danger) * 100
        else:
            near_miss_rate = 0.0

        # 决策统计
        decision_stats = self._analyze_decisions()

        # 失败分析
        failure_analysis = self._analyze_failures()

        return SimulationResult(
            config=self.config,
            total_frames=self.current_frame,
            frames=self.frame_results,

            damage_taken=self.player.damage_taken,
            near_misses=self.player.near_miss_count,
            total_collisions=len(self.damage_events),
            total_frames_with_danger=total_frames_with_danger,

            dodge_rate=dodge_rate,
            near_miss_rate=near_miss_rate,
            damage_rate=self.player.damage_taken / max(1, self.current_frame / 60),

            decision_stats=decision_stats,
            failure_analysis=failure_analysis
        )

    def _analyze_decisions(self) -> Dict[str, Any]:
        """分析决策模式"""
        active_count = sum(1 for f in self.frame_results if f.decision_active)
        total_with_hazard = sum(1 for f in self.frame_results if f.num_hazards > 0)

        avg_score = 0
        avg_clearance = 0
        if active_count > 0:
            scores = [f.decision_score for f in self.frame_results if f.decision_active]
            clearances = [f.decision_min_clearance for f in self.frame_results if f.decision_active]
            avg_score = sum(scores) / len(scores)
            avg_clearance = sum(clearances) / len(clearances)

        return {
            'activation_rate': active_count / max(1, total_with_hazard),
            'avg_score': avg_score,
            'avg_min_clearance': avg_clearance,
            'total_activations': active_count
        }

    def _analyze_failures(self) -> List[Dict[str, Any]]:
        """分析失败案例"""
        failures = []

        for event in self.damage_events:
            frame = event['frame']

            # 找到对应帧的结果
            frame_result = None
            for f in self.frame_results:
                if f.frame == frame:
                    frame_result = f
                    break

            if frame_result:
                failures.append({
                    'frame': frame,
                    'type': event['type'],
                    'hazards_at_collision': frame_result.num_hazards,
                    'decision_active': frame_result.decision_active,
                    'decision_score': frame_result.decision_score,
                    'min_clearance': frame_result.decision_min_clearance,
                    'player_pos': {
                        'x': event['player_pos'].x,
                        'y': event['player_pos'].y
                    },
                    'entity_pos': {
                        'x': event['entity_pos'].x,
                        'y': event['entity_pos'].y
                    }
                })

        return failures


def run_simulation(config: Optional[SimulationConfig] = None) -> SimulationResult:
    """运行单个模拟"""
    config = config or SimulationConfig()
    sim = Simulation(config)
    return sim.run()


def run_batch(configs: List[SimulationConfig]) -> List[SimulationResult]:
    """批量运行模拟"""
    results = []
    for config in configs:
        sim = Simulation(config)
        results.append(sim.run())
    return results


def save_result(result: SimulationResult, path: str):
    """保存结果到文件"""
    data = {
        'config': {
            'total_frames': result.config.total_frames,
            'seed': result.config.seed,
            'num_enemies': result.config.num_enemies,
            'planner_enabled': result.config.planner_enabled,
        },
        'summary': {
            'total_frames': result.total_frames,
            'damage_taken': result.damage_taken,
            'near_misses': result.near_misses,
            'dodge_rate': result.dodge_rate,
            'near_miss_rate': result.near_miss_rate,
        },
        'decision_stats': result.decision_stats,
        'failure_analysis': result.failure_analysis,
    }

    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)


def generate_report(result: SimulationResult) -> str:
    """生成分析报告"""
    lines = [
        "=" * 60,
        "以撒闪避模拟器 - 分析报告",
        "=" * 60,
        "",
        "## 配置",
        f"- 总帧数: {result.config.total_frames}",
        f"- 敌人数量: {result.config.num_enemies}",
        f"- 规划器: {'启用' if result.config.planner_enabled else '禁用'}",
        "",
        "## 统计结果",
        f"- 总运行帧数: {result.total_frames}",
        f"- 受伤次数: {result.damage_taken}",
        f"- 近距离擦过: {result.near_misses}",
        f"- 危险帧数: {result.total_frames_with_danger}",
        "",
        "## 闪避率",
        f"- 总闪避率: {result.dodge_rate:.2%}",
        f"- 近距离擦过率: {result.near_miss_rate:.2f}/帧",
        "",
        "## 决策分析",
        f"- 激活率: {result.decision_stats.get('activation_rate', 0):.2%}",
        f"- 平均得分: {result.decision_stats.get('avg_score', 0):.1f}",
        f"- 平均最小间隙: {result.decision_stats.get('avg_min_clearance', 0):.1f}px",
        "",
    ]

    if result.failure_analysis:
        lines.append("## 失败案例分析")
        for i, failure in enumerate(result.failure_analysis[:10]):  # 最多显示10个
            lines.extend([
                f"",
                f"### 失败 #{i + 1} (帧 {failure['frame']})",
                f"- 碰撞类型: {failure['type']}",
                f"- 当时危险数: {failure['hazards_at_collision']}",
                f"- 规划器激活: {failure['decision_active']}",
                f"- 规划得分: {failure['decision_score']:.1f}",
                f"- 最小间隙: {failure['min_clearance']:.1f}px",
            ])

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)
