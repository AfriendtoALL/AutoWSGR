"""TimedScheduler 单元测试 — 全程 mock 设备与 ops, 不碰真机。

测试纪律 (项目约定): 只断言「返回值等于预期」的是假测试。本文件全部用例
均断言**行为** (runner 调用次数/顺序/时刻、恢复函数被调用、next_run 计算值、
停止信号), 被测逻辑失效时必有断言失败。

时间源全部通过 ``clock`` / ``sleeper`` / ``monotonic`` 注入, 无真实 sleep。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from autowsgr.combat import CombatResult
from autowsgr.infra.exceptions import (
    ActionFailedError,
    CombatDecisionError,
    CombatRecognitionTimeoutError,
    ConfigError,
    CriticalError,
    DockFullError,
    EmulatorConnectionError,
    EmulatorNotFoundError,
    ImageNotFoundError,
    NavigationError,
    NetworkError,
    OCRError,
    PageNotFoundError,
    RepeatedActionError,
    ResourceError,
)
from autowsgr.scheduler import timed_scheduler as ts_mod
from autowsgr.scheduler.recovery import RecoveryOutcome
from autowsgr.scheduler.timed_scheduler import (
    DailySchedule,
    IntervalSchedule,
    Schedule,
    TaskSpec,
    TaskState,
    TimedScheduler,
    WeeklySchedule,
    next_daily_time,
    next_weekday_time,
)
from autowsgr.types import ConditionFlag


if TYPE_CHECKING:
    from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# 测试基础设施
# ═══════════════════════════════════════════════════════════════════════════════


def _dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    """测试用朴素时间构造器 (调度器用本地墙上时钟, 全域无 tz)。

    集中一处 ``noqa`` 而不是在 30 个构造点各写一遍。
    """
    return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001


class FakeClock:
    """可推进的墙上时钟。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class EventRecorder:
    """按序记录 ('run', name, time) / ('sleep', s) / ('recover_main',) 等事件。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add(self, *item: object) -> None:
        self.events.append(item)

    def names(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == 'run']

    def run_times(self) -> list[datetime]:
        return [e[2] for e in self.events if e[0] == 'run']

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e[0] == kind)


class ScriptedRunner:
    """按脚本返回结果 / 抛异常的 runner; 每次调用记录到 recorder。

    ``_script`` 由多个 runner 实例**共享** (不在 ``__init__`` 拷贝) ——
    任务每轮由工厂新建 runner, 只有共享列表才能让脚本跨轮消耗, 否则每轮都
    从头开始 (无停止条件的用例会因此挂死)。

    ``stop_when_done=True`` 时脚本耗尽即置停止信号 (单次执行类测试用)。
    """

    def __init__(
        self,
        script: list,
        ctx: SimpleNamespace,
        recorder: EventRecorder,
        clock: FakeClock,
        name: str = 'task',
        *,
        stop_when_done: bool = True,
    ) -> None:
        self._script = script
        self._ctx = ctx
        self._recorder = recorder
        self._clock = clock
        self._name = name
        self._stop_when_done = stop_when_done
        self.calls = 0

    def run(self) -> CombatResult:
        self.calls += 1
        self._recorder.add('run', self._name, self._clock.now())
        if not self._script:
            if self._stop_when_done:
                self._ctx.stop_event.set()
            return CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)
        item = self._script.pop(0)
        if not self._script and self._stop_when_done:
            self._ctx.stop_event.set()
        if isinstance(item, BaseException):
            raise item
        return item


def make_ctx(_clock: FakeClock, **overrides: object) -> SimpleNamespace:
    """构造假 GameContext (只含调度器实际读写的字段)。"""
    fields: dict = {
        'stop_event': threading.Event(),
        'active_fight_tasks': 0,
        'dropped_ship_count': 0,
        'dropped_loot_count': 0,
        'quick_repair_used': 0,
        'ctrl': object(),
        'config': SimpleNamespace(account=SimpleNamespace(game_app='com.test.app')),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_scheduler(
    ctx: SimpleNamespace,
    clock: FakeClock,
    recorder: EventRecorder,
    *,
    deadline: datetime | None = None,
    monotonic_value: float = 10_000.0,
    suppress_idle_goto: bool = True,
    **kwargs,
) -> TimedScheduler:
    """构造调度器: 注入时钟/睡眠, 并把恢复动作记到 recorder。

    ``deadline``: 假睡眠推进到该时刻后置停止信号 (替代 runner 自行停止,
    用于需要多轮执行的测试)。未提供时默认 1 小时 — 防止无停止条件的用例
    把测试挂死 (失败也比挂死好)。

    ``suppress_idle_goto``: 默认把空闲期行为降级为仅记录 —— 否则等待下一轮
    到期时的空闲 tick 也会回主页, 污染失败路径的 ``recover_main`` 计数。
    空闲期行为本身的测试显式传 ``False``。
    """
    if deadline is None:
        deadline = clock.now() + timedelta(hours=1)

    def sleeper(seconds: float) -> None:
        recorder.add('sleep', seconds)
        clock.advance(seconds)
        if clock.now() >= deadline:
            ctx.stop_event.set()

    scheduler = TimedScheduler(
        ctx,
        clock=clock.now,
        sleeper=sleeper,
        monotonic=lambda: monotonic_value,
        **kwargs,
    )
    # 恢复动作: 只记录, 不碰设备/页面 (真方法会 import ops 并操作模拟器)
    scheduler._try_recover_to_main = lambda: recorder.add('recover_main')  # type: ignore[method-assign]
    scheduler._restart_game_safely = lambda: recorder.add('restart_game')  # type: ignore[method-assign]
    scheduler._ensure_main_page_clean = lambda: recorder.add('overlay_check')  # type: ignore[method-assign]
    if suppress_idle_goto:
        scheduler._idle_tick = lambda: recorder.add('idle_tick')  # type: ignore[method-assign]
    return scheduler


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(_dt(2026, 10, 12, 12, 0, 0))  # 周一中午


@pytest.fixture
def recorder() -> EventRecorder:
    return EventRecorder()


@pytest.fixture(autouse=True)
def no_device_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """设备诊断/拉起一律 mock 掉 — 任何用例都不得碰真机。"""
    monkeypatch.setattr(
        ts_mod,
        'recover_device',
        lambda _ctx, _policy: RecoveryOutcome.NEEDS_GAME_RESTART,
    )
    monkeypatch.setattr(
        ts_mod,
        'maybe_launch_emulator',
        lambda _ctx, _policy: RecoveryOutcome.DEVICE_UNAVAILABLE,
    )


def _spec(
    name: str,
    script: list,
    recorder: EventRecorder,
    ctx: SimpleNamespace,
    clock: FakeClock,
    *,
    stop_when_done: bool = True,
    **kwargs: object,
) -> TaskSpec:
    """用 ScriptedRunner 工厂快速构造 TaskSpec。

    脚本列表由各轮 runner 共享 (跨轮消耗); 耗尽后按 ``stop_when_done`` 置
    停止信号或返回 success 占位。
    """
    shared = list(script)

    def factory(
        _ctx: object,
        _script: list = shared,
        _name: str = name,
        _stop: bool = stop_when_done,
    ) -> ScriptedRunner:
        return ScriptedRunner(_script, ctx, recorder, clock, name=_name, stop_when_done=_stop)

    kwargs.setdefault('success_schedule', IntervalSchedule(3600))
    return TaskSpec(name=name, runner_factory=factory, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# 调度规格 (Schedule) 纯函数
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchedules:
    def test_next_daily_time_nearest_today(self) -> None:
        now = _dt(2026, 10, 12, 10, 0, 0)
        assert next_daily_time(('03:00', '12:00'), now) == _dt(2026, 10, 12, 12, 0, 0)

    def test_next_daily_time_wraps_to_tomorrow(self) -> None:
        now = _dt(2026, 10, 12, 13, 0, 0)
        assert next_daily_time(('03:00', '12:00'), now) == _dt(2026, 10, 13, 3, 0, 0)

    def test_next_daily_time_empty_raises(self) -> None:
        with pytest.raises(ValueError, match='不能为空'):
            next_daily_time((), _dt(2026, 10, 12, 10, 0, 0))

    def test_next_daily_time_bad_format_raises(self) -> None:
        with pytest.raises(ValueError, match='超出范围'):
            next_daily_time(('25:00',), _dt(2026, 10, 12, 10, 0, 0))
        with pytest.raises(ValueError, match='HH:MM'):
            next_daily_time(('3点',), _dt(2026, 10, 12, 10, 0, 0))

    def test_next_weekday_time_same_week(self) -> None:
        monday = _dt(2026, 10, 12, 10, 0, 0)  # 周一
        assert next_weekday_time(2, '03:00', monday) == _dt(2026, 10, 14, 3, 0, 0)

    def test_next_weekday_time_wraps_a_week(self) -> None:
        wed_0400 = _dt(2026, 10, 14, 4, 0, 0)  # 周三已过 03:00
        assert next_weekday_time(2, '03:00', wed_0400) == _dt(2026, 10, 21, 3, 0, 0)

    def test_next_weekday_time_bad_weekday_raises(self) -> None:
        with pytest.raises(ValueError, match='weekday'):
            next_weekday_time(7, '03:00', _dt(2026, 10, 12, 10, 0, 0))

    def test_interval_schedule_adds_seconds(self) -> None:
        now = _dt(2026, 10, 12, 10, 0, 0)
        assert IntervalSchedule(90).next_after(now) == _dt(2026, 10, 12, 10, 1, 30)


# ═══════════════════════════════════════════════════════════════════════════════
# 主循环行为
# ═══════════════════════════════════════════════════════════════════════════════


class TestMainLoop:
    def test_due_task_runs_immediately(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """注册即到期 (ALAS NextRun 默认过去) → 立即执行一次。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                '任务A', [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)], recorder, ctx, clock
            ),
        )
        scheduler.run()
        assert recorder.names() == ['任务A']
        assert recorder.count('sleep') == 0  # 没空转

    def test_no_tasks_returns_immediately(self, clock: FakeClock, recorder: EventRecorder) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.run()
        assert recorder.events == []

    def test_duplicate_command_rejected(self, clock: FakeClock, recorder: EventRecorder) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(_spec('A', [], recorder, ctx, clock, command='dup'))
        with pytest.raises(ValueError, match='dup'):
            scheduler.register(_spec('B', [], recorder, ctx, clock, command='dup'))

    def test_waiting_task_not_run_early(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """日课跑完后必须等到当日时刻才再跑 — 提前执行会打乱每日刷新节奏。

        注册即立即执行 (ALAS NextRun 默认过去), 因此验证的是**第二轮**:
        12:00 跑完后 next_run 应为 13:00, 在 13:00 之前只空转不执行。
        """
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 12, 14, 0, 0))
        scheduler.register(
            _spec(
                '日报',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)] * 2,
                recorder,
                ctx,
                clock,
                success_schedule=DailySchedule(('13:00',)),
                exhausted_schedule=DailySchedule(('13:00',)),
            ),
        )
        scheduler.run()
        times = recorder.run_times()
        assert len(times) == 2
        assert times[0] == _dt(2026, 10, 12, 12, 0, 0)  # 注册即执行
        assert times[1] >= _dt(2026, 10, 12, 13, 0, 0)  # 等到时刻才跑

    def test_priority_order(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """到期任务按 priority 升序出队 (数值小先执行)。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 12, 12, 3, 0))
        scheduler.register(
            _spec(
                '低优先',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                priority=10,
                success_schedule=IntervalSchedule(60),
                stop_when_done=False,
            ),
        )
        scheduler.register(
            _spec(
                '高优先',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                priority=5,
                success_schedule=IntervalSchedule(60),
                stop_when_done=False,
            ),
        )
        scheduler.run()
        assert recorder.names()[:2] == ['高优先', '低优先']

    def test_task_call_forces_immediate_run(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """task_call 把 next_run 改到 now (ALAS task_call 语义)。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                '远务',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                success_schedule=DailySchedule(('23:59',)),
            ),
        )
        assert scheduler.task_call('远务') is True
        scheduler.run()
        assert recorder.names() == ['远务']

    def test_task_call_unknown_returns_false(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        assert scheduler.task_call('不存在') is False
        assert scheduler.task_delay('不存在', 10) is False

    def test_stop_event_halts_before_running(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        ctx.stop_event.set()
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec('A', [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)], recorder, ctx, clock),
        )
        scheduler.run()
        assert recorder.events == []


