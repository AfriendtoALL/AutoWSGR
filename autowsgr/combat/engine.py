"""战斗引擎 — 状态机主循环。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from autowsgr.infra.logger import get_logger
from autowsgr.types import ConditionFlag, Formation, ShipDamageState

from .actions import click_speed_up, dismiss_resource_confirm
from .handlers import PhaseHandlersMixin
from .history import CombatHistory, CombatResult
from .node_tracker import MapNodeData, NodeTracker
from .plan import MODE_CATEGORIES, CombatMode, CombatPlan, NodeDecision
from .recognizer import (
    CombatRecognitionTimeoutError,
    CombatRecognizer,
    CombatStopRequestedError,
)
from .state import CombatPhase, ModeCategory, resolve_successors


if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

    from autowsgr.context import GameContext


_log = get_logger('combat')
_log_sm = get_logger('combat.engine')  # 状态机转移专用，可单独静默


# ═══════════════════════════════════════════════════════════════════════════════
# 熔断护栏
#
# 状态机的三种"卡死"形态，都必须能被确定性终止，不能靠 while 自身的
# 退出条件碰运气：
#   1. 识别超时 → 恢复成功 → 继续后又超时  →  恢复死循环
#   2. 识别始终成功 → 但页面不再推进     →  步数死循环
#   3. 节点追踪失败 → 反复撤退           →  由步数上限兜底
#
# Alas 的对应机制是 device.py 里的 stuck_timer（60s/180s 双计时器，
# 超时打印完整调用栈再 raise GameStuckError）。此处移植其**思路**：
#   - ``max_recovery_attempts`` 封住 (1)
#   - ``max_steps`` 封住 (2)(3)
#   - 触发时 log 里带上节点/阶段/步数，便于事后定位
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MAX_RECOVERY_ATTEMPTS = 3
"""连续恢复的最大次数，超过则判 SL 重开整局。

取 3 的依据：单次超时的典型值是3~30s（START_FIGHT 仅 3s、PROCEED 7.5s、
RESULT 90s、NIGHT_PROMPT 150s），三次恢复 ≈ 最多约 1 分钟额外开销，
换��确定退出，代价可接受。
"""

DEFAULT_MAX_STEPS = 200
"""单场战斗的最大状态机步数。

正常一次节点战斗约 8~14 步（行军→战况→索敌→阵型→导弹→战斗→夜战→战果→
经验→掉落→前进）。``selected_nodes`` 有3 点时约 40 步，留 5 倍余量到 200。
超限说明页面反复横跳或节点判定失效，属异常而非正常流程。
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 战斗引擎
# ═══════════════════════════════════════════════════════════════════════════════


