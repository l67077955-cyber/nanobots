"""
以撒房间模拟器 - 核心房间模型
用于测试闪避率的基础框架
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import math


@dataclass
class Vector2:
    """2D向量"""
    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: 'Vector2') -> 'Vector2':
        return Vector2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: 'Vector2') -> 'Vector2':
        return Vector2(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> 'Vector2':
        return Vector2(self.x * scalar, self.y * scalar)

    def __rmul__(self, scalar: float) -> 'Vector2':
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> 'Vector2':
        if abs(scalar) < 0.0001:
            return Vector2(0, 0)
        return Vector2(self.x / scalar, self.y / scalar)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y)

    def length_squared(self) -> float:
        return self.x * self.x + self.y * self.y

    def normalized(self) -> 'Vector2':
        length = self.length()
        if length < 0.0001:
            return Vector2(0, 0)
        return Vector2(self.x / length, self.y / length)

    def dot(self, other: 'Vector2') -> float:
        return self.x * other.x + self.y * other.y

    def rotate_90(self, clockwise: bool = True) -> 'Vector2':
        """旋转90度"""
        if clockwise:
            return Vector2(self.y, -self.x)
        return Vector2(-self.y, self.x)

    @staticmethod
    def from_angle(angle: float) -> 'Vector2':
        """从角度创建单位向量"""
        return Vector2(math.cos(angle), math.sin(angle))

    def copy(self) -> 'Vector2':
        return Vector2(self.x, self.y)


@dataclass
class RoomBounds:
    """房间边界"""
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center(self) -> Vector2:
        return Vector2(
            (self.left + self.right) / 2,
            (self.top + self.bottom) / 2
        )

    def contains(self, pos: Vector2, margin: float = 0) -> bool:
        """检查位置是否在房间内"""
        return (self.left + margin <= pos.x <= self.right - margin and
                self.top + margin <= pos.y <= self.bottom - margin)

    def clamp(self, pos: Vector2, margin: float = 0) -> Vector2:
        """将位置限制在房间边界内"""
        return Vector2(
            max(self.left + margin, min(self.right - margin, pos.x)),
            max(self.top + margin, min(self.bottom - margin, pos.y))
        )

    @classmethod
    def standard_room(cls, width: float = 472, height: float = 412) -> 'RoomBounds':
        """标准以撒房间尺寸 (游戏内单位)"""
        # 假设房间中心在原点
        return cls(
            left=-width / 2,
            top=-height / 2,
            right=width / 2,
            bottom=height / 2
        )


@dataclass
class GridEntity:
    """网格实体 (石头、坑等)"""
    position: Vector2
    grid_type: str  # 'rock', 'pit', 'spikes', 'poop'
    size: float = 20.0

    def collides_with(self, pos: Vector2, radius: float) -> bool:
        """检查是否与给定位置碰撞"""
        if self.grid_type == 'pit':
            # 坑只在玩家不能飞行时阻挡
            return False  # 简化处理
        dist = (pos - self.position).length()
        return dist < (self.size + radius)


@dataclass
class Room:
    """
    以撒房间模拟器

    模拟房间布局、碰撞检测、边界处理
    """
    bounds: RoomBounds
    grid_entities: List[GridEntity] = field(default_factory=list)

    # 玩家飞行状态
    player_can_fly: bool = False

    # 玩家能否踩碎石头
    player_can_crush_rocks: bool = False

    def is_position_valid(self, pos: Vector2, radius: float = 14) -> bool:
        """检查位置是否有效 (在房间内且不与障碍物碰撞)"""
        # 边界检查
        if not self.bounds.contains(pos, radius):
            return False

        # 网格障碍物检查
        for grid in self.grid_entities:
            if grid.collides_with(pos, radius):
                # 特殊处理
                if grid.grid_type == 'rock' and self.player_can_crush_rocks:
                    continue
                if grid.grid_type == 'pit' and self.player_can_fly:
                    continue
                return False

        return True

    def get_valid_position(self, pos: Vector2, radius: float = 14) -> Vector2:
        """获取有效的最近位置"""
        # 先限制在边界内
        clamped = self.bounds.clamp(pos, radius)

        # 如果有障碍物碰撞，尝试找最近的空位
        if self.is_position_valid(clamped, radius):
            return clamped

        # 简单处理：返回边界内位置
        return clamped

    def check_line_of_sight(self, start: Vector2, end: Vector2,
                           check_walls: bool = True) -> Tuple[bool, Optional[Vector2]]:
        """检查两点之间是否有视线"""
        delta = end - start
        length = delta.length()
        if length < 1:
            return True, None

        steps = int(length / 5) + 1
        direction = delta.normalized()

        for i in range(1, steps):
            check_pos = start + direction * (length * i / steps)
            if check_walls and not self.is_position_valid(check_pos, 1):
                return False, check_pos

        return True, None

    @classmethod
    def create_standard_room(cls,
                            obstacles: List[Tuple[float, float, str]] = None) -> 'Room':
        """
        创建标准房间

        Args:
            obstacles: 障碍物列表 [(x, y, type), ...]
        """
        bounds = RoomBounds.standard_room()
        room = cls(bounds=bounds)

        if obstacles:
            for x, y, grid_type in obstacles:
                room.grid_entities.append(GridEntity(
                    position=Vector2(x, y),
                    grid_type=grid_type
                ))

        return room

    def get_wall_clearance(self, pos: Vector2, radius: float = 14) -> float:
        """获取到最近墙的距离"""
        return min(
            pos.x - self.bounds.left - radius,
            self.bounds.right - pos.x - radius,
            pos.y - self.bounds.top - radius,
            self.bounds.bottom - pos.y - radius
        )

    def get_escape_directions(self, pos: Vector2, radius: float = 14,
                              probe_distance: float = 40) -> int:
        """计算可用的逃跑方向数量"""
        open_count = 0
        # 检查16个方向
        for i in range(16):
            angle = i * math.pi / 8
            direction = Vector2.from_angle(angle)
            test_pos = pos + direction * probe_distance
            if self.is_position_valid(test_pos, radius):
                open_count += 1
        return open_count