# ═══════════════════════════════════════════════════════════════════════════════
# 结局分类与 next_run 计算
# ═══════════════════════════════════════════════════════════════════════════════


class TestOutcomeScheduling:
    def test_success_reschedules_by_interval(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                'A',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                success_schedule=IntervalSchedule(600),
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.next_run - state.last_run_at == timedelta(seconds=600)
        assert state.failure_count == 0

    def test_exhausted_uses_exhausted_schedule(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """战役次数用尽 → 顺延到次日重置点, 且不计失败。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                '战役',
                [CombatResult(flag=ConditionFlag.BATTLE_TIMES_EXCEED)],
                recorder,
                ctx,
                clock,
                success_schedule=IntervalSchedule(60),
                exhausted_schedule=DailySchedule(('00:05',)),
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.last_outcome.value == 'exhausted'
        assert state.next_run == _dt(2026, 10, 13, 0, 5, 0)
        assert state.failure_count == 0

    def test_exhausted_without_schedule_falls_back_to_success(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                '演习',
                [CombatResult(flag=ConditionFlag.SKIP_FIGHT)],
                recorder,
                ctx,
                clock,
                success_schedule=IntervalSchedule(300),
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.next_run - state.last_run_at == timedelta(seconds=300)

    def test_dock_full_not_counted_as_failure(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """船坞满是正常资源信号 — 不计失败、不触发熔断 (用户指定口径)。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                '常规战',
                [CombatResult(flag=ConditionFlag.DOCK_FULL, dock_full_destroyed=False)],
                recorder,
                ctx,
                clock,
                dock_full_interval=1800,
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.last_outcome.value == 'dock_full'
        assert state.failure_count == 0
        assert state.next_run - state.last_run_at == timedelta(seconds=1800)
        assert recorder.count('recover_main') == 0

    def test_dock_full_destroyed_short_retry(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """解装完毕的轮 (未开打) → 5 秒短重试, 不占失败。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec(
                '常规战',
                [CombatResult(flag=ConditionFlag.DOCK_FULL, dock_full_destroyed=True)],
                recorder,
                ctx,
                clock,
                dock_full_interval=1800,
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.failure_count == 0
        assert state.next_run - state.last_run_at == timedelta(seconds=ts_mod.DOCK_RETRY_DELAY)

    def test_failure_flags_exclude_dock_full(self) -> None:
        """锁定失败判定口径: DOCK_FULL/SKIP_FIGHT/混合结果都不是失败。"""
        assert ConditionFlag.DOCK_FULL not in ts_mod._FAILURE_FLAGS
        assert ConditionFlag.SKIP_FIGHT not in ts_mod._FAILURE_FLAGS
        assert ConditionFlag.ACTION_FAILED in ts_mod._FAILURE_FLAGS
        assert ConditionFlag.SL in ts_mod._FAILURE_FLAGS

    def test_mixed_results_are_success(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """一轮里部分成功部分失败的混合结果有产出, 不算失败轮。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            TaskSpec(
                name='三连',
                runner_factory=lambda _c: SimpleNamespace(
                    run=lambda: [
                        CombatResult(flag=ConditionFlag.OPERATION_SUCCESS),
                        CombatResult(flag=ConditionFlag.SL),
                    ]
                ),
                success_schedule=IntervalSchedule(60),
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.last_outcome.value == 'success'
        assert state.failure_count == 0

    def test_empty_results_is_failure(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """无结果的轮 (任务未真正执行) 按失败处理, 累积到阈值即接管。"""
        ctx = make_ctx(clock)
        takeovers = []
        scheduler = make_scheduler(
            ctx, clock, recorder, on_human_takeover=lambda _s, _r: takeovers.append(_r)
        )
        scheduler.register(
            TaskSpec(
                name='空跑',
                runner_factory=lambda _c: SimpleNamespace(run=list),
                success_schedule=IntervalSchedule(60),
                max_failures=2,
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.last_outcome.value == 'failed'
        assert state.failure_count == 2  # 两轮空结果即达单任务阈值
        assert len(takeovers) == 1
        assert ctx.stop_event.is_set()


# ═══════════════════════════════════════════════════════════════════════════════
# 失败处置: 退避 / 单任务熔断 / 全局熔断
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailureHandling:
    def test_failure_backoff_and_recover_main(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """未达全局阈值 → 轻量恢复回主页 + 指数退避 (防高速空转)。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, max_consecutive_failures=5)
        scheduler.register(
            _spec(
                'A',
                [CombatResult(flag=ConditionFlag.ACTION_FAILED)],
                recorder,
                ctx,
                clock,
                failure_interval=300,
            ),
        )
        scheduler.run()
        state = scheduler.tasks[0]
        assert state.failure_count == 1
        assert state.next_run - state.last_run_at == timedelta(seconds=300)
        assert recorder.count('recover_main') == 1
        assert 5.0 in [e[1] for e in recorder.events if e[0] == 'sleep']  # backoff 5*2^0

    def test_per_task_max_failures_human_takeover(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """同一任务连续失败达 max_failures → 人工接管 (ALAS failure_record)。"""
        ctx = make_ctx(clock)
        takeovers: list[tuple[str, str]] = []
        scheduler = make_scheduler(
            ctx,
            clock,
            recorder,
            max_consecutive_failures=99,  # 关掉全局熔断, 隔离验证单任务轨
            on_human_takeover=lambda state, reason: takeovers.append((state.name, reason)),
        )
        scheduler.register(
            _spec(
                '顽疾',
                [CombatResult(flag=ConditionFlag.ACTION_FAILED)] * 2,
                recorder,
                ctx,
                clock,
                max_failures=2,
                failure_interval=60,
            ),
        )
        scheduler.run()
        assert len(takeovers) == 1
        name, reason = takeovers[0]
        assert name == '顽疾'
        assert '连续失败 2 次' in reason
        assert ctx.stop_event.is_set()  # 默认模式: 停止调度而非杀进程

    def test_human_takeover_raise_mode(self, clock: FakeClock, recorder: EventRecorder) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(
            ctx, clock, recorder, max_consecutive_failures=99, human_takeover_mode='raise'
        )
        scheduler.register(
            _spec(
                '顽疾',
                [CombatResult(flag=ConditionFlag.ACTION_FAILED)],
                recorder,
                ctx,
                clock,
                max_failures=1,
                failure_interval=60,
            ),
        )
        with pytest.raises(ts_mod.HumanTakeoverError, match='顽疾'):
            scheduler.run()

    def test_global_circuit_breaker_restarts_game_once(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """全局连续失败达阈值 → 重启游戏; 冷却期内不重复重启。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(
            ctx, clock, recorder, max_consecutive_failures=2, recovery_cooldown=60
        )
        scheduler.register(
            _spec(
                'A',
                [CombatResult(flag=ConditionFlag.ACTION_FAILED)] * 3,
                recorder,
                ctx,
                clock,
                max_failures=99,
                failure_interval=60,
            ),
        )
        scheduler.run()
        # 第 1 次失败: 恢复主页; 第 2 次: 重启 (计数清零); 第 3 次: 又只剩 1 次 → 恢复主页
        assert recorder.count('restart_game') == 1
        assert recorder.count('recover_main') == 2

    def test_restart_cooldown_blocks_second_restart(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(
            ctx, clock, recorder, max_consecutive_failures=2, recovery_cooldown=3600
        )
        scheduler.register(
            _spec(
                'A',
                [CombatResult(flag=ConditionFlag.ACTION_FAILED)] * 4,
                recorder,
                ctx,
                clock,
                max_failures=99,
                failure_interval=60,
            ),
        )
        scheduler.run()
        # monotonic 恒定 → 第二次达阈值时仍在冷却期, 不再重启
        assert recorder.count('restart_game') == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 异常 → 恢复动作映射
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptionMapping:
    def test_map_exception_table(self) -> None:
        cases = {
            EmulatorConnectionError('x'): 'reconnect_device',
            EmulatorNotFoundError('x'): 'reconnect_device',
            RepeatedActionError('x'): 'restart_game',
            PageNotFoundError('x'): 'goto_main',
            NavigationError('a', 'b'): 'goto_main',
            ActionFailedError('x'): 'goto_main',
            NetworkError('x'): 'retry_later',
            ResourceError('x'): 'retry_later',
            OCRError('x'): 'retry_later',
            ImageNotFoundError('t', 1.0): 'retry_later',
            CombatRecognitionTimeoutError(['a'], 1.0): 'retry_later',
            CombatDecisionError('x'): 'retry_later',
            DockFullError('x'): 'dock_full',
            ConfigError('x'): 'human_takeover',
            CriticalError('x'): 'human_takeover',
            ValueError('x'): 'failed',  # 未识别 → 通用失败
        }
        for exc, expected in cases.items():
            assert ts_mod.map_exception(exc).value == expected, type(exc).__name__

    def test_subclass_matches_parent_entry(self) -> None:
        """子类异常按 MRO 落到父类表项 — 新增子类不查表也有合理动作。"""

        class _SubError(EmulatorConnectionError):
            pass

        assert ts_mod.map_exception(_SubError('x')).value == 'reconnect_device'

    def test_device_exception_triggers_reconnect(
        self, clock: FakeClock, recorder: EventRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """设备异常 → 走 L2 重连 (无副作用), 而不是重启游戏。"""
        calls = {'recover': 0}

        def fake_recover(_ctx: object, _policy: object) -> RecoveryOutcome:
            calls['recover'] += 1
            return RecoveryOutcome.RECOVERED

        monkeypatch.setattr(ts_mod, 'recover_device', fake_recover)
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec('A', [EmulatorConnectionError('offline')], recorder, ctx, clock),
        )
        scheduler.run()
        assert calls['recover'] == 1
        assert recorder.count('restart_game') == 0
        assert scheduler.tasks[0].failure_count == 1  # 重连成功但本轮仍无产出

    def test_page_exception_triggers_goto_main(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec('A', [PageNotFoundError('未知页面')], recorder, ctx, clock),
        )
        scheduler.run()
        assert recorder.count('recover_main') == 1

    def test_critical_exception_immediate_takeover(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """CriticalError 重试无意义 → 直接人工接管, 不计失败轮。"""
        takeovers = []
        ctx = make_ctx(clock)
        scheduler = make_scheduler(
            ctx, clock, recorder, on_human_takeover=lambda _s, _r: takeovers.append(_r)
        )
        scheduler.register(
            _spec('A', [CriticalError('炸了')], recorder, ctx, clock),
        )
        scheduler.run()
        assert len(takeovers) == 1
        assert 'CriticalError' in takeovers[0]
        assert scheduler.tasks[0].failure_count == 0

    def test_retry_later_exception_no_page_action(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """网络类异常只退避, 不碰页面/设备。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec('A', [NetworkError('断线')], recorder, ctx, clock),
        )
        scheduler.run()
        assert recorder.count('recover_main') == 0
        assert recorder.count('restart_game') == 0
        assert scheduler.tasks[0].failure_count == 1

    def test_dock_full_exception_classified_as_dock_full(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            _spec('A', [DockFullError('满')], recorder, ctx, clock),
        )
        scheduler.run()
        assert scheduler.tasks[0].last_outcome.value == 'dock_full'
        assert scheduler.tasks[0].failure_count == 0

    def test_runner_factory_failure_is_failed_round(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """工厂构造失败 (如计划文件缺失) 也按失败轮处理, 不逃出主循环。"""
        ctx = make_ctx(clock)

        def bad_factory(_c: object) -> object:
            raise FileNotFoundError('plan.yaml')

        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler.register(
            TaskSpec(
                name='坏工厂', runner_factory=bad_factory, success_schedule=IntervalSchedule(60)
            ),
        )
        scheduler.run()
        assert scheduler.tasks[0].last_outcome.value == 'failed'


# ═══════════════════════════════════════════════════════════════════════════════
# 同期任务批量入队 (WSGR 各重置时刻已对齐, 无需 ALAS 式囤积延迟)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNaturalBatching:
    def test_same_deadline_tasks_run_back_to_back(
        self, clock: FakeClock, recorder: EventRecorder
    ) -> None:
        """同一重置时刻到期的任务连续执行, 中间不插入空闲等待。

        WSGR 的战役/演习/常规战顺延点都指向 00:05 (见 wsgr_daily), 到期时刻
        天然对齐 —— 同期入队即批量, 这正是不移植 ALAS 囤积机制的原因。
        """
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 13, 0, 10, 0))
        for name in ('战役', '演习'):
            scheduler.register(
                _spec(
                    name,
                    [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                    recorder,
                    ctx,
                    clock,
                    success_schedule=IntervalSchedule(3600),
                    exhausted_schedule=DailySchedule(('00:05',)),
                    stop_when_done=False,
                ),
            )
        # 注册即执行发生在 12:00; 把 next_run 改到次日 00:05 模拟「打满顺延」
        scheduler.task_delay('战役', seconds=12 * 3600)
        scheduler.task_delay('演习', seconds=12 * 3600)
        recorder.events.clear()
        scheduler.run()
        names = recorder.names()
        assert names[:2] == ['战役', '演习']
        run_indices = [i for i, e in enumerate(recorder.events) if e[0] == 'run']
        assert all(
            recorder.events[i][0] != 'sleep' for i in range(run_indices[0] + 1, run_indices[1])
        )

    def test_interval_cadence_exact(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """间隔任务的节奏精确等于间隔 (无任何额外延迟)。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 12, 12, 2, 30))
        scheduler.register(
            _spec(
                '远征',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                success_schedule=IntervalSchedule(60),
                stop_when_done=False,
            ),
        )
        scheduler.run()
        times = recorder.run_times()
        assert len(times) >= 2
        assert times[1] - times[0] == timedelta(seconds=60)


# ═══════════════════════════════════════════════════════════════════════════════
# 跨日重置与状态持久化
# ═══════════════════════════════════════════════════════════════════════════════


class TestDailyResetAndState:
    def test_daily_counters_reset_on_date_change(self, recorder: EventRecorder) -> None:
        """跨 0 点 → ctx 每日计数器清零 (游戏 0 点刷新掉落计数)。"""
        clock = FakeClock(_dt(2026, 10, 12, 23, 59, 50))
        ctx = make_ctx(clock, dropped_ship_count=42, dropped_loot_count=7)
        seen: list[int] = []

        class _ProbeRunner:
            def run(self) -> CombatResult:
                seen.append(ctx.dropped_ship_count)
                if len(seen) >= 2:
                    ctx.stop_event.set()  # 只看前两轮: 跨日前后各一次
                return CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)

        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 13, 0, 5, 0))
        scheduler.register(
            TaskSpec(
                name='探针',
                runner_factory=lambda _c: _ProbeRunner(),
                success_schedule=IntervalSchedule(30),
            ),
        )
        scheduler.run()
        # 第一次 23:59:50 计数仍是 42; 第二次在 00:00:20 之后, 已被清零
        assert seen == [42, 0]

    def test_state_persistence_roundtrip(
        self, clock: FakeClock, recorder: EventRecorder, tmp_path: Path
    ) -> None:
        """持久化: 重启脚本后各任务不错过周期 (ALAS NextRun 落盘)。"""
        state_file = tmp_path / 'timed_state.json'
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler._state_path = str(state_file)
        scheduler.register(
            _spec(
                '远征',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                success_schedule=IntervalSchedule(3600),
            ),
        )
        scheduler.run()
        assert state_file.exists()
        data = json.loads(state_file.read_text(encoding='utf-8'))
        assert '远征' in data['tasks']

        # 新调度器读同一状态文件 → 任务未到期, 短截止内不再执行
        clock2 = FakeClock(clock.now() + timedelta(seconds=600))
        ctx2 = make_ctx(clock2)
        recorder2 = EventRecorder()
        scheduler2 = make_scheduler(
            ctx2, clock2, recorder2, deadline=clock2.now() + timedelta(seconds=300)
        )
        scheduler2._state_path = str(state_file)
        scheduler2.register(
            _spec(
                '远征',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder2,
                ctx2,
                clock2,
                success_schedule=IntervalSchedule(3600),
            ),
        )
        scheduler2.run()
        assert recorder2.count('run') == 0

    def test_corrupt_state_file_ignored(
        self, clock: FakeClock, recorder: EventRecorder, tmp_path: Path
    ) -> None:
        """坏状态文件按新会话处理, 不崩溃 (尽力而为)。"""
        state_file = tmp_path / 'bad.json'
        state_file.write_text('{不是 json', encoding='utf-8')
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder)
        scheduler._state_path = str(state_file)
        scheduler.register(
            _spec('A', [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)], recorder, ctx, clock),
        )
        scheduler.run()  # 不抛异常即通过
        assert recorder.count('run') == 1

    def test_unknown_state_keys_dropped(
        self, clock: FakeClock, recorder: EventRecorder, tmp_path: Path
    ) -> None:
        state_file = tmp_path / 'unknown.json'
        state_file.write_text(
            json.dumps({'tasks': {'A': {'next_run': '2099-01-01 00:00:00'}, '幽灵': {}}}),
            encoding='utf-8',
        )
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 12, 12, 5, 0))
        scheduler._state_path = str(state_file)
        scheduler.register(
            _spec('A', [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)], recorder, ctx, clock),
        )
        scheduler.run()
        # A 的 next_run 被恢复为 2099 → 不该执行; 「幽灵」键被忽略
        assert recorder.count('run') == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 空闲期行为
# ═══════════════════════════════════════════════════════════════════════════════


class TestIdleBehavior:
    def _register_idle_task(
        self,
        scheduler: TimedScheduler,
        ctx: SimpleNamespace,
        clock: FakeClock,
        recorder: EventRecorder,
    ) -> None:
        scheduler.register(
            _spec(
                '晚报',
                [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],
                recorder,
                ctx,
                clock,
                success_schedule=DailySchedule(('23:00',)),
            ),
        )
        # 注册即执行的语义会把首次运行提前到当下 — 推到来验证空闲期行为
        scheduler.task_delay('晚报', seconds=3600)

    def test_goto_main_on_entering_idle(self, clock: FakeClock, recorder: EventRecorder) -> None:
        """队列排空 → 回一次主页 (含每日浮层处理), 不每轮重复。"""
        ctx = make_ctx(clock)
        scheduler = make_scheduler(
            ctx,
            clock,
            recorder,
            deadline=_dt(2026, 10, 12, 12, 0, 30),
            suppress_idle_goto=False,
        )
        self._register_idle_task(scheduler, ctx, clock, recorder)
        scheduler.run()
        assert recorder.count('run') == 0  # 到期前不执行
        assert recorder.count('recover_main') == 1  # 只做一次
        assert recorder.count('overlay_check') == 1
        assert recorder.count('sleep') >= 1

    def test_stay_there_does_nothing(self, clock: FakeClock, recorder: EventRecorder) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(
            ctx,
            clock,
            recorder,
            deadline=_dt(2026, 10, 12, 12, 0, 20),
            when_queue_empty='stay_there',
        )
        self._register_idle_task(scheduler, ctx, clock, recorder)
        scheduler.run()
        assert recorder.count('recover_main') == 0
        assert recorder.count('sleep') >= 1

    def test_invalid_when_queue_empty_rejected(self, clock: FakeClock) -> None:
        ctx = make_ctx(clock)
        with pytest.raises(ValueError, match='when_queue_empty'):
            TimedScheduler(ctx, when_queue_empty='跳舞')


# ═══════════════════════════════════════════════════════════════════════════════
# 调度规格与 TaskSpec 校验
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskSpec:
    def test_command_defaults_to_name(self) -> None:
        spec = TaskSpec(
            name='战役',
            runner_factory=lambda _c: None,
            success_schedule=IntervalSchedule(60),
        )
        assert spec.command == '战役'

    def test_task_state_fields(self) -> None:
        spec = TaskSpec(
            name='A',
            runner_factory=lambda _c: None,
            success_schedule=IntervalSchedule(60),
            priority=7,
        )
        state = TaskState(spec=spec, next_run=_dt(2026, 1, 1))
        assert state.name == 'A'
        assert state.priority == 7
        assert state.failure_count == 0

    def test_schedule_protocol_conformance(self) -> None:
        """三种 Schedule 都满足协议 (静态结构检查的运行时兜底)。"""
        for schedule in (
            IntervalSchedule(1),
            DailySchedule(('00:00',)),
            WeeklySchedule(0, '00:00'),
        ):
            assert isinstance(schedule, Schedule)

    def test_task_disable_skips_task(self, clock: FakeClock, recorder: EventRecorder) -> None:
        ctx = make_ctx(clock)
        scheduler = make_scheduler(ctx, clock, recorder, deadline=_dt(2026, 10, 12, 12, 0, 20))
        scheduler.register(
            _spec('A', [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)], recorder, ctx, clock),
        )
        assert scheduler.task_disable('A') is True
        scheduler.run()
        assert recorder.count('run') == 0