class CombatEngine(PhaseHandlersMixin):
    """自包含的战斗状态机引擎。

    引擎内部自行完成图像匹配、敌方编成/阵型识别、战果等级检测等，
    无需外部注入回调函数，沿袭旧代码 ``Timer`` 内置识别能力的设计。

    Parameters
    ----------
    device:
        设备控制器 (截图 + 触控)。
    ocr:
        OCR 引擎实例 (阵型识别用)。可选，为 ``None`` 则跳过阵型识别。
    max_recovery_attempts:
        单场战斗内允许的连续恢复次数上限，超过则判 SL。默认
        :data:`DEFAULT_MAX_RECOVERY_ATTEMPTS`。
    max_steps:
        单场战斗内允许的状态机步数上限。默认 :data:`DEFAULT_MAX_STEPS`。
    """

    def __init__(
        self,
        ctx: GameContext,
        *,
        max_recovery_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self._ctx = ctx
        self._device = ctx.ctrl
        self._ocr = ctx.ocr

        # 熔断护栏 (可在构造时覆盖)
        self._max_recovery_attempts = max_recovery_attempts
        self._max_steps = max_steps

        # 运行时状态 (由 fight() 重置)
        self._plan: CombatPlan = CombatPlan(name='', mode=CombatMode.BATTLE)
        self._recognizer: CombatRecognizer = None  # type: ignore[assignment]  # set in fight()
        self._phase = CombatPhase.PROCEED
        self._last_action = 'yes'
        self._node = '0'
        self._ship_stats: list[ShipDamageState] = [ShipDamageState.NORMAL] * 6
        self._history = CombatHistory()
        self._node_count = 0

        # 熔断计数器 (由 _reset 重置)
        self._recovery_attempts = 0
        self._steps = 0

        # 节点跟踪器 (仅常规战模式有效，由 fight() 初始化)
        self._tracker: NodeTracker | None = None

        # 节点级临时状态
        self._formation_by_rule: Formation | None = None

    # ═══════════════════════════════════════════════════════════════════════════
    # 公共接口
    # ═══════════════════════════════════════════════════════════════════════════

    def fight(  # noqa: PLR0912
        self,
        plan: CombatPlan,
        initial_ship_stats: list[ShipDamageState],
    ) -> CombatResult:
        """执行一次完整的战斗循环。

        从当前状态开始，循环执行:
        ``update_state → make_decision`` 直到战斗结束或 SL。

        Parameters
        ----------
        plan:
            作战计划 (阵型、夜战、节点决策等)。
        initial_ship_stats:
            初始血量状态（来自出征准备页面的检测结果）。

        Returns
        -------
        CombatResult
        """
        self._plan = plan
        self._recognizer = CombatRecognizer(
            self._ctx,
        )
        self._reset()

        # 常规战 / 活动战模式下加载地图节点数据并初始化节点追踪器
        if plan.mode == CombatMode.NORMAL:
            map_data = MapNodeData.load(plan.chapter, plan.map_id)
            if map_data is not None:
                self._tracker = NodeTracker(map_data)
                _log.info(
                    '[Combat] 节点追踪器已加载: {}-{} ({} 个节点)',
                    plan.chapter,
                    plan.map_id,
                    len(map_data),
                )
            else:
                self._tracker = None
                _log.warning(
                    '[Combat] 无法加载地图数据 {}-{}，节点追踪将不可用',
                    plan.chapter,
                    plan.map_id,
                )
        elif plan.mode == CombatMode.EVENT and plan.event_name:
            map_data = MapNodeData.load_event(
                plan.event_name,
                plan.chapter,
                plan.map_id,
                plan.entrance,
            )
            if map_data is not None:
                self._tracker = NodeTracker(map_data)
                _log.info(
                    '[Combat] 活动节点追踪器已加载: {}/{}-{} ({} 个节点)',
                    plan.event_name,
                    plan.chapter,
                    plan.map_id,
                    len(map_data),
                )
            else:
                self._tracker = None
                _log.warning(
                    '[Combat] 无法加载活动地图数据 {}/{}-{}，节点追踪将不可用',
                    plan.event_name,
                    plan.chapter,
                    plan.map_id,
                )
        else:
            self._tracker = None

        if initial_ship_stats is not None:
            self._ship_stats = initial_ship_stats[:]

        result = CombatResult(history=self._history)

        while True:
            # ── 护栏 2：步数上限 ──
            # 封住「识别始终成功但页面不再推进」的死循环（含节点追踪失败
            # 反复撤退）。正常一次节点战斗 8~14 步，此处默认 200 步余量充足。
            self._steps += 1
            if self._steps > self._max_steps:
                _log.error(
                    '[Combat] 步数超限 ({} > {}), 判定状态机卡死。'
                    '当前 phase={} node={} 节点数={} 历史={} —— 判 SL 重开整局',
                    self._steps,
                    self._max_steps,
                    self._phase.name,
                    self._node,
                    self._node_count,
                    self._history,
                )
                result.flag = ConditionFlag.SL
                break

            try:
                decision = self._step()
            except CombatStopRequestedError:
                _log.info('[Combat] 收到停止请求，正在退出战斗')
                result.flag = ConditionFlag.FIGHT_END
                break
            except CombatRecognitionTimeoutError as e:
                _log.warning(
                    '[Combat] 状态识别超时 (第 {} 次, 步 {}): {}',
                    self._recovery_attempts + 1,
                    self._steps,
                    e,
                )

                # ── 护栏 1：恢复次数上限 ──
                # 封住「恢复成功 → 继续 → 又超时」的恢复死循环。此前此处
                # 是裸 `continue`：若页面稳定停在某个后继态（如 PROCEED
                # 模板误命中但点击无效），每次都能被认出来，于是无限自我
                # 循环，永不退出。
                self._recovery_attempts += 1
                if self._recovery_attempts > self._max_recovery_attempts:
                    _log.error(
                        '[Combat] 连续恢复 {} 次仍未推进 (上限 {}), 放弃本局。'
                        '当前 phase={} node={} 步={} —— 判 SL 重开整局',
                        self._recovery_attempts,
                        self._max_recovery_attempts,
                        self._phase.name,
                        self._node,
                        self._steps,
                    )
                    result.flag = ConditionFlag.SL
                    break

                if self._try_recovery():
                    continue
                result.flag = ConditionFlag.SL
                break

            # 本轮成功推进了状态机 → 恢复计数归零
            self._recovery_attempts = 0

            if decision == ConditionFlag.FIGHT_CONTINUE:
                continue
            elif decision == ConditionFlag.DOCK_FULL:
                _log.warning('[Combat] 战斗进入失败：船坞已满')
                result.flag = ConditionFlag.DOCK_FULL
                break
            elif decision == ConditionFlag.SL:
                # TODO: 这里出现了轻微的抽象泄露，因为 SL 需要调用 restart_game
                result.flag = ConditionFlag.SL
                from autowsgr.ops import restart_game

                restart_game(self._device)
                break
            elif decision == ConditionFlag.SKIP_FIGHT:
                # 索敌后主动撤退 (节点不在白名单/规则撤退): 不是成功打完,
                # 不记 OPERATION_SUCCESS (实机 2026-10-08: 撤退轮曾冒充
                # success)。触发器不计入达标, 会继续产出下一轮。
                _log.warning('[Combat] 战斗被主动跳过 (撤退), 记 skip fight')
                result.flag = ConditionFlag.SKIP_FIGHT
                break
            elif decision == ConditionFlag.FIGHT_END:
                _log.debug('[Combat] 战斗已结束，日志: {}', self._history)
                result.flag = ConditionFlag.OPERATION_SUCCESS
                break

        result.ship_stats = self._ship_stats[:]
        result.node_count = self._node_count
        _log.info(
            '[Combat] 战斗结束: {} (节点数={}, 步数={}, 恢复={})',
            result.flag.value,
            result.node_count,
            self._steps,
            self._recovery_attempts,
        )
        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════════════════════
    def _is_map_routing_phase(self, last_phase: CombatPhase) -> bool:
        """是否处于地图行军/出征过渡阶段 (poll 做节点追踪 + 加速点击)。

        START_FIGHT **必须包含**: 它是「点击开始出征之后、进入战斗之前」
        的过渡态, 期间 poll 负责节点追踪 —— 若不含, 首次索敌时
        ``self._node`` 仍是初始 '0', 白名单判定失败会**误撤退**
        (实机 2026-10-08 18:11: 三连全部「节点=0 撤退」)。
        该阶段的加速点击位于底部安全带 (见 :attr:`Coords.SPEED_UP_NORMAL`),
        无可交互元素; 其余时间轴 (FIGHT_PERIOD/NIGHT_PROMPT 等待期) 依然
        绝不盲点。
        """
        return (
            last_phase
            in (CombatPhase.PROCEED, CombatPhase.FIGHT_CONDITION, CombatPhase.START_FIGHT)
            or self._last_action == 'detour'
        )

    def _reset(self) -> None:
        """重置运行时状态。"""
        self._history.reset()
        self._node = '0'
        self._node_count = 0
        self._formation_by_rule = None
        self._recovery_attempts = 0
        self._steps = 0

        # 重置节点追踪器
        if self._tracker is not None:
            self._tracker.reset()

        self._phase = CombatPhase.START_FIGHT
        self._last_action = ''

    def _step(self) -> ConditionFlag:
        """执行一步: 状态更新 + 决策。"""
        new_phase = self._update_state()
        return self._make_decision(new_phase)

    def _update_state(self) -> CombatPhase:
        """等待并识别下一个状态。"""
        last_phase = self._phase

        candidates = resolve_successors(
            self._plan.transitions,
            self._phase,
            self._last_action,
        )

        _log_sm.debug(
            '[Combat] 当前: {} (action={}) → 候选: {}',
            last_phase.name,
            self._last_action,
            [c.name for c in candidates],
        )

        # 构建轮询间动作（加速点击 + 节点追踪）
        poll_action = self._get_poll_action(last_phase)
        new_phase = self._recognizer.wait_for_phase(
            candidates,
            poll_action=poll_action,
            allow_event_overlay_anchor=last_phase is not CombatPhase.START_FIGHT,
        )

        self._phase = new_phase
        return new_phase

    def _get_poll_action(self, last_phase: CombatPhase) -> Callable[[np.ndarray], None] | None:
        """根据当前状态和模式大类，返回每轮匹配前执行的动作。

        MAP 模式下，地图移动/出征过渡期间执行行军加速 (底部安全带)、
        节点追踪、资源弹窗点掉；SINGLE 模式下仅加速点击。
        **战斗动画期 (FIGHT_PERIOD/NIGHT_PROMPT 等待) 绝不点击** —— 盲点
        会把战斗/结算整段点穿 (实机 2026-10-08: 夜战三连被跳过)。结算
        三层的快速跳过在处理器层 (RESULT 触发) 完成, 位置为底部安全带。
        """
        category = MODE_CATEGORIES.get(self._plan.mode)
        if self._phase is CombatPhase.FIGHT_PERIOD:
            return lambda _: time.sleep(0.5)
        if category is ModeCategory.MAP:
            if self._is_map_routing_phase(last_phase):
                tracker = self._tracker
                device = self._device

                def _poll_map(screen: np.ndarray) -> None:
                    click_speed_up(device)
                    if tracker is not None:
                        tracker.update_ship_position(screen)
                        new_node = tracker.update_node()
                        if new_node != self._node:
                            self._node = new_node
                        dismiss_resource_confirm(device, screen)

                return _poll_map

        elif category is ModeCategory.SINGLE and last_phase is CombatPhase.START_FIGHT:

            def _poll_single(_: np.ndarray) -> None:
                click_speed_up(self._device)

            return _poll_single

        return None

    # ═══════════════════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════════════════

    def _get_current_decision(self) -> NodeDecision:
        """获取当前节点的决策。"""
        return self._plan.get_node_decision(self._node)

    def _try_recovery(self) -> bool:
        """尝试从错误中恢复。"""
        _log.warning('[Combat] 尝试错误恢复...')
        time.sleep(3.0)

        screen = self._device.screenshot()
        end_phase = self._plan.end_phase

        # 恢复判据不只看终态页: 若页面其实停在当前状态的某个合法后继上
        # (典型: 战果页点击穿透到掉落页/经验页), 认出来就能接着跑, 不必
        # 判 SL 重开整局 —— 那会丢掉刚打完的这一局战果。
        candidates: list[CombatPhase] = []
        if end_phase is not None:
            candidates.append(end_phase)
        for phase in resolve_successors(self._plan.transitions, self._phase, self._last_action):
            if phase not in candidates:
                candidates.append(phase)

        # 结算三层兜底: 快速结算穿行模式下结算页 (战果/经验/掉落) 不在
        # 后继集, 超时若恰好落在这些页上 (点击尚未推进完), 识别到即可把
        # 页面交还处理器接着点穿 (转移图仍保留其条目), 不必 SL 重开整局。
        # 仅 MAP 模式添加: SINGLE (ep=None) 的 RESULT 无转移条目, resume
        # 会在 resolve_successors KeyError。
        if MODE_CATEGORIES.get(self._plan.mode) is ModeCategory.MAP:
            for phase in (CombatPhase.RESULT, CombatPhase.EXP_SETTLEMENT, CombatPhase.GET_SHIP):
                if phase not in candidates:
                    candidates.append(phase)

        result = self._recognizer.identify_current(screen, candidates)
        if result is not None:
            _log.warning('[Combat] 错误恢复: 识别到 {}, 继续战斗', result.name)
            self._phase = result
            return True
        return False

    def set_node(self, node: str) -> None:
        """设置当前节点（外部调用，如地图追踪更新）。"""
        self._node = node

    @property
    def current_node(self) -> str:
        """当前节点。"""
        return self._node

    @property
    def history(self) -> CombatHistory:
        """战斗历史。"""
        return self._history


# ═══════════════════════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════════════════════


def run_combat(
    ctx: GameContext,
    plan: CombatPlan,
    *,
    ship_stats: list[ShipDamageState],
) -> CombatResult:
    """执行一次完整战斗的便捷函数。"""
    engine = CombatEngine(ctx=ctx)
    return engine.fight(plan, initial_ship_stats=ship_stats)
