"""wsgr_daily 测试 — WSGR 日常计划构建与常规战轮转 (无设备)。"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from autowsgr.combat import CombatResult
from autowsgr.infra.config import DailyAutomationConfig
from autowsgr.scheduler.timed_scheduler import (
    DailySchedule,
    IntervalSchedule,
    TimedScheduler,
)
from autowsgr.scheduler.triggers import NormalFightPlan
from autowsgr.scheduler.wsgr_daily import (
    EXERCISE_RESET_TIMES,
    NormalFightRotator,
    build_wsgr_daily_plan,
    run_daily_automation,
)
from autowsgr.types import ConditionFlag


def _cfg(**kwargs) -> DailyAutomationConfig:
    """构造 DailyAutomationConfig (frozen 模型)。

    ``normal_fight_tasks`` 传 dict 列表 — 校验器只接受 classic 列表/字符串/
    dict 写法, 不接受已构造的 ``NormalFightTaskConfig`` 实例。
    """
    tasks = kwargs.pop('normal_fight_tasks', None)
    if tasks is not None:
        kwargs['normal_fight_tasks'] = [{'name': t} if isinstance(t, str) else t for t in tasks]
    return DailyAutomationConfig(**kwargs)


def _dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """测试用朴素时间构造器 (调度器用本地墙上时钟, 全域无 tz)。"""
    return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001


def _ctx(cfg: DailyAutomationConfig, **overrides: object) -> SimpleNamespace:
    fields = {
        'stop_event': threading.Event(),
        'active_fight_tasks': 0,
        'dropped_ship_count': 0,
        'dropped_loot_count': 0,
        'quick_repair_used': 0,
        'ctrl': object(),
        'config': SimpleNamespace(
            account=SimpleNamespace(game_app='com.test.app'),
            daily_automation=cfg,
            plan_root=None,
        ),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _scheduler(ctx: SimpleNamespace, **kwargs: object) -> TimedScheduler:
    return TimedScheduler(
        ctx,
        clock=lambda: _dt(2026, 10, 12, 12, 0, 0),
        sleeper=lambda _s: None,
        **kwargs,
    )


class TestBuildWsgrDailyPlan:
    def test_all_enabled_registers_six_tasks(self) -> None:
        """全启用 → 6 个任务, 优先级与 daily_plan 触发器模式一致。"""
        cfg = _cfg(normal_fight_tasks=['8-5AI'])
        ctx = _ctx(cfg)
        scheduler = _scheduler(ctx)
        build_wsgr_daily_plan(scheduler, ctx)
        got = {t.name: t for t in scheduler.tasks}
        assert set(got) == {'远征', '任务奖励', '战役/困难潜艇', '演习', '常规战', '浴室修理'}
        priorities = {name: t.priority for name, t in got.items()}
        assert priorities == {
            '远征': 0,
            '任务奖励': 1,
            '战役/困难潜艇': 5,
            '演习': 10,
            '常规战': 100,
            '浴室修理': 200,
        }

    def test_daily_reset_times_follow_wiki(self) -> None:
        """战役 0 点刷新 → 耗尽后回次日 00:05; 演习 0/12/18 三时段。"""
        cfg = _cfg(normal_fight_tasks=['8-5AI'])
        ctx = _ctx(cfg)
        scheduler = _scheduler(ctx)
        build_wsgr_daily_plan(scheduler, ctx)
        by_name = {t.name: t for t in scheduler.tasks}
        campaign = by_name['战役/困难潜艇'].spec
        exercise = by_name['演习'].spec
        assert isinstance(campaign.exhausted_schedule, DailySchedule)
        assert campaign.exhausted_schedule.times == ('00:05',)
        assert exercise.exhausted_schedule.times == EXERCISE_RESET_TIMES
        # 战斗类任务成功间隔短 (场间可插入远征), 远征/奖励/修理为 10 分钟轮询
        assert isinstance(campaign.success_schedule, IntervalSchedule)
        assert campaign.success_schedule.seconds == 60
        assert by_name['远征'].spec.success_schedule.seconds == 600

    def test_all_disabled_registers_nothing(self) -> None:
        cfg = _cfg(
            auto_expedition=False,
            auto_gain_bonus=False,
            auto_bath_repair=False,
            auto_battle=False,
            auto_exercise=False,
            auto_normal_fight=False,
        )
        ctx = _ctx(cfg)
        scheduler = _scheduler(ctx)
        build_wsgr_daily_plan(scheduler, ctx)
        assert scheduler.tasks == []

    def test_no_daily_automation_config(self) -> None:
        ctx = _ctx(None)
        scheduler = _scheduler(ctx)
        build_wsgr_daily_plan(scheduler, ctx)
        assert scheduler.tasks == []

    def test_normal_fight_tasks_required(self) -> None:
        """auto_normal_fight 开但任务列表空 → 不注册常规战, 其余不受影响。"""
        cfg = _cfg(auto_normal_fight=True, normal_fight_tasks=[])
        ctx = _ctx(cfg)
        scheduler = _scheduler(ctx)
        build_wsgr_daily_plan(scheduler, ctx)
        assert '常规战' not in {t.name for t in scheduler.tasks}
        assert len(scheduler.tasks) == 5

    def test_bad_normal_fight_plan_skipped(self) -> None:
        """单个计划解析失败只跳过该项, 不拖垮整体注册。"""
        cfg = _cfg(normal_fight_tasks=['不存在的计划'])
        ctx = _ctx(cfg)
        scheduler = _scheduler(ctx)
        build_wsgr_daily_plan(scheduler, ctx)
        assert '常规战' not in {t.name for t in scheduler.tasks}


class TestNormalFightRotator:
    def _plan(
        self,
        name: str,
        target: int | None = None,
        flag: ConditionFlag = ConditionFlag.OPERATION_SUCCESS,
        calls: list | None = None,
    ) -> NormalFightPlan:
        if calls is None:
            calls = []

        def factory(_ctx: object) -> SimpleNamespace:
            runner = SimpleNamespace()
            runner.run = lambda: (calls.append(name), CombatResult(flag=flag))[1]
            return runner

        return NormalFightPlan(factory=factory, name=name, fleet_id=1, target=target)

    def test_picks_unfinished_target_plan_first(self) -> None:
        """有 target 的未完成 plan 优先于无限 plan (镜像触发器语义)。"""
        calls: list[str] = []
        plans = [self._plan('无限A', None, calls=calls), self._plan('有限B', 2, calls=calls)]
        rotator = NormalFightRotator(plans, SimpleNamespace(quick_repair_used=0))
        rotator.run()
        assert calls == ['有限B']

    def test_round_robin_unlimited_plans(self) -> None:
        calls: list[str] = []
        plans = [self._plan('A', None, calls=calls), self._plan('B', None, calls=calls)]
        rotator = NormalFightRotator(plans, SimpleNamespace(quick_repair_used=0))
        rotator.run()
        rotator.run()
        assert calls == ['A', 'B']

    def test_all_done_returns_skip_fight(self) -> None:
        """今日目标全部完成 → SKIP_FIGHT (调度器据此顺延到次日重置点)。"""
        calls: list[str] = []
        plans = [self._plan('有限', 1, calls=calls)]
        rotator = NormalFightRotator(plans, SimpleNamespace(quick_repair_used=0))
        first = rotator.run()
        assert [r.flag for r in first] == [ConditionFlag.OPERATION_SUCCESS]
        assert plans[0].completed == 1
        second = rotator.run()
        assert [r.flag for r in second] == [ConditionFlag.SKIP_FIGHT]
        assert calls == ['有限']  # 没有多打

    def test_quick_repair_limit_stops_fighting(self) -> None:
        """快修达上限 → 今日不再出击 (与触发器模式口径一致)。"""
        calls: list[str] = []
        plans = [self._plan('A', None, calls=calls)]
        rotator = NormalFightRotator(
            plans, SimpleNamespace(quick_repair_used=50), quick_repair_limit=50
        )
        results = rotator.run()
        assert [r.flag for r in results] == [ConditionFlag.SKIP_FIGHT]
        assert calls == []

    def test_daily_reset_clears_completed(self) -> None:
        """跨日 → 各 plan 完成计数清零 (懒重置, 自包含不依赖调度器回调)。"""
        plans = [self._plan('有限', 1)]
        rotator = NormalFightRotator(plans, SimpleNamespace(quick_repair_used=0))
        rotator.run()
        assert plans[0].completed == 1
        rotator._reset_date = date.today() - timedelta(days=1)  # noqa: DTZ011  # 模拟已跨日
        results = rotator.run()
        assert [r.flag for r in results] == [ConditionFlag.OPERATION_SUCCESS]
        assert plans[0].completed == 1  # 重新计

    def test_unmet_grade_condition_not_counted(self) -> None:
        """配了战果条件的 plan: 未达标场次不计入次数。"""
        from autowsgr.combat import GradeCondition

        cond = GradeCondition(node='A', grade='S')
        calls: list[str] = []
        plans = [
            NormalFightPlan(
                factory=lambda _c: SimpleNamespace(
                    run=lambda: (
                        calls.append('x'),
                        CombatResult(flag=ConditionFlag.OPERATION_SUCCESS),
                    )[1]
                ),
                name='带条件',
                fleet_id=1,
                target=1,
                conditions=(cond,),
            )
        ]
        rotator = NormalFightRotator(plans, SimpleNamespace(quick_repair_used=0))
        rotator.run()
        # 该场没有对应节点战果数据 → 条件不满足 → 不计数
        assert plans[0].completed == 0
        assert calls == ['x']


class TestRunDailyAutomation:
    def test_stop_event_before_run_exits_immediately(self) -> None:
        cfg = _cfg(normal_fight_tasks=['8-5AI'])
        ctx = _ctx(cfg)
        ctx.stop_event.set()
        run_daily_automation(ctx)  # 不抛异常即通过
