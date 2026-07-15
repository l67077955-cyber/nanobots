"""
以撒闪避规划器 - 从Eye of Prediction mod移植
核心路径规划和决策算法
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
from enum import Enum
import math

from .room import Vector2, Room
from .player import PlayerState
from .hazards import HazardPrimitive, HazardMode, HazardShape


# 配置参数 (从config.lua移植)
@dataclass
class PlannerConfig:
    """规划器配置"""
    enabled: bool = True
    lookahead_frames: int = 28
    skip_frames_when_safe: int = 3
    acceleration: float = 0.38
    early_accept_score: float = 260.0
    override_score_margin: float = 25.0

    activation_window_frames: int = 15
    proximity_trigger_clearance: float = 36.0

    # 惩罚/奖励
    collision_penalty: float = 12000.0
    near_miss_reward: float = 190.0
    tracking_near_penalty: float = 260.0
    wall_penalty: float = 28.0
    corner_penalty: float = 70.0
    sharp_turn_reward: float = 32.0
    horizontal_alignment_bonus: float = 42.0
    horizontal_alignment_tolerance: float = 30.0
    alignment_safe_clearance: float = 30.0
    future_time_falloff: float = 0.25

    player_radius_fallback: float = 14.0

    # 墙/角落避免
    wall_comfort_px: float = 68.0
    center_bias: float = 75.0
    pocket_penalty: float = 350.0

    # 风筝 (保持与追踪敌人的距离)
    kite_distance: float = 150.0
    kite_pressure_per_px: float = 1.1
    kite_delta_weight: float = 2.2

    # 候选搜索
    gap_candidate_count: int = 3
    cull_margin_px: float = 60.0

    # 调试
    debug_logging: bool = True


# 危险模式代码
HAZARD_MODE_CODES = {
    HazardMode.STATIC: "0",
    HazardMode.STRAIGHT: "1",
    HazardMode.TRACKING: "2",
    HazardMode.SIN: "3",
    HazardMode.ORBIT: "4",
    HazardMode.EXPLOSIVE: "5",
}


@dataclass
class HazardSnapshot:
    """
    危险快照

    预先计算的优化数据结构，用于快速评估路径
    """
    hazards: List[Dict[str, Any]] = field(default_factory=list)
    bodies: List[Dict[str, Any]] = field(default_factory=list)
    body_count: int = 0
    static_count: int = 0
    total_hazards: int = 0
    room_frame: int = 0
    lookahead: int = 28
    px: float = 0.0
    py: float = 0.0
    player_speed: float = 2.4
    player_radius: float = 14.0


@dataclass
class PathEvaluation:
    """路径评估结果"""
    score: float = 0.0
    collision: bool = False
    earliest_hit: Optional[int] = None
    hazard_collision: bool = False
    earliest_hazard_hit: Optional[int] = None
    min_clearance: float = 9999.0
    immediate_hazard_min_clearance: float = 9999.0
    threat: Optional[HazardPrimitive] = None
    threat_point: Optional[Vector2] = None
    threat_class: str = ""


@dataclass
class Decision:
    """闪避决策"""
    direction: Vector2
    score: float
    active: bool
    no_solution: bool = False
    collision: bool = False
    min_clearance: float = 9999.0
    earliest_hit: Optional[int] = None


class DangerBuckets:
    """
    危险角度直方图

    用于快速识别子弹模式中的逃跑空隙
    """

    BUCKET_COUNT = 16

    def __init__(self):
        self.buckets = [0.0] * self.BUCKET_COUNT

    def add_hazard(self, entry: Dict[str, Any], snapshot: HazardSnapshot):
        """添加危险到直方图"""
        dist = entry.get('dist_now', 9999)
        if dist <= 1:
            return

        reach_base = snapshot.player_speed * snapshot.lookahead + snapshot.player_radius
        reach = reach_base + entry.get('speed', 0) * snapshot.lookahead + entry.get('radius', 0) + 40

        urgency = 1 - dist / reach
        if urgency <= 0:
            return

        dx = entry.get('now_x', 0) - snapshot.px
        dy = entry.get('now_y', 0) - snapshot.py

        closing = 0
        speed = entry.get('speed', 0)
        if speed > 0.05:
            closing = -(dx * entry.get('vx', 0) + dy * entry.get('vy', 0)) / (dist * speed)
            closing = max(0, min(1, closing))

        weight = urgency * (0.6 + 0.9 * closing)

        is_line = entry.get('is_line', False)
        if is_line:
            weight *= 0.7

        if weight <= 0.01:
            return

        # 计算角度和桶索引
        angle = math.atan2(dy, dx)
        scaled = (angle + math.pi) / (2 * math.pi) * self.BUCKET_COUNT
        index = int(scaled) % self.BUCKET_COUNT

        # 添加到主桶和相邻桶 (模糊化)
        left_index = (index - 1) % self.BUCKET_COUNT
        right_index = (index + 1) % self.BUCKET_COUNT

        self.buckets[index] += weight
        self.buckets[left_index] += weight * 0.5
        self.buckets[right_index] += weight * 0.5

    def get_safest_directions(self, count: int = 3) -> List[Vector2]:
        """获取最安全的方向"""
        order = sorted(range(self.BUCKET_COUNT), key=lambda i: self.buckets[i])

        directions = []
        for g in range(min(count, self.BUCKET_COUNT)):
            index = order[g]
            angle = ((index + 0.5) / self.BUCKET_COUNT) * (2 * math.pi) - math.pi
            directions.append(Vector2.from_angle(angle))

        return directions


class DodgePlanner:
    """
    闪避规划器

    核心算法：从Lua移植，包含路径模拟、危险检测、决策评分
    """

    def __init__(self, config: Optional[PlannerConfig] = None):
        self.config = config or PlannerConfig()

        # 时间权重缓存
        self._time_weight_cache: Dict[int, List[float]] = {}

        # 危险桶
        self._danger_buckets = DangerBuckets()

        # 决策历史 (用于分析)
        self.decision_history: List[Dict[str, Any]] = []
        self.max_history = 1000

    def get_time_weights(self, frames: int) -> List[float]:
        """获取时间权重 (用于评分衰减)"""
        if frames not in self._time_weight_cache:
            weights = []
            falloff = self.config.future_time_falloff
            for frame in range(1, frames + 1):
                normalized_soon = (frames - frame + 1) / frames
                weight = falloff + (1 - falloff) * normalized_soon * normalized_soon
                weights.append(weight)
            self._time_weight_cache[frames] = weights
        return self._time_weight_cache[frames]

    def build_hazard_snapshot(self,
                               hazards: List[HazardPrimitive],
                               player: PlayerState,
                               room: Room,
                               room_frame: int = 0) -> HazardSnapshot:
        """
        构建危险快照

        预计算优化结构，过滤不可能击中玩家的危险
        """
        snapshot = HazardSnapshot()
        snapshot.room_frame = room_frame
        snapshot.lookahead = self.config.lookahead_frames
        snapshot.px = player.position.x
        snapshot.py = player.position.y
        snapshot.player_speed = player.speed
        snapshot.player_radius = player.radius

        px, py = player.position.x, player.position.y
        player_speed = player.speed
        player_radius = player.radius
        lookahead = self.config.lookahead_frames
        cull_margin = self.config.cull_margin_px
        player_can_fly = player.can_fly

        for prim in hazards:
            # 检查是否忽略飞行
            flags = prim.__dict__.get('flags', {})
            if flags.get('ignoresFlying') and player_can_fly:
                continue

            # 检查激活时间窗口
            start_frame = prim.start_frame
            end_frame = prim.end_frame

            active_from = start_frame - room_frame
            if active_from < 1:
                active_from = 1
            active_to = end_frame - room_frame
            if active_to > lookahead:
                active_to = lookahead

            if active_from > active_to:
                continue

            # 提取危险属性
            mode = prim.mode
            shape = prim.shape
            is_line = shape == HazardShape.LINE
            is_static = mode == HazardMode.STATIC
            is_straight = mode == HazardMode.STRAIGHT
            is_tracking_body = prim.source_type == "npc" and prim.kind == "enemy_body"

            # 位置和速度
            base_pos = prim.position
            hx, hy = base_pos.x, base_pos.y
            vel = prim.velocity
            vx, vy = vel.x, vel.y
            acc = prim.acceleration
            ax, ay = acc.x, acc.y

            speed = math.sqrt(vx * vx + vy * vy)
            if speed < 0.0001:
                speed = prim.speed

            radius = prim.radius
            metadata = prim.__dict__.get('metadata', {})
            growth = metadata.get('growthPerFrame', 0)
            max_radius = metadata.get('maxRadius')

            max_reach_radius = radius
            if growth > 0:
                max_reach_radius = radius + growth * lookahead
                if max_radius is not None and max_reach_radius > max_radius:
                    max_reach_radius = max_radius

            # 计算当前距离和可达范围
            now_x, now_y = hx, hy
            if is_line:
                # 线形状: 计算到线段的最近点
                line_end = metadata.get('lineEnd') or prim.line_end
                if line_end:
                    ex, ey = line_end.x, line_end.y
                else:
                    line_len = metadata.get('lineLength', 0)
                    direction = prim.direction
                    ex = hx + direction.x * line_len
                    ey = hy + direction.y * line_len

                # 投影到线段
                seg_x, seg_y = ex - hx, ey - hy
                length_sq = seg_x * seg_x + seg_y * seg_y
                t = 0
                if length_sq > 0.0001:
                    t = ((px - hx) * seg_x + (py - hy) * seg_y) / length_sq
                    t = max(0, min(1, t))
                now_x = hx + seg_x * t
                now_y = hy + seg_y * t
            else:
                # 点形状: 可能有预计算路径
                samples = metadata.get('pathSamples') or prim.sample_positions
                if samples and len(samples) > 0:
                    sample_x, sample_y = [], []
                    for frame in range(lookahead):
                        idx = min(frame + 1, len(samples) - 1)
                        sample = samples[idx]
                        sample_x.append(sample.x)
                        sample_y.append(sample.y)
                    # 简化处理
                    if sample_x:
                        now_x = sample_x[0]
                        now_y = sample_y[0]

            dx, dy = now_x - px, now_y - py
            dist_now = math.sqrt(dx * dx + dy * dy)

            # 可达范围检查 (性能优化: 过滤远处的危险)
            reach = (speed + player_speed) * lookahead + max_reach_radius + player_radius + cull_margin
            if is_tracking_body:
                reach += (speed + player_speed) * 10

            if dist_now > reach:
                continue

            # 创建快照条目
            entry = {
                'prim': prim,
                'mode': mode,
                'is_line': is_line,
                'is_static': is_static,
                'is_straight': is_straight,
                'is_tracking_body': is_tracking_body,
                'x': hx,
                'y': hy,
                'vx': vx,
                'vy': vy,
                'ax': ax,
                'ay': ay,
                'speed': speed,
                'radius': radius,
                'growth': growth,
                'max_radius': max_radius,
                'base_dt': room_frame - start_frame,
                'active_from': active_from,
                'active_to': active_to,
                'now_x': now_x,
                'now_y': now_y,
                'dist_now': dist_now,
            }

            # 处理线形状
            if is_line:
                line_end = metadata.get('lineEnd') or prim.line_end
                if line_end:
                    entry['ex'] = line_end.x
                    entry['ey'] = line_end.y
                else:
                    line_len = metadata.get('lineLength', 0)
                    direction = prim.direction
                    entry['ex'] = hx + direction.x * line_len
                    entry['ey'] = hy + direction.y * line_len

            snapshot.hazards.append(entry)

            if is_static:
                snapshot.static_count += 1

            if is_tracking_body:
                snapshot.bodies.append(entry)

        snapshot.total_hazards = len(snapshot.hazards)
        snapshot.body_count = len(snapshot.bodies)

        # 计算敌人身体质心 (用于风筝)
        if snapshot.body_count > 0:
            sum_x, sum_y = 0, 0
            for body in snapshot.bodies:
                sum_x += body.get('now_x', 0)
                sum_y += body.get('now_y', 0)
            snapshot.body_centroid_x = sum_x / snapshot.body_count
            snapshot.body_centroid_y = sum_y / snapshot.body_count
            dx = snapshot.body_centroid_x - px
            dy = snapshot.body_centroid_y - py
            snapshot.centroid_dist_now = math.sqrt(dx * dx + dy * dy)

        return snapshot

    def build_danger_buckets(self, snapshot: HazardSnapshot) -> DangerBuckets:
        """构建危险角度直方图"""
        buckets = DangerBuckets()
        for entry in snapshot.hazards:
            buckets.add_hazard(entry, snapshot)
        return buckets

    def evaluate_path(self,
                      path: List[Vector2],
                      snapshot: HazardSnapshot,
                      bounds: Any,
                      candidate_dir: Vector2,
                      raw_input: Optional[Vector2] = None,
                      relax_walls: bool = False,
                      relax_static: bool = False) -> PathEvaluation:
        """
        评估路径的安全性

        Args:
            path: 模拟路径点序列
            snapshot: 危险快照
            bounds: 房间边界
            candidate_dir: 候选方向
            raw_input: 原始输入方向
            relax_walls: 是否放宽墙壁惩罚
            relax_static: 是否放宽静止危险惩罚

        Returns:
            路径评估结果
        """
        n = len(path)
        if n == 0:
            return PathEvaluation()

        weights = self.get_time_weights(n)
        player_radius = snapshot.player_radius

        collision_penalty = self.config.collision_penalty
        near_miss_reward = self.config.near_miss_reward
        tracking_near_penalty = self.config.tracking_near_penalty
        static_penalty_scale = self.config.override_score_margin / 100 if relax_static else 1.0
        static_radius_scale = self.config.override_score_margin / 100 if relax_static else 1.0
        static_min_radius = 6.0
        wall_comfort = self.config.wall_comfort_px
        wall_scale = self.config.wall_penalty * 0.015

        score = 0.0
        collision = False
        earliest_hit = None
        hazard_collision = False
        earliest_hazard_hit = None
        min_clearance = 9999.0
        immediate_min_clearance = 9999.0
        threat_entry = None
        threat_x, threat_y = None, None

        # 逐帧检查
        for frame in range(n):
            pos = path[frame]
            x, y = pos.x, pos.y
            weight = weights[frame]

            # 墙壁碰撞
            if not relax_walls and bounds:
                edge = min(
                    x - bounds.left - player_radius,
                    bounds.right - x - player_radius,
                    y - bounds.top - player_radius,
                    bounds.bottom - y - player_radius
                )
                if edge < 0:
                    score -= collision_penalty * weight
                    collision = True
                    if earliest_hit is None:
                        earliest_hit = frame
                elif edge < wall_comfort:
                    depth = wall_comfort - edge
                    score -= depth * depth * wall_scale * weight

            # 危险碰撞检查
            for entry in snapshot.hazards:
                if frame < entry['active_from'] or frame > entry['active_to']:
                    continue

                # 计算危险位置
                hx, hy = self._get_hazard_xy(entry, frame)

                # 计算距离
                dxh, dyh = x - hx, y - hy
                dist = math.sqrt(dxh * dxh + dyh * dyh)

                # 计算危险半径
                hazard_radius = entry['radius']
                if entry.get('growth', 0) > 0:
                    dt = entry['base_dt'] + frame
                    if dt > 0:
                        hazard_radius += entry['growth'] * dt
                        max_r = entry.get('max_radius')
                        if max_r and hazard_radius > max_r:
                            hazard_radius = max_r

                if relax_static and entry.get('is_static'):
                    hazard_radius *= static_radius_scale
                    hazard_radius = max(static_min_radius, hazard_radius)

                clearance = dist - player_radius - hazard_radius

                if clearance < min_clearance:
                    min_clearance = clearance
                    threat_entry = entry
                    threat_x, threat_y = hx, hy

                if frame < self.config.activation_window_frames:
                    if clearance < immediate_min_clearance:
                        immediate_min_clearance = clearance

                if clearance <= 0:
                    penalty = collision_penalty
                    if relax_static and entry.get('is_static'):
                        penalty *= static_penalty_scale
                    score -= penalty * weight
                    collision = True
                    hazard_collision = True
                    if earliest_hit is None:
                        earliest_hit = frame
                    if earliest_hazard_hit is None:
                        earliest_hazard_hit = frame
                else:
                    # 近距离奖励/惩罚
                    proximity_window = entry['radius'] + 34
                    if proximity_window < 42:
                        proximity_window = 42
                    proximity = 1 - clearance / proximity_window
                    if proximity > 0:
                        if entry.get('is_straight'):
                            # 直线弹: 近距离奖励 (鼓励闪避)
                            score += near_miss_reward * proximity * weight
                        else:
                            # 其他: 近距离惩罚
                            penalty = tracking_near_penalty
                            if relax_static and entry.get('is_static'):
                                penalty *= static_penalty_scale
                            score -= penalty * proximity * weight

        # 后处理: 候选方向评分
        final_pos = path[-1] if path else Vector2(snapshot.px, snapshot.py)

        # 墙壁舒适度
        if not relax_walls and bounds:
            start_edge = min(
                snapshot.px - bounds.left - player_radius,
                bounds.right - snapshot.px - player_radius,
                snapshot.py - bounds.top - player_radius,
                bounds.bottom - snapshot.py - player_radius
            )
            end_edge = min(
                final_pos.x - bounds.left - player_radius,
                bounds.right - final_pos.x - player_radius,
                final_pos.y - bounds.top - player_radius,
                bounds.bottom - final_pos.y - player_radius
            )
            edge_gain = end_edge - start_edge
            if edge_gain > 60:
                edge_gain = 60
            elif edge_gain < -60:
                edge_gain = -60
            score += edge_gain * 2.5

            # 角落惩罚
            sides_close = 0
            if final_pos.x - bounds.left - player_radius < 90:
                sides_close += 1
            if bounds.right - final_pos.x - player_radius < 90:
                sides_close += 1
            if final_pos.y - bounds.top - player_radius < 90:
                sides_close += 1
            if bounds.bottom - final_pos.y - player_radius < 90:
                sides_close += 1

            if sides_close >= 2:
                score -= self.config.corner_penalty * 10
            elif sides_close == 1:
                score -= self.config.corner_penalty * 2

        # 方向评分
        if raw_input and raw_input.length() > 0.05:
            raw_norm = raw_input.normalized()
            cand_norm = candidate_dir.normalized()
            turn_amount = (1 - cand_norm.dot(raw_norm)) * 0.5
            score += turn_amount * self.config.sharp_turn_reward

        return PathEvaluation(
            score=score,
            collision=collision,
            earliest_hit=earliest_hit,
            hazard_collision=hazard_collision,
            earliest_hazard_hit=earliest_hazard_hit,
            min_clearance=min_clearance,
            immediate_hazard_min_clearance=immediate_min_clearance,
            threat=threat_entry.get('prim') if threat_entry else None,
            threat_point=Vector2(threat_x, threat_y) if threat_x is not None else None,
            threat_class=self._hazard_class(threat_entry) if threat_entry else "",
        )

    def _get_hazard_xy(self, entry: Dict, frame: int) -> Tuple[float, float]:
        """计算危险在指定帧的位置"""
        if 'sample_x' in entry and entry['sample_x']:
            idx = min(frame, len(entry['sample_x']) - 1)
            return entry['sample_x'][idx], entry['sample_y'][idx]

        dt = entry['base_dt'] + frame
        half_dt_sq = 0.5 * dt * dt
        return (
            entry['x'] + entry['vx'] * dt + entry['ax'] * half_dt_sq,
            entry['y'] + entry['vy'] * dt + entry['ay'] * half_dt_sq
        )

    def _hazard_class(self, entry: Optional[Dict]) -> str:
        """获取危险类型字符串"""
        if not entry:
            return "unknown"
        return f"{entry.get('mode', 'unknown')}/{entry.get('is_line', False) and 'line' or 'point'}/{entry.get('kind', 'unknown')}"

    def select_best_direction(self,
                               snapshot: HazardSnapshot,
                               player: PlayerState,
                               room: Room,
                               raw_input: Optional[Vector2] = None,
                               intended_eval: Optional[PathEvaluation] = None) -> Decision:
        """
        选择最佳闪避方向

        Args:
            snapshot: 危险快照
            player: 玩家状态
            room: 房间
            raw_input: 原始输入方向
            intended_eval: 意向路径评估 (用于判断是否需要转向)

        Returns:
            最佳决策
        """
        if snapshot.total_hazards == 0:
            # 无危险: 保持当前输入或向中心移动
            if raw_input and raw_input.length() > 0.05:
                return Decision(direction=raw_input.normalized(), score=0, active=False)
            else:
                center_dir = room.bounds.center - player.position
                if center_dir.length() > 1:
                    center_dir = center_dir.normalized()
                else:
                    center_dir = Vector2(0, 0)
                return Decision(direction=center_dir, score=0, active=False)

        # 构建危险桶
        danger_buckets = self.build_danger_buckets(snapshot)

        # 收集候选方向
        candidates: List[Vector2] = []

        # 1. 原始输入
        if raw_input and raw_input.length() > 0.05:
            candidates.append(raw_input.normalized())

        # 2. 向房间中心
        center_dir = room.bounds.center - player.position
        if center_dir.length() > 1:
            candidates.append(center_dir.normalized())

        # 3. 威胁的垂直方向
        if intended_eval and intended_eval.threat_point:
            to_threat = intended_eval.threat_point - player.position
            if to_threat.length() > 0.1:
                threat_dir = to_threat.normalized()
                candidates.append(threat_dir.rotate_90(False))
                candidates.append(threat_dir.rotate_90(True))
                candidates.append(threat_dir * -1)  # 远离威胁

        # 4. 远离敌人质心
        if snapshot.body_count > 0:
            away_bodies = Vector2(
                snapshot.px - snapshot.body_centroid_x,
                snapshot.py - snapshot.body_centroid_y
            )
            if away_bodies.length() > 0.1:
                candidates.append(away_bodies.normalized())

        # 5. 最安全的方向 (从危险桶)
        safe_dirs = danger_buckets.get_safest_directions(self.config.gap_candidate_count)
        candidates.extend(safe_dirs)

        # 去重
        unique_candidates = []
        for c in candidates:
            is_duplicate = False
            for u in unique_candidates:
                if c.dot(u) > 0.96:
                    is_duplicate = True
                    break
            if not is_duplicate and c.length() > 0.05:
                unique_candidates.append(c.normalized())

        # 评估每个候选方向
        best_decision = None
        best_score = float('-inf')
        bounds = room.bounds

        for candidate in unique_candidates:
            # 模拟路径
            path = self._simulate_path(player, candidate, self.config.lookahead_frames)

            # 评估路径
            eval_result = self.evaluate_path(
                path, snapshot, bounds,
                candidate, raw_input
            )

            # 早期接受检查
            if eval_result.score >= self.config.early_accept_score and not eval_result.collision:
                return Decision(
                    direction=candidate,
                    score=eval_result.score,
                    active=True,
                    collision=False,
                    min_clearance=eval_result.min_clearance
                )

            if eval_result.score > best_score:
                best_score = eval_result.score
                best_decision = Decision(
                    direction=candidate,
                    score=eval_result.score,
                    active=not eval_result.collision,
                    collision=eval_result.collision,
                    no_solution=eval_result.collision and eval_result.earliest_hit is not None and eval_result.earliest_hit < 5,
                    min_clearance=eval_result.min_clearance,
                    earliest_hit=eval_result.earliest_hit
                )

        # 如果最佳决策是碰撞，尝试放宽静止危险
        if best_decision and best_decision.collision:
            for candidate in unique_candidates:
                path = self._simulate_path(player, candidate, self.config.lookahead_frames)
                eval_result = self.evaluate_path(
                    path, snapshot, bounds,
                    candidate, raw_input,
                    relax_static=True
                )
                if eval_result.score > best_score:
                    best_score = eval_result.score
                    best_decision = Decision(
                        direction=candidate,
                        score=eval_result.score,
                        active=not eval_result.collision,
                        collision=eval_result.collision,
                        no_solution=False,
                        min_clearance=eval_result.min_clearance,
                        earliest_hit=eval_result.earliest_hit
                    )

        if best_decision is None:
            # 兜底: 向中心移动
            center_dir = room.bounds.center - player.position
            if center_dir.length() > 0.1:
                center_dir = center_dir.normalized()
            else:
                center_dir = Vector2(1, 0)
            return Decision(direction=center_dir, score=0, active=False, no_solution=True)

        return best_decision

    def _simulate_path(self, player: PlayerState, direction: Vector2, frames: int) -> List[Vector2]:
        """模拟玩家路径"""
        path = []
        pos = player.position.copy()
        vel = player.velocity.copy()
        speed = player.speed
        max_speed = speed * 1.25
        accel = self.config.acceleration

        for _ in range(frames):
            # 加速
            target_vel = direction * speed
            vel = vel + (target_vel - vel) * accel

            # 限速
            current_speed = vel.length()
            if current_speed > max_speed:
                vel = vel * (max_speed / current_speed)

            pos = pos + vel
            path.append(pos.copy())

        return path

    def record_decision(self, decision: Decision, context: Dict[str, Any]):
        """记录决策 (用于分析)"""
        record = {
            'decision': decision,
            'context': context,
            'timestamp': len(self.decision_history)
        }
        self.decision_history.append(record)
        if len(self.decision_history) > self.max_history:
            self.decision_history.pop(0)
