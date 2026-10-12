"""WSGR 日常自动化计划 — 把 zjsnrwiki 的机制事实翻译成定时任务。

本模块是 :class:`~autowsgr.scheduler.timed_scheduler.TimedScheduler` 的
** WSGR 专用配方**: 读取 ``ctx.config.daily_automation``, 按游戏的真实刷新
节奏注册任务。与 :func:`~autowsgr.scheduler.daily_plan.build_daily_plan`
(触发器模式) 二选一使用, 语义等价但调度模型不同。

机制事实与设定依据 (zjsnrwiki):

==============================  ====================================
刷新 / 限制                     本模块设定
==============================  ====================================
每日任务凌晨 3 点刷新            (由游戏自身处理; 脚本只按 flag 顺延)
掉落计数 / 战役次数 0 点刷新      战役/常规战打满后 ``exhausted`` 回到 00:05
演习 0 / 12 / 18 点刷新          演习打满后回到三个时段之一
战役每日 8 次 (活动期 12 次)     runner 返回 ``BATTLE_TIMES_EXCEED`` 自适应
船坞满是正常资源信号             退避 30 分钟重试, **不计失败** (见偏差说明)
==============================  ====================================

与触发器模式的有意偏差:

- **船坞满不停止会话**: 触发器模式 (``stop_dock_full``) 在船坞满且未解装时
  永久停止常规战; 定时模式只退避 30 分钟重试 —— 无人值守时用户可能随时清
  船坞, 停止会话反而需要人工重启脚本。
- **打满即走, 不轮空转**: 战役/演习/常规战在 runner 返回耗尽 flag 后直接
  顺延到下一个重置点, 不会每分钟空转检查。

使用方式::

    from autowsgr.scheduler import launch
    from autowsgr.scheduler.timed_scheduler import TimedScheduler
    from autowsgr.scheduler.wsgr_daily import build_wsgr_daily_plan

    ctx = launch('usersettings.yaml')
    scheduler = TimedScheduler(ctx, state_path='logs/timed_state.json')
    build_wsgr_daily_plan(scheduler, ctx)
    scheduler.run()
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from autowsgr.combat import CombatResult, grade_condition_met
from autowsgr.infra.logger import get_logger
from autowsgr.scheduler.timed_scheduler import (
    DailySchedule,
    FunctionRunner,
    IntervalSchedule,
    TaskSpec,
    TimedScheduler,
)
from autowsgr.scheduler.triggers import NormalFightPlan
from autowsgr.types import ConditionFlag


if TYPE_CHECKING:
    from autowsgr.context import GameContext
    from autowsgr.infra.config import DailyAutomationConfig


_log = get_logger('scheduler.wsgr_daily')

# 优先级常量 (数值越小越先执行) — 与 daily_plan.py 保持一致, 便于对照
PRIO_EXPEDITION = 0
PRIO_BONUS = 1
PRIO_CAMPAIGN = 5
PRIO_EXERCISE = 10
PRIO_NORMAL_FIGHT = 100
PRIO_BATH_REPAIR = 200

#: 远征 / 奖励 / 修理的轮询间隔 (秒)。collect_expedition 内部会先检查是否有
#: 远征可收取, 无则空转返回, 故可较短。
DEFAULT_POLL_INTERVAL = 600.0

#: 战斗类任务「打完一场 → 下一场」的间隔 (秒)。给优先级更高的远征/奖励
#: 插队窗口 (战役 8 次 / 演习 5 次要分多次执行, 场间应可插入远征收取)。
FIGHT_NEXT_INTERVAL = 60.0

#: 0 点刷新类任务的首次执行时刻 (留 5 分钟余量等游戏端刷新完成)
DROP_RESET_TIME = '00:05'

#: 演习对手刷新时段 (0 / 12 / 18 点, wiki/新手入门)
EXERCISE_RESET_TIMES = ('00:05', '12:05', '18:05')

#: 视为「成功打完一场」的标志 (常规战计数用) — 与 triggers.py 口径一致
_DONE_FLAGS = {
    ConditionFlag.OPERATION_SUCCESS,
    ConditionFlag.FIGHT_END,
    ConditionFlag.SL,
}


class NormalFightRotator:
    """常规战 plan 轮转 runner — 镜像 ``NormalFightTrigger`` 的挑选/计数语义。

    每次 ``run()`` 选一个未达标的 plan 执行; 全部达 target (或达到快修上限)
    时返回 ``SKIP_FIGHT``, 调度器据此顺延到下一个重置点。

    「今日次数」按本地日期懒重置 (每个 run 前检查一次), 不依赖调度器的跨日
    回调 —— 自包含, 可单测。
    """

    def __init__(
        self,
        plans: list[NormalFightPlan],
        ctx: GameContext,
        *,
        name: str = '常规战',
        quick_repair_limit: int | None = None,
    ) -> None:
        self._plans = plans
        self._ctx = ctx
        self._name = name
        self._quick_repair_limit = quick_repair_limit
        self._round_robin = 0
        self._reset_date = date.today()  # noqa: DTZ011  # 本地墙上时钟 (游戏 0 点刷新)

    def run(self) -> list[CombatResult]:
        """执行一场常规战; 今日目标已完成则返回 ``SKIP_FIGHT``。"""
        self._maybe_reset_daily()
        if (
            self._quick_repair_limit is not None
            and self._ctx.quick_repair_used >= self._quick_repair_limit
        ):
            _log.warning(
                '[Rotator] {} 快修已达上限 ({}/{}), 今日不再出击',
                self._name,
                self._ctx.quick_repair_used,
                self._quick_repair_limit,
            )
            return [CombatResult(flag=ConditionFlag.SKIP_FIGHT)]

        plan = self._pick_plan()
        if plan is None:
            _log.info('[Rotator] {} 今日目标已全部完成, 等待次日重置', self._name)
            return [CombatResult(flag=ConditionFlag.SKIP_FIGHT)]

        runner = plan.factory(self._ctx)
        raw = runner.run()
        results = raw if isinstance(raw, list) else [raw]
        for result in results:
            self._record(plan, result)
        return results

    def _pick_plan(self) -> NormalFightPlan | None:
        """选下一个 plan: 优先未完成的有 target 的; 再轮询无限的。"""
        for plan in self._plans:
            if plan.target is not None and plan.completed < plan.target:
                return plan
        unlimited = [p for p in self._plans if p.target is None]
        if unlimited:
            plan = unlimited[self._round_robin % len(unlimited)]
            self._round_robin += 1
            return plan
        return None

    def _record(self, plan: NormalFightPlan, result: CombatResult) -> None:
        """记录一场结果并计数 (与 ``NormalFightTrigger._on_done`` 同口径)。"""
        plan.results.append(result)
        counted = result.flag in _DONE_FLAGS and all(
            grade_condition_met(cond, result) for cond in plan.conditions
        )
        if counted:
            plan.completed += 1
            target = f'{plan.completed}/{plan.target}' if plan.target else str(plan.completed)
            _log.info('[Rotator] {} {} 出击 {}', self._name, plan.name, target)
        elif result.flag in _DONE_FLAGS and plan.conditions:
            _log.info(
                '[Rotator] {} {} 本场未达战果条件, 不计数',
                self._name,
                plan.name,
            )

    def _maybe_reset_daily(self) -> None:
        """跨日 (0 点) 重置各 plan 的今日完成计数。"""
        today = date.today()  # noqa: DTZ011  # 本地墙上时钟
        if today == self._reset_date:
            return
        _log.info('[Rotator] {} 跨日 ({} → {}), 重置出击计数', self._name, self._reset_date, today)
        for plan in self._plans:
            plan.completed = 0
        self._round_robin = 0
        self._reset_date = today


def _resolve_normal_fight_plans(
    ctx: GameContext,
    cfg: DailyAutomationConfig,
) -> list[NormalFightPlan]:
    """把配置里的常规战任务解析成 ``NormalFightPlan`` 列表 (失败项跳过)。"""
    from autowsgr.combat.fleet import resolve_fleet_selection
    from autowsgr.ops.normal_fight import NormalFightRunner, get_normal_fight_plan

    plans: list[NormalFightPlan] = []
    for task in cfg.normal_fight_tasks:
        try:
            plan = get_normal_fight_plan(task.name, plan_root=ctx.config.plan_root)
        except Exception as exc:
            _log.opt(exception=True).warning(
                '[wsgr_daily] 无法解析常规战计划 {!r}, 跳过: {}',
                task.name,
                exc,
            )
            continue
        fleet_id = task.fleet_id or plan.fleet_id or 1
        plans.append(
            NormalFightPlan(
                # 默认参数捕获 plan/fleet, 避免闭包晚绑定
                factory=lambda c, p=plan, f=fleet_id: NormalFightRunner(
                    c,
                    p,
                    resolve_fleet_selection(p, fleet_id=f),
                ),
                name=task.name,
                fleet_id=fleet_id,
                target=task.times,  # None = 无限 (空闲填充)
                conditions=plan.conditions,
            ),
        )
    return plans


def build_wsgr_daily_plan(
    scheduler: TimedScheduler,
    ctx: GameContext,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """按 ``ctx.config.daily_automation`` 注册 WSGR 日常定时任务。

    Parameters
    ----------
    scheduler:
        目标 :class:`TimedScheduler`。
    ctx:
        游戏上下文。
    poll_interval:
        远征 / 任务奖励 / 浴室修理的轮询间隔 (秒)。
    """
    cfg = ctx.config.daily_automation
    if cfg is None:
        _log.warning('[wsgr_daily] 未配置 daily_automation, 不注册任何任务')
        return

    _log.info('[wsgr_daily] 构建 WSGR 日常计划 (定时调度模式)')

    # ── 远征 (定时, prio 0): 间隔轮询, 到点收取 + 重派 ──
    if cfg.auto_expedition:
        from autowsgr.ops.expedition import collect_expedition

        scheduler.register(
            TaskSpec(
                name='远征',
                runner_factory=lambda c: FunctionRunner(
                    lambda: collect_expedition(c),
                    name='远征',
                ),
                success_schedule=IntervalSchedule(poll_interval),
                priority=PRIO_EXPEDITION,
                failure_interval=120.0,
            ),
        )

    # ── 任务奖励 (定时, prio 1): 随远征周期收取 ──
    if cfg.auto_gain_bonus:
        from autowsgr.ops.reward import collect_rewards

        scheduler.register(
            TaskSpec(
                name='任务奖励',
                runner_factory=lambda c: FunctionRunner(
                    lambda: collect_rewards(c),
                    name='任务奖励',
                ),
                success_schedule=IntervalSchedule(poll_interval),
                priority=PRIO_BONUS,
                failure_interval=120.0,
            ),
        )

    # ── 战役 (prio 5): 0 点刷新, 每日 8/12 次由 runner 自适应 ──
    if cfg.auto_battle:
        from autowsgr.ops.campaign import CampaignRunner

        battle_type = cfg.battle_type
        scheduler.register(
            TaskSpec(
                name=f'战役/{battle_type}',
                runner_factory=lambda c, bt=battle_type: CampaignRunner(c, bt, times=1),
                # 打完一场尽快打下一场 (场间允许远征插队); 次数耗尽回到次日 0 点
                success_schedule=IntervalSchedule(FIGHT_NEXT_INTERVAL),
                exhausted_schedule=DailySchedule((DROP_RESET_TIME,)),
                priority=PRIO_CAMPAIGN,
                failure_interval=120.0,
                max_failures=3,
            ),
        )

    # ── 演习 (prio 10): 0/12/18 三个时段各 5 次 ──
    if cfg.auto_exercise:
        from autowsgr.ops.exercise import ExerciseOnceRunner

        fleet_id = cfg.exercise_fleet_id or 1
        scheduler.register(
            TaskSpec(
                name='演习',
                runner_factory=lambda c, f=fleet_id: ExerciseOnceRunner(c, f),
                success_schedule=IntervalSchedule(FIGHT_NEXT_INTERVAL),
                exhausted_schedule=DailySchedule(EXERCISE_RESET_TIMES),
                priority=PRIO_EXERCISE,
                failure_interval=120.0,
                max_failures=3,
            ),
        )

    # ── 常规战 (prio 100, 空闲填充): plan 轮转, 今日目标完成则等次日 ──
    if cfg.auto_normal_fight and cfg.normal_fight_tasks:
        plans = _resolve_normal_fight_plans(ctx, cfg)
        if plans:
            scheduler.register(
                TaskSpec(
                    name='常规战',
                    runner_factory=lambda c, p=plans: NormalFightRotator(p, c),
                    success_schedule=IntervalSchedule(FIGHT_NEXT_INTERVAL),
                    exhausted_schedule=DailySchedule((DROP_RESET_TIME,)),
                    priority=PRIO_NORMAL_FIGHT,
                    failure_interval=300.0,
                    dock_full_interval=1800.0,
                ),
            )
        else:
            _log.warning('[wsgr_daily] 常规战任务列表为空或全部解析失败, 跳过')

    # ── 浴室修理 (prio 200, 空闲填充): 所有战斗任务之后 ──
    if cfg.auto_bath_repair:
        from autowsgr.ops.repair import repair_one_available

        blacklist = cfg.bath_repair_blacklist
        scheduler.register(
            TaskSpec(
                name='浴室修理',
                runner_factory=lambda c, bl=blacklist: FunctionRunner(
                    lambda: repair_one_available(c, blacklist=bl),
                    name='浴室修理',
                ),
                success_schedule=IntervalSchedule(poll_interval),
                priority=PRIO_BATH_REPAIR,
                failure_interval=300.0,
            ),
        )

    if not scheduler.tasks:
        _log.warning('[wsgr_daily] 未启用任何日常任务, run() 将直接退出')


def run_daily_automation(
    ctx: GameContext,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    **scheduler_kwargs,
) -> None:
    """一站式日常挂机: 构造定时调度器 → 构建 WSGR 日常计划 → 常驻运行。

    ``scheduler_kwargs`` 透传给 :class:`TimedScheduler` (``hoarding`` /
    ``max_consecutive_failures`` / ``recovery_cooldown`` /
    ``auto_launch_emulator`` / ``state_path`` / ``when_queue_empty`` 等)。

    推荐显式传 ``state_path`` 持久化调度进度 (脚本重启后不错过周期)::

        run_daily_automation(ctx, state_path='logs/timed_state.json')
    """
    scheduler = TimedScheduler(ctx, **scheduler_kwargs)
    build_wsgr_daily_plan(scheduler, ctx, poll_interval=poll_interval)
    scheduler.run()


#: 兼容性别名 (与 daily_plan.build_daily_plan 命名风格对齐)
build_daily_plan_timed = build_wsgr_daily_plan

__all__ = [
    'DEFAULT_POLL_INTERVAL',
    'DROP_RESET_TIME',
    'EXERCISE_RESET_TIMES',
    'FIGHT_NEXT_INTERVAL',
    'NormalFightRotator',
    'build_daily_plan_timed',
    'build_wsgr_daily_plan',
    'run_daily_automation',
]
