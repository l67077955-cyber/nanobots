"""
以撒闪避模拟器
用于测试和分析Eye of Prediction mod的闪避逻辑

包含:
- room.py: 房间模型
- player.py: 玩家模型
- hazards.py: 危险对象模型 (子弹、敌人等)
- dodge_planner.py: 闪避规划器 (从Lua移植)
- simulator.py: 模拟器主入口
"""

from .room import Vector2, Room, RoomBounds, GridEntity
from .player import PlayerState, PlayerController
from .hazards import HazardPrimitive, HazardMode, HazardShape, Projectile, Enemy, Bomb
from .dodge_planner import DodgePlanner, PlannerConfig, Decision
from .simulator import (
    Simulation,
    SimulationConfig,
    SimulationResult,
    run_simulation,
    run_batch,
    save_result,
    generate_report
)

__all__ = [
    # Room
    'Vector2',
    'Room',
    'RoomBounds',
    'GridEntity',

    # Player
    'PlayerState',
    'PlayerController',

    # Hazards
    'HazardPrimitive',
    'HazardMode',
    'HazardShape',
    'Projectile',
    'Enemy',
    'Bomb',

    # Planner
    'DodgePlanner',
    'PlannerConfig',
    'Decision',

    # Simulator
    'Simulation',
    'SimulationConfig',
    'SimulationResult',
    'run_simulation',
    'run_batch',
    'save_result',
    'generate_report',
]

__version__ = '0.1.0'
