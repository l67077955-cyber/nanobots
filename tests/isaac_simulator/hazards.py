"""
以撒危险对象模型 - 子弹、怪、炸弹等
"""

from dataclasses import dataclass, field
from typing import List, Optional, Callable
from enum import Enum
import math
import random

from .room import Vector2, Room


class HazardMode(Enum):
    """危险对象运动模式"""
    STATIC = "static"      # 静止 (地刺、坑等)
    STRAIGHT = "straight"  # 直线运动 (普通子弹)
    TRACKING = "tracking"  # 追踪 (追踪弹、怪身体)
    SIN = "sin"           # 正弦波动 (蠕动子弹)
    ORBIT = "orbit"       # 环绕 (环绕弹)
    EXPLOSIVE = "explosive"  # 爆炸 (炸弹)


class HazardShape(Enum):
    """危险对象形状"""
    POINT = "point"  # 点状
    AREA = "area"    # 区域 (静止危险)
    LINE = "line"   # 线状 (激光)


@dataclass
class HazardPrimitive:
    """
    危险原语 - 单个危险对象的完整描述

    从以撒mod的danger_detection.lua移植的核心概念
    """
    # 基本信息
    hazard_id: str
    kind: str  # 'projectile', 'enemy_body', 'bomb', 'laser', 'effect'
    mode: HazardMode
    shape: HazardShape

    # 位置和运动
    position: Vector2
    velocity: Vector2 = field(default_factory=lambda: Vector2(0, 0))
    acceleration: Vector2 = field(default_factory=lambda: Vector2(0, 0))
    direction: Vector2 = field(default_factory=lambda: Vector2(0, 0))  # 朝向
    speed: float = 0.0

    # 碰撞属性
    radius: float = 10.0
    initial_radius: float = 10.0
    max_radius: Optional[float] = None
    growth_per_frame: float = 0.0

    # 生命周期
    start_frame: int = 0
    end_frame: int = 999999
    hold_frames: int = 0  # 持续时间 (静止危险)

    # 特殊运动参数
    sin_amplitude: float = 0.0  # 正弦振幅
    sin_period_frames: int = 18  # 正弦周期
    orbit_turn_rate: float = 0.0  # 环绕转向率
    orbit_radius: float = 48.0  # 环绕半径

    # 追踪参数
    tracking_strength: float = 0.0  # 追踪强度

    # 爆炸参数
    explosion_radius: float = 54.0
    explosion_delay_frames: int = 24

    # 线形状参数 (激光)
    line_end: Optional[Vector2] = None
    line_length: float = 0.0

    # 预计算路径采样点
    sample_positions: List[Vector2] = field(default_factory=list)

    # 源信息
    source_type: str = "unknown"  # 'projectile', 'npc', 'bomb', 'laser', 'effect'
    source_key: str = ""

    def get_position_at_frame(self, frame: int, target_pos: Optional[Vector2] = None) -> Vector2:
        """
        计算指定帧的位置

        Args:
            frame: 相对于start_frame的帧数
            target_pos: 追踪目标位置 (仅TRACKING模式需要)
        """
        if frame < 0:
            return self.position.copy()

        dt = frame
        half_dt_sq = 0.5 * dt * dt

        if self.mode == HazardMode.STATIC:
            return self.position.copy()

        elif self.mode == HazardMode.STRAIGHT:
            # 直线运动: pos + v*t + 0.5*a*t^2
            return Vector2(
                self.position.x + self.velocity.x * dt + self.acceleration.x * half_dt_sq,
                self.position.y + self.velocity.y * dt + self.acceleration.y * half_dt_sq
            )

        elif self.mode == HazardMode.TRACKING:
            # 追踪运动 - 需要目标位置
            if target_pos is None:
                # 无目标时走直线
                return Vector2(
                    self.position.x + self.velocity.x * dt,
                    self.position.y + self.velocity.y * dt
                )
            # 简化的追踪: 每帧朝目标偏移
            base_pos = Vector2(
                self.position.x + self.velocity.x * dt,
                self.position.y + self.velocity.y * dt
            )
            # 加入追踪偏移 (简化)
            to_target = (target_pos - base_pos).normalized()
            tracking_offset = to_target * (self.tracking_strength * dt)
            return base_pos + tracking_offset

        elif self.mode == HazardMode.SIN:
            # 正弦波动
            # 基础直线运动 + 正弦偏移
            base_pos = Vector2(
                self.position.x + self.velocity.x * dt,
                self.position.y + self.velocity.y * dt
            )
            # 正弦偏移 (垂直于运动方向)
            perp = self.direction.rotate_90(clockwise=False)
            phase = (2 * math.pi * dt) / self.sin_period_frames
            sin_offset = perp * (self.sin_amplitude * math.sin(phase))
            return base_pos + sin_offset

        elif self.mode == HazardMode.ORBIT:
            # 环绕运动
            # 以初始位置为中心旋转
            center = self.position  # 简化处理
            angle = self.orbit_turn_rate * dt
            # 从初始速度方向开始旋转
            rotated = Vector2(
                self.velocity.x * math.cos(angle) - self.velocity.y * math.sin(angle),
                self.velocity.x * math.sin(angle) + self.velocity.y * math.cos(angle)
            )
            return center + rotated.normalized() * self.orbit_radius

        elif self.mode == HazardMode.EXPLOSIVE:
            # 爆炸物 - 抛物线运动到地面然后爆炸
            # 简化: 直线运动到地面高度
            return Vector2(
                self.position.x + self.velocity.x * dt + self.acceleration.x * half_dt_sq,
                self.position.y + self.velocity.y * dt + self.acceleration.y * half_dt_sq
            )

        return self.position.copy()

    def get_radius_at_frame(self, frame: int) -> float:
        """计算指定帧的半径"""
        if self.growth_per_frame <= 0:
            return self.radius

        dt = max(0, frame)
        radius = self.initial_radius + self.growth_per_frame * dt

        if self.max_radius is not None:
            radius = min(radius, self.max_radius)

        return max(0, radius)

    def is_active_at_frame(self, frame: int, room_frame: int = 0) -> bool:
        """检查是否在指定帧激活"""
        absolute_frame = room_frame + frame
        return self.start_frame <= absolute_frame <= self.end_frame

    def get_distance_to_point(self, pos: Vector2, frame: int,
                             target_pos: Optional[Vector2] = None) -> float:
        """计算到指定点的距离"""
        hazard_pos = self.get_position_at_frame(frame, target_pos)

        if self.shape == HazardShape.LINE:
            # 线形状: 计算到线段的最近距离
            if self.line_end is not None:
                return self._distance_to_line_segment(pos, hazard_pos, self.line_end)
            elif self.line_length > 0:
                line_end = hazard_pos + self.direction * self.line_length
                return self._distance_to_line_segment(pos, hazard_pos, line_end)

        # 点形状: 直接计算距离
        return (pos - hazard_pos).length()

    def _distance_to_line_segment(self, point: Vector2,
                                  seg_start: Vector2, seg_end: Vector2) -> float:
        """计算点到线段的距离"""
        segment = seg_end - seg_start
        seg_len_sq = segment.length_squared()

        if seg_len_sq < 0.0001:
            return (point - seg_start).length()

        t = max(0, min(1, (point - seg_start).dot(segment) / seg_len_sq))
        closest = seg_start + segment * t
        return (point - closest).length()


