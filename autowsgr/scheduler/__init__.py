"""调度层 — 启动编排与任务调度。

提供一站式启动入口和 GameContext 构造：

- :func:`launch` — 加载配置 → 连接模拟器 → 启动游戏 → 返回就绪的 GameContext
- :class:`Launcher` — 可定制的启动器
- :class:`TaskScheduler` — 基础任务调度器 (顺序执行 + 远征定时检查 + 触发器调度)
- :class:`FightTask` — 战斗任务描述
- :func:`build_daily_plan` — 把 ``daily_automation`` 配置翻译成触发器 (auto_daily)
- :class:`TimedScheduler` — ALAS 风格定时调度器 (每任务 NextRun + 熔断 + 持久化)
- :func:`build_wsgr_daily_plan` — 把 ``daily_automation`` 配置翻译成定时任务

典型用法::

    from autowsgr.scheduler import launch, TaskScheduler, FightTask

    ctx = launch("user_settings.yaml")
    scheduler = TaskScheduler(ctx, expedition_interval=900)
    scheduler.add(FightTask(runner=my_runner, times=30))
    scheduler.run()

auto_daily 全天挂机 (触发器模式)::

    from autowsgr.scheduler import launch, TaskScheduler, build_daily_plan

    ctx = launch("user_settings.yaml")
    scheduler = TaskScheduler(ctx, expedition_interval=0)  # 远征交给触发器
    build_daily_plan(scheduler, ctx)
    scheduler.run_daily()

auto_daily 全天挂机 (ALAS 定时模式, 支持跨重启持久化)::

    from autowsgr.scheduler import launch
    from autowsgr.scheduler.wsgr_daily import run_daily_automation

    ctx = launch("user_settings.yaml")
    run_daily_automation(ctx, state_path="logs/timed_state.json")
"""

from .daily_plan import build_daily_plan
from .launcher import Launcher, launch
from .plan_runner import run_yaml_plan
from .scheduler import BatchRunnerAdapter, FightTask, TaskScheduler
from .timed_scheduler import (
    DailySchedule,
    HumanTakeoverError,
    IntervalSchedule,
    RecoveryAction,
    TaskOutcome,
    TaskSpec,
    TaskState,
    TimedScheduler,
    WeeklySchedule,
    map_exception,
)
from .wsgr_daily import build_wsgr_daily_plan, run_daily_automation


__all__ = [
    'BatchRunnerAdapter',
    'DailySchedule',
    'FightTask',
    'HumanTakeoverError',
    'IntervalSchedule',
    'Launcher',
    'RecoveryAction',
    'TaskOutcome',
    'TaskScheduler',
    'TaskSpec',
    'TaskState',
    'TimedScheduler',
    'WeeklySchedule',
    'build_daily_plan',
    'build_wsgr_daily_plan',
    'launch',
    'map_exception',
    'run_daily_automation',
    'run_yaml_plan',
]
