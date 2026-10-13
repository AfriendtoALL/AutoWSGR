"""auto_daily 触发器 — 决定何时把何种任务插入调度队列。

四类触发器,对应四类日常任务::

    ExpeditionTrigger  (定时, prio 0)   远征: 到点收取+重派, 无限循环
    CampaignTrigger    (条件, prio 5)   战役: 每次一场, BATTLE_TIMES_EXCEED 即停, 跨日 reset
    ExerciseTrigger    (条件, prio 10)  演习: 每次一场, 无可挑战对手即停, 跨时段 reset
    NormalFightTrigger (条件, prio 100) 常规战: 空闲填充, 到掉落/次数上限停, 跨日 reset

设计要点:

- 每个触发器同一时刻最多产出一个 pending 任务 (``_idle`` 标志), 避免队列堆积;
  任务完成后经 ``on_done`` 回调翻回 ``_idle``。
- 战役/演习是「返回标志驱动」的可耗尽触发器 (:class:`ExhaustibleTrigger`):
  runner 返回特定 :class:`~autowsgr.types.ConditionFlag` → 标记 ``_exhausted``,
  不再产出, 直到 ``reset()`` (跨日/跨时段)。
- 常规战不靠返回标志, 而是每次 ``should_fire`` 主动检查全局上限 (``ctx`` 每日计数器)。
- 跨日由 :class:`~autowsgr.scheduler.scheduler.TaskScheduler._check_daily_reset` 调
  ``reset()``;演习跨时段在 ``should_fire`` 内自检。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from autowsgr.combat import GradeCondition, grade_condition_met
from autowsgr.infra.logger import get_logger
from autowsgr.scheduler.scheduler import FightTask
from autowsgr.types import ConditionFlag


if TYPE_CHECKING:
    from autowsgr.combat import CombatResult
    from autowsgr.context import GameContext

_log = get_logger('scheduler')

# 任务工厂: 接收 ctx, 返回一个有 run() 方法的 runner
# GameContext 仅运行时在 TYPE_CHECKING 下导入, 此处用字符串前向引用
TaskFactory = Callable[['GameContext'], object]

# 视为「成功打完一场」的标志 (用于常规战计数累加)
_DONE_FLAGS = {
    ConditionFlag.OPERATION_SUCCESS,
    ConditionFlag.FIGHT_END,
    ConditionFlag.SL,
}

#: 连续多少场 ``SKIP_FIGHT`` 判定为「空转」(2026-10-13)。
#:
#: 场景：计划要求白名单节点 (如 ``selected_nodes: [C]``)，但编队不满足
#: 带路条件 → 实际进入 B 点 → 白名单未命中 → 撤退 → 再出征 → 再撤退。
#: 此类循环**逃过现有全部三道防线**（见 ``NormalFightPlan.disabled_reason``
#: 与测试 ``testing/scheduler/test_idle_loop.py`` 的说明）。
#:
#: 取 10 的依据：白名单撤退本身是正常行为（试错/绕路），偶发几次不算异常；
#: 按每场约 2~3 分钟估算，10 次 ≈ 20~30 分钟后报警。比熔断阈值 5 更宽，
#: 因为熔断针对的是「报错」，而这里针对的是「没报错但没进展」。
IDLE_LOOP_THRESHOLD = 10


# ═══════════════════════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════════════════════


class Trigger:
    """触发器基类。

    Parameters
    ----------
    priority:
        产出任务的优先级 (数值越小越先执行)。
    name:
        触发器名称 (日志用)。
    task_factory:
        接收 ctx、返回 runner (有 ``run()`` 方法) 的工厂。每次 ``should_fire`` 命中时
        调用, 产出新 runner 包装进 :class:`FightTask`。常规战触发器不用它 (各 plan
        自带工厂), 可留空。
    """

    def __init__(
        self,
        *,
        priority: int,
        name: str,
        task_factory: TaskFactory | None = None,
    ) -> None:
        self.priority = priority
        self.name = name
        self._task_factory = task_factory
        # 同一时刻最多一个 pending 任务, 避免队列堆积
        self._idle = True

    def should_fire(self, ctx: GameContext) -> FightTask | None:
        """是否应产出任务。子类重写, 返回 FightTask 或 None。"""
        raise NotImplementedError

    def reset(self) -> None:
        """跨日/跨时段重置。子类按需重写。"""
        self._idle = True

    def _build_task(
        self,
        ctx: GameContext,
        on_done: Callable[[CombatResult], None] | None = None,
    ) -> FightTask:
        """用 ``task_factory`` 构造一个 FightTask。"""
        if self._task_factory is None:
            raise RuntimeError(f'触发器 {self.name} 未配置 task_factory')
        runner = self._task_factory(ctx)
        return FightTask(
            runner=runner,
            times=1,
            priority=self.priority,
            name=self.name,
            on_done=on_done,
        )


class ExhaustibleTrigger(Trigger):
    """返回标志驱动的可耗尽触发器 (战役/演习)。

    runner 返回的 ``CombatResult.flag`` 若命中 ``exhaust_flags``, 则标记
    ``_exhausted``, 不再产出新任务, 直到 :meth:`reset` (跨日/跨时段)。
    """

    #: 子类定义: 命中这些 flag 即视为本周期打满
    exhaust_flags: ClassVar[set[ConditionFlag]] = set()

    def __init__(
        self,
        *,
        task_factory: TaskFactory,
        priority: int,
        name: str,
    ) -> None:
        super().__init__(priority=priority, name=name, task_factory=task_factory)
        self._exhausted = False

    def should_fire(self, ctx: GameContext) -> FightTask | None:
        if not self._idle or self._exhausted:
            return None
        self._idle = False
        return self._build_task(ctx, on_done=self._on_done)

    def _on_done(self, result: CombatResult) -> None:
        self._idle = True
        if result.flag in self.exhaust_flags:
            self._exhausted = True
            _log.info(
                '[Trigger] {} 本周期已打满 ({}), 停止产出',
                self.name,
                result.flag,
            )

    def reset(self) -> None:
        self._exhausted = False
        self._idle = True


# ═══════════════════════════════════════════════════════════════════════════════
# 定时触发 (远征)
# ═══════════════════════════════════════════════════════════════════════════════


class TimerTrigger(Trigger):
    """定时触发器: 到间隔产出一个任务, 不耗尽 (远征用)。"""

    def __init__(
        self,
        *,
        task_factory: TaskFactory,
        priority: int,
        name: str,
        interval: float,
    ) -> None:
        super().__init__(priority=priority, name=name, task_factory=task_factory)
        self._interval = interval
        self._last_fire = 0.0

    def should_fire(self, ctx: GameContext) -> FightTask | None:
        if not self._idle:
            return None
        if time.monotonic() - self._last_fire < self._interval:
            return None
        self._idle = False
        self._last_fire = time.monotonic()
        return self._build_task(ctx, on_done=self._on_done)

    def _on_done(self, _result: CombatResult) -> None:
        self._idle = True


class ExpeditionTrigger(TimerTrigger):
    """远征触发器: 定时收取 + 重派 (无限循环, 永不耗尽)。"""


# ═══════════════════════════════════════════════════════════════════════════════
# 条件触发 (战役 / 演习)
# ═══════════════════════════════════════════════════════════════════════════════


class CampaignTrigger(ExhaustibleTrigger):
    """战役触发器: ``BATTLE_TIMES_EXCEED`` 即停 (每日次数 8/12 自适应)。"""

    exhaust_flags: ClassVar[set[ConditionFlag]] = {ConditionFlag.BATTLE_TIMES_EXCEED}


class ExerciseTrigger(ExhaustibleTrigger):
    """演习触发器: 无可挑战对手 (``SKIP_FIGHT``) 即停, 跨时段 (0/12/18) reset。

    每日 0/12/18 三个时段各刷新 5 次机会;跨时段时 ``_exhausted`` 清零。
    """

    exhaust_flags: ClassVar[set[ConditionFlag]] = {ConditionFlag.SKIP_FIGHT}

    def __init__(
        self,
        *,
        task_factory: TaskFactory,
        priority: int,
        name: str,
    ) -> None:
        super().__init__(task_factory=task_factory, priority=priority, name=name)
        self._last_slot = self._current_slot()

    @staticmethod
    def _current_slot() -> int:
        """当前演习时段: 0 (0-12点) / 1 (12-18点) / 2 (18-24点)。"""
        hour = time.localtime().tm_hour
        if hour < 12:
            return 0
        if hour < 18:
            return 1
        return 2

    def should_fire(self, ctx: GameContext) -> FightTask | None:
        slot = self._current_slot()
        if slot != self._last_slot:
            _log.info(
                '[Trigger] 演习跨时段 ({}→{}), 重置可挑战次数',
                self._last_slot,
                slot,
            )
            self._exhausted = False
            self._idle = True
            self._last_slot = slot
        return super().should_fire(ctx)


# ═══════════════════════════════════════════════════════════════════════════════
# 条件触发 (常规战 — 空闲填充)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class NormalFightPlan:
    """常规战单个 plan 的运行时状态。

    Attributes
    ----------
    factory:
        接收 ctx、返回该 plan 的 NormalFightRunner 的工厂。
    name:
        plan 名称 (如 ``8-5AI-only1DD``), 用于日志。
    fleet_id:
        出击舰队编号。
    target:
        本周期目标出击次数。``None`` 表示无限 (空闲填充, 仅受全局上限
        ``quick_repair_limit`` / ``stop_dock_full`` 约束)。
        dev 的 ``DailyAutomationConfig.normal_fight_tasks`` 只给 plan 名、不给次数,
        所以默认 ``None``, 多个无限 plan 会轮询执行。
    conditions:
        战果达成条件列表 (镜像自 :class:`CombatPlan.conditions`, 从
        ``node_args`` 各节点的 ``grade`` 派生)。非空时只有满足全部
        条件的场次才计数; 配置条件的 plan 自动走慢速结算采集
        (grade/MVP), 触发器侧谓词才有材料可判。
    completed:
        已成功完成次数 (运行时, 仅日志/有限 plan 的停止判断用)。
    results:
        全部尝试场次的结果列表 (含未达标场, 供调用方统计/排查)。
        触发器模式下调度的战斗不走 ``task.results`` 路径, 此列表是
        每场 ``CombatResult`` 的唯一记录处。
    """

    factory: TaskFactory
    name: str
    fleet_id: int
    target: int | None = None
    conditions: tuple[GradeCondition, ...] = ()
    completed: int = 0
    results: list[CombatResult] = field(default_factory=list)

    consecutive_skips: int = 0
    """连续「白名单撤退」(``SKIP_FIGHT``) 的场次计数。

    任意非 ``SKIP_FIGHT`` 的结果都会归零 —— 包括 ``ACTION_FAILED``
    (它由熔断负责，不在此重复计)。
    """

    skip_nodes: dict[str, int] = field(default_factory=dict)
    """连续撤退期间各节点累计次数 (运行时: 仅诊断用)。成功一场即清空。"""

    disabled_reason: str | None = None
    """非 ``None`` 表示该 plan 已因空转被停用，不再产出任务。

    会话级 (``reset()`` 不清除)：根因是配置错误 (如编队不满足带路条件)，
    跨日不会自动缓解，须人工修正后重启脚本 —— 与 ``_dock_full_stopped``
    语义一致。
    """


class NormalFightTrigger(Trigger):
    """常规战触发器: 多 plan 列表 + 全局上限, 空闲填充 (priority 最低)。

    每次产出一个未达 ``target`` 的 plan 的任务;全部 plan 达 target, 或 ``ctx``
    每日掉落/船/快修达上限, 则停止产出 (直到 :meth:`reset`)。

    priority 设为最高数值 → 排队尾 → 只有没其他任务时才执行 (= 空闲填充)。
    """

    def __init__(
        self,
        *,
        priority: int,
        name: str,
        plans: list[NormalFightPlan],
        quick_repair_limit: int | None = None,
        stop_dock_full: bool = True,
    ) -> None:
        super().__init__(priority=priority, name=name)
        self._plans = plans
        self._quick_repair_limit = quick_repair_limit
        self._stop_dock_full = stop_dock_full
        self._current: NormalFightPlan | None = None
        # 无限 plan (target=None) 的轮询游标
        self._round_robin = 0
        # 是否被禁用 (不再产出任务)。启动校准计数器失败 (OCR 不可用) 等场景由
        # 调度层设置; 持续整个会话, reset() 不清除 (OCR 可用性不跨日变化)。
        self._disabled = False
        # 船坞已满且未解装 → 会话级停止产出。reset() 不清除: 船坞满是物理
        # 阻塞, 不因跨日自动缓解, 需清船/解装后重启脚本。
        self._dock_full_stopped = False

    @property
    def dock_full_stopped(self) -> bool:
        """本触发器是否已因船坞满且未解装而停止 (会话级, reset 不清除)。"""
        return self._dock_full_stopped

    def disable(self, reason: str = '') -> None:
        """禁用本触发器 (不再产出任务)。

        用于依赖的每日计数器无法校准 (OCR 引擎不可用) 等场景 —— 与其降级后
        靠首场战斗自行校准 (战斗未必掉落, 计数器可能一直为 0 → 持续误触发),
        不如直接禁用并提示用户。持续整个会话, :meth:`reset` 不清除。
        """
        if not self._disabled:
            self._disabled = True
            _log.warning('[Trigger] 常规战触发器已禁用: {}', reason)

    def should_fire(self, ctx: GameContext) -> FightTask | None:
        if self._disabled:
            return None
        if not self._idle:
            return None
        if self._is_exhausted(ctx):
            return None
        plan = self._pick_plan()
        if plan is None:
            return None
        self._idle = False
        self._current = plan
        runner = plan.factory(ctx)
        return FightTask(
            runner=runner,
            times=1,
            priority=self.priority,
            name=f'{self.name}/{plan.name}',
            on_done=self._on_done,
        )

    def _has_plan(self) -> bool:
        """是否有可产出的 plan (无副作用, 供耗尽判断用)。

        已因空转停用的 plan (``disabled_reason`` 非空) 不计入 —— 否则
        ``_is_exhausted`` 会误判「还有活可干」，让调度器一直空转等待。
        """
        return any(
            plan.disabled_reason is None
            and (plan.target is None or plan.completed < plan.target)
            for plan in self._plans
        )

    def _pick_plan(self) -> NormalFightPlan | None:
        """选下一个 plan: 优先未完成的有 target 的; 再轮询无限的。

        跳过已因空转停用的 plan（``disabled_reason`` 非空）。
        """
        for plan in self._plans:
            if plan.disabled_reason is not None:
                continue
            if plan.target is not None and plan.completed < plan.target:
                return plan
        unlimited = [
            p
            for p in self._plans
            if p.target is None and p.disabled_reason is None
        ]
        if unlimited:
            plan = unlimited[self._round_robin % len(unlimited)]
            self._round_robin += 1
            return plan
        return None

    def _is_exhausted(self, ctx: GameContext) -> bool:
        """是否本周期常规战已全部完成 / 达上限。"""
        if (
            self._quick_repair_limit is not None
            and ctx.quick_repair_used >= self._quick_repair_limit
        ):
            return True
        if self._stop_dock_full and self._dock_full_stopped:
            return True
        return not self._has_plan()

    def _on_done(self, result: CombatResult) -> None:
        self._idle = True
        if self._current is None:
            return
        # 记录全部场次结果 (含未达标场), 供调用方 (如 run_yaml_plan) 统计
        self._current.results.append(result)
        self._track_idle_loop(self._current, result)
        # 仅成功打完一场才计数 (对齐 classic: SUCCESS/SL 才算);
        # 配置 conditions 的 plan 还须战果全部达标 — 不达标的场次 (如 SL
        # 重开、评级不足) 不计入, 触发器下轮继续产出直到达标次数打满。
        counted = result.flag in _DONE_FLAGS and all(
            grade_condition_met(cond, result) for cond in self._current.conditions
        )
        if counted:
            self._current.completed += 1
            target = self._current.target
            progress = (
                f'{self._current.completed}/{target}'
                if target is not None
                else str(self._current.completed)
            )
            _log.info(
                '[Trigger] {} {} 出击 {}',
                self.name,
                self._current.name,
                progress,
            )
        elif result.flag in _DONE_FLAGS and self._current.conditions:
            _log.info(
                '[Trigger] {} {} 本场未达战果条件 ({}), 不计数',
                self.name,
                self._current.name,
                '/'.join(f'{c.node}>={c.grade}' for c in self._current.conditions),
            )
        # 船坞已满且未解装 (解装成功轮 dock_full_destroyed=True 不在此分支):
        # 会话级停止: 无法继续打满, 常规战不再产出, 直到清船/解装后重启脚本。
        # 仅 stop_dock_full 开启时置位 — 关闭时保持旧挂机语义 (空转重试)。
        if (
            self._stop_dock_full
            and result.flag == ConditionFlag.DOCK_FULL
            and not result.dock_full_destroyed
        ):
            self._dock_full_stopped = True
            _log.warning(
                '[Trigger] {} 船坞已满且无法解装, 常规战停止 (清船/解装后重启脚本)',
                self.name,
            )

    def _track_idle_loop(self, plan: NormalFightPlan, result: CombatResult) -> None:
        """空转检测：连续多场白名单撤退且零成功 → 停用该 plan 并报根因。

        为什么需要它（2026-10-13 用户实机反馈）
        ----------------------------------------
        用户的 5-2 计划要求白名单 ``[C]``，但默认编队不满足带路条件，
        舰队实际进入 ``B`` 点 → 撤退 → ``SKIP_FIGHT`` → 再出征 → 无限循环。
        三道现有防线**全部漏掉**：

        1. ``max_steps``：``CombatEngine._reset()`` 每场清零，而每场只走 ~5 步；
        2. 熔断：``SKIP_FIGHT`` 刻意不算失败（正常资源信号）；
        3. 停滞检测：每场点不同坐标，不满足「同坐标」前提。

        于是 ``completed`` 永不增长、触发器无限产出 —— 本方法补这一类
        **「无异常、只是无进展」**的循环。

        判据刻意比熔断（连续失败 5 次）更宽：白名单撤退本身是正常行为，
        偶发几次（试错、绕路）不该报警；只有**持续且零成功**才是问题。
        """
        if result.flag is not ConditionFlag.SKIP_FIGHT:
            # 任意其它结果都中断「连续」计数（含 ACTION_FAILED: 它归熔断管，
            # 此处不重复计，避免同一次故障被双重惩罚）
            plan.consecutive_skips = 0
            plan.skip_nodes.clear()
            return

        plan.consecutive_skips += 1
        for event in result.history.events:
            if event.action == '撤退':
                plan.skip_nodes[event.node] = plan.skip_nodes.get(event.node, 0) + 1

        if plan.consecutive_skips < IDLE_LOOP_THRESHOLD:
            return

        nodes = ', '.join(f'{n} 点 {c} 次' for n, c in sorted(plan.skip_nodes.items()))
        plan.disabled_reason = (
            f'连续 {plan.consecutive_skips} 场白名单撤退且无一次成功 '
            f'(撤退节点: {nodes or "未知"}) —— '
            f'疑似编队不满足带路条件, 请核对该关卡对舰队编成/舰种/航速的要求, '
            f'或确认 selected_nodes 与计划目标一致'
        )
        _log.error(
            '[Trigger] {} {} 空转判定, 已停止该计划: {}',
            self.name,
            plan.name,
            plan.disabled_reason,
        )

    def reset(self) -> None:
        for plan in self._plans:
            plan.completed = 0
        self._idle = True
        self._current = None
        self._round_robin = 0