@dataclass
class Projectile:
    """
    子弹实体

    以撒游戏中最常见的危险源
    """
    position: Vector2
    velocity: Vector2
    radius: float = 10.0
    projectile_id: int = 0

    # 运动属性
    mode: HazardMode = HazardMode.STRAIGHT
    acceleration: Vector2 = field(default_factory=lambda: Vector2(0, 0))

    # 特殊属性
    is_homing: bool = False  # 追踪弹
    sin_amplitude: float = 0.0
    sin_period: int = 18

    # 生命周期
    lifetime: int = 200  # 最大存活帧数
    age: int = 0

    # 所属实体
    spawner_entity: Optional[int] = None

    def update(self, dt: float = 1.0, target_pos: Optional[Vector2] = None):
        """更新子弹状态"""
        self.age += 1

        if self.is_homing and target_pos is not None:
            # 追踪逻辑
            to_target = (target_pos - self.position).normalized()
            # 每帧微调速度方向
            self.velocity = (self.velocity + to_target * 0.1).normalized() * self.velocity.length()

        self.position = self.position + self.velocity * dt
        self.velocity = self.velocity + self.acceleration * dt

    def is_expired(self) -> bool:
        return self.age >= self.lifetime

    def to_primitive(self) -> HazardPrimitive:
        """转换为危险原语"""
        mode = self.mode
        if self.is_homing:
            mode = HazardMode.TRACKING

        return HazardPrimitive(
            hazard_id=f"proj_{self.projectile_id}",
            kind="projectile",
            mode=mode,
            shape=HazardShape.POINT,
            position=self.position.copy(),
            velocity=self.velocity.copy(),
            acceleration=self.acceleration.copy(),
            direction=self.velocity.normalized(),
            speed=self.velocity.length(),
            radius=self.radius,
            start_frame=0,
            end_frame=self.lifetime - self.age,
            sin_amplitude=self.sin_amplitude,
            sin_period_frames=self.sin_period,
        )


