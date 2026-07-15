"""
以撒玩家模型 - 移动、碰撞、状态
"""

from dataclasses import dataclass, field
from typing import Optional, List, Tuple
import math

from .room import Vector2, Room


@dataclass
class PlayerState:
    """
    玩家状态

    模拟以撒中玩家的完整状态
    """
    # 位置和速度
    position: Vector2 = field(default_factory=lambda: Vector2(0, 0))
    velocity: Vector2 = field(default_factory=lambda: Vector2(0, 0))

    # 碰撞属性
    radius: float = 14.0
    size: float = 14.0  # 以撒标准碰撞大小

    # 移动属性
    move_speed: float = 1.0  # 移动速度倍率 (游戏内属性)
    base_speed: float = 2.4  # 基础移动速度 (像素/帧)

    # 特殊状态
    can_fly: bool = False
    can_crush_rocks: bool = False

    # 无敌帧
    invincibility_frames: int = 0

    # 生命值
    hearts: float = 3.0
    max_hearts: float = 6.0
    soul_hearts: float = 0.0

    # 统计
    damage_taken: int = 0
    dodge_count: int = 0
    near_miss_count: int = 0

    @property
    def speed(self) -> float:
        """实际移动速度"""
        return self.base_speed * self.move_speed

    def take_damage(self, amount: float = 1.0) -> bool:
        """受到伤害"""
        if self.invincibility_frames > 0:
            return False

        # 先扣红心
        if self.hearts > 0:
            self.hearts = max(0, self.hearts - amount)
        else:
            # 再扣魂心
            self.soul_hearts = max(0, self.soul_hearts - amount)

        self.invincibility_frames = 60  # 标准无敌时间
        self.damage_taken += 1
        return True

    def update(self, dt: float = 1.0):
        """更新状态"""
        if self.invincibility_frames > 0:
            self.invincibility_frames -= 1

    def is_alive(self) -> bool:
        return self.hearts > 0 or self.soul_hearts > 0

    def copy(self) -> 'PlayerState':
        """创建副本"""
        return PlayerState(
            position=self.position.copy(),
            velocity=self.velocity.copy(),
            radius=self.radius,
            size=self.size,
            move_speed=self.move_speed,
            base_speed=self.base_speed,
            can_fly=self.can_fly,
            can_crush_rocks=self.can_crush_rocks,
            invincibility_frames=self.invincibility_frames,
            hearts=self.hearts,
            max_hearts=self.max_hearts,
            soul_hearts=self.soul_hearts,
            damage_taken=self.damage_taken,
            dodge_count=self.dodge_count,
            near_miss_count=self.near_miss_count,
        )


class PlayerController:
    """
    玩家控制器

    处理输入、移动、碰撞
    """

    def __init__(self, player: PlayerState, room: Room):
        self.player = player
        self.room = room

        # 更新房间的玩家状态
        room.player_can_fly = player.can_fly
        room.player_can_crush_rocks = player.can_crush_rocks

        # 移动参数 (从以撒mod移植)
        self.acceleration = 0.38
        self.friction = 0.85

        # 历史轨迹 (用于可视化)
        self.position_history: List[Vector2] = []
        self.max_history = 60

    def move(self, direction: Vector2, dt: float = 1.0) -> Vector2:
        """
        处理移动输入

        Args:
            direction: 归一化的输入方向
            dt: 时间步长

        Returns:
            实际移动的距离向量
        """
        if direction.length() < 0.01:
            # 无输入时减速
            self.player.velocity = self.player.velocity * self.friction
            if self.player.velocity.length() < 0.1:
                self.player.velocity = Vector2(0, 0)
        else:
            # 有输入时加速
            target_velocity = direction * self.player.speed
            # 平滑加速
            self.player.velocity = self.player.velocity + (target_velocity - self.player.velocity) * self.acceleration

            # 速度上限
            speed = self.player.velocity.length()
            max_speed = self.player.speed * 1.25
            if speed > max_speed:
                self.player.velocity = self.player.velocity * (max_speed / speed)

        # 计算新位置
        new_pos = self.player.position + self.player.velocity * dt

        # 碰撞检测
        if self.room.is_position_valid(new_pos, self.player.radius):
            self.player.position = new_pos
        else:
            # 尝试沿墙滑动
            # X方向
            test_x = Vector2(new_pos.x, self.player.position.y)
            if self.room.is_position_valid(test_x, self.player.radius):
                self.player.position.x = new_pos.x
                self.player.velocity.y *= 0.5
            # Y方向
            test_y = Vector2(self.player.position.x, new_pos.y)
            if self.room.is_position_valid(test_y, self.player.radius):
                self.player.position.y = new_pos.y
                self.player.velocity.x *= 0.5

            # 如果都不行，停止
            if not self.room.is_position_valid(test_x, self.player.radius) and \
               not self.room.is_position_valid(test_y, self.player.radius):
                self.player.velocity = Vector2(0, 0)

        # 记录历史
        self.position_history.append(self.player.position.copy())
        if len(self.position_history) > self.max_history:
            self.position_history.pop(0)

        return self.player.velocity * dt

    def simulate_path(self, direction: Vector2, frames: int) -> List[Vector2]:
        """
        模拟沿给定方向移动的路径

        用于闪避规划器评估候选方向

        Args:
            direction: 移动方向 (归一化)
            frames: 模拟帧数

        Returns:
            预测的位置序列
        """
        path = []
        pos = self.player.position.copy()
        vel = self.player.velocity.copy()
        speed = self.player.speed
        max_speed = speed * 1.25

        for _ in range(frames):
            # 加速
            target_vel = direction * speed
            vel = vel + (target_vel - vel) * self.acceleration

            # 限速
            current_speed = vel.length()
            if current_speed > max_speed:
                vel = vel * (max_speed / current_speed)

            # 移动
            new_pos = pos + vel

            # 碰撞检测 (简化版)
            if self.room.is_position_valid(new_pos, self.player.radius):
                pos = new_pos
            else:
                # 停止
                break

            path.append(pos.copy())

        return path

    def get_escape_directions(self, probe_distance: float = 40) -> List[Tuple[Vector2, int]]:
        """
        获取可用的逃跑方向

        Returns:
            [(方向, 开放度)] 列表
        """
        directions = []
        for i in range(16):
            angle = i * math.pi / 8
            direction = Vector2.from_angle(angle)
            test_pos = self.player.position + direction * probe_distance
            if self.room.is_position_valid(test_pos, self.player.radius):
                # 计算该方向的开放度
                open_count = 0
                for j in range(8):
                    sub_angle = j * math.pi / 4
                    sub_dir = Vector2.from_angle(angle + sub_angle * 0.2)
                    sub_pos = test_pos + sub_dir * 20
                    if self.room.is_position_valid(sub_pos, self.player.radius):
                        open_count += 1
                directions.append((direction, open_count))

        return directions

    def reset(self, position: Optional[Vector2] = None):
        """重置玩家状态"""
        if position:
            self.player.position = position.copy()
        self.player.velocity = Vector2(0, 0)
        self.player.invincibility_frames = 0
        self.position_history.clear()