@dataclass
class Enemy:
    """
    敌人实体

    包含敌人身体碰撞和攻击模式
    """
    position: Vector2
    velocity: Vector2
    radius: float = 14.0
    enemy_type: int = 0
    variant: int = 0
    enemy_id: int = 0

    # 攻击属性
    attack_pattern: str = "random"  # 'random', 'cardinal', 'radial', 'tracking'
    shoot_interval_frames: int = 60
    projectile_speed: float = 6.0
    projectile_radius: float = 10.0
    projectiles_per_shot: int = 1

    # 状态
    age: int = 0
    last_shot_frame: int = 0

    # 运动属性
    can_move: bool = True
    move_speed: float = 2.0
    chase_player: bool = False

    def update(self, player_pos: Vector2, dt: float = 1.0):
        """更新敌人状态"""
        self.age += 1

        if self.can_move:
            if self.chase_player:
                # 追踪玩家
                to_player = (player_pos - self.position).normalized()
                self.velocity = to_player * self.move_speed
            # else: 保持当前速度

            self.position = self.position + self.velocity * dt

    def should_shoot(self) -> bool:
        """检查是否应该射击"""
        return self.age - self.last_shot_frame >= self.shoot_interval_frames

    def generate_projectiles(self, player_pos: Vector2) -> List[Projectile]:
        """生成子弹"""
        if not self.should_shoot():
            return []

        self.last_shot_frame = self.age
        projectiles = []

        if self.attack_pattern == "random":
            for _ in range(self.projectiles_per_shot):
                angle = random.random() * 2 * math.pi  # 随机角度
                velocity = Vector2.from_angle(angle) * self.projectile_speed
                projectiles.append(Projectile(
                    position=self.position.copy(),
                    velocity=velocity,
                    radius=self.projectile_radius,
                    projectile_id=self.enemy_id * 1000 + self.age,
                ))

        elif self.attack_pattern == "cardinal":
            # 四方向射击
            for i in range(4):
                angle = i * math.pi / 2
                velocity = Vector2.from_angle(angle) * self.projectile_speed
                projectiles.append(Projectile(
                    position=self.position.copy(),
                    velocity=velocity,
                    radius=self.projectile_radius,
                    projectile_id=self.enemy_id * 1000 + self.age * 10 + i,
                ))

        elif self.attack_pattern == "radial":
            # 放射状射击
            for i in range(self.projectiles_per_shot):
                angle = i * 2 * math.pi / self.projectiles_per_shot
                velocity = Vector2.from_angle(angle) * self.projectile_speed
                projectiles.append(Projectile(
                    position=self.position.copy(),
                    velocity=velocity,
                    radius=self.projectile_radius,
                    projectile_id=self.enemy_id * 1000 + self.age * 100 + i,
                ))

        elif self.attack_pattern == "tracking":
            # 追踪弹
            to_player = (player_pos - self.position).normalized()
            for i in range(self.projectiles_per_shot):
                # 带角度偏移
                angle_offset = (i - self.projectiles_per_shot // 2) * 0.1
                velocity = Vector2.from_angle(
                    math.atan2(to_player.y, to_player.x) + angle_offset
                ) * self.projectile_speed
                projectiles.append(Projectile(
                    position=self.position.copy(),
                    velocity=velocity,
                    radius=self.projectile_radius,
                    projectile_id=self.enemy_id * 1000 + self.age * 10 + i,
                    is_homing=True,
                ))

        return projectiles

    def to_body_primitive(self) -> HazardPrimitive:
        """转换为身体碰撞原语"""
        return HazardPrimitive(
            hazard_id=f"enemy_body_{self.enemy_id}",
            kind="enemy_body",
            mode=HazardMode.TRACKING if self.chase_player else HazardMode.STATIC,
            shape=HazardShape.POINT,
            position=self.position.copy(),
            velocity=self.velocity.copy(),
            direction=self.velocity.normalized() if self.velocity.length() > 0.1 else Vector2(0, 0),
            speed=self.velocity.length(),
            radius=self.radius,
            source_type="npc",
            source_key=f"{self.enemy_type}:{self.variant}:0",
        )


@dataclass
class Bomb:
    """炸弹"""
    position: Vector2
    explosion_radius: float = 54.0
    explosion_delay_frames: int = 24
    age: int = 0
    bomb_id: int = 0

    def update(self, dt: float = 1.0):
        self.age += 1

    def is_exploding(self) -> bool:
        return self.age >= self.explosion_delay_frames

    def to_primitive(self) -> HazardPrimitive:
        """转换为危险原语"""
        return HazardPrimitive(
            hazard_id=f"bomb_{self.bomb_id}",
            kind="bomb",
            mode=HazardMode.EXPLOSIVE,
            shape=HazardShape.POINT if self.age < self.explosion_delay_frames else HazardShape.AREA,
            position=self.position.copy(),
            radius=self.explosion_radius if self.is_exploding() else 10,
            initial_radius=10,
            max_radius=self.explosion_radius,
            growth_per_frame=self.explosion_radius / 10 if self.is_exploding() else 0,
            start_frame=self.explosion_delay_frames,
            end_frame=self.explosion_delay_frames + 15,
        )
