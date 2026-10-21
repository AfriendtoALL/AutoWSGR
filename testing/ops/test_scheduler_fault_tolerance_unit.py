"""调度器故障恢复与熔断 (T2-1) 的无设备单元测试。

背景: ``recover_to_main_or_restart()`` 定义在 ``ops/startup.py:304``, 功能完整
(含重启游戏), 但此前 ``run_daily()`` 主循环**从不调用**它。而子任务异常在
``_run_task`` 内部就被捕获并转成 ``ACTION_FAILED`` (scheduler.py:323-334),
**异常不会传播到调用方** —— 于是设备掉线 / 游戏闪退 / 网络断时:

    goto_page 抛 NavigationError
      → _run_task 吞成 ACTION_FAILED
      → 触发器 on_done 只翻 _idle=True
      → 下一轮继续产出任务 → 再失败 → **无限空转, 日志持续写盘**

``logs/`` 下 18+ 个时间戳目录即为此现象留痕。

本次加入两级处置: 未达阈值 → 轻量恢复回主页面; 连续失败达阈值(默认 5,
用户 2026-10-11 指定) → 熔断重启游戏, 并受冷却期保护。

覆盖:
- 失败判定口径 (全失败/ 空结果 / 混合结果 / DOCK_FULL 不算失败)
- 阈值未达 → 只轻量恢复, 不重启
- 达到阈值 → 重启一次
- 冷却期内 → 不重复重启
- 成功后计数归零 (不跨正常轮累积)
- 恢复与重启自身抛异常时主循环继续
"""

from __future__ import annotations

import threading
import types
from unittest.mock import MagicMock

import pytest

from autowsgr.combat import CombatResult
from autowsgr.scheduler.scheduler import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_RECOVERY_COOLDOWN,
    TaskScheduler,
)
from autowsgr.types import ConditionFlag


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════════════


def _make_ctx() -> MagicMock:
    """最小 GameContext 替身: stop_event + config.account.game_app。"""
    ctx = MagicMock()
    ctx.stop_event = threading.Event()
    ctx.active_fight_tasks = 0
    ctx.config.account.game_app = 'official'
    return ctx


def _make_scheduler(**kwargs) -> TaskScheduler:
    """构造调度器, 并把两个恢复动作替换为计数 mock。

    同时把失败退避的 sleep 打成 0 —— 调度器在失败时会指数退避
    (5→10→20→40s), 不屏蔽的话单个用例就会真实等待 75秒。
    退避逻辑本身由``testing/ops/test_device_recovery_unit.py::TestBackoff``
    单独断言。
    """
    sched = TaskScheduler(_make_ctx(), **kwargs)
    sched._try_recover_to_main = MagicMock()  # type: ignore[method-assign]
    sched._restart_game_safely = MagicMock()  # type: ignore[method-assign]
    sched._recovery_policy.backoff_base = 0.0
    sched._recovery_policy.backoff_max = 0.0
    return sched


def _task() -> types.SimpleNamespace:
    return types.SimpleNamespace(name='test_task', on_done=None)


def _fail_round(sched: TaskScheduler, task) -> None:
    """模拟一次「跑完但结果全是失败」的调度。"""
    results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]
    sched._consecutive_failures += 1
    sched._handle_failure(task, results, crashed=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 失败判定口径
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsFailedRound:
    """_is_failed_round 的判定口径。"""

    def test_empty_results_is_failure(self) -> None:
        """无结果 (任务未真正执行) 算失败。"""
        assert TaskScheduler._is_failed_round([]) is True

    def test_all_action_failed_is_failure(self) -> None:
        results = [
            CombatResult(flag=ConditionFlag.ACTION_FAILED),
            CombatResult(flag=ConditionFlag.ACTION_FAILED),
        ]
        assert TaskScheduler._is_failed_round(results) is True

    def test_all_sl_is_failure(self) -> None:
        """SL 表示状态机反复无法推进, 也算失败。"""
        results = [CombatResult(flag=ConditionFlag.SL)]
        assert TaskScheduler._is_failed_round(results) is True

    def test_mixed_is_not_failure(self) -> None:
        """混合结果 (A/C 成功 D 点 SL) 不算失败 —— 该轮仍有产出。"""
        results = [
            CombatResult(flag=ConditionFlag.OPERATION_SUCCESS),
            CombatResult(flag=ConditionFlag.OPERATION_SUCCESS),
            CombatResult(flag=ConditionFlag.SL),
        ]
        assert TaskScheduler._is_failed_round(results) is False

    def test_dock_full_is_not_failure(self) -> None:
        """船坞满是正常资源信号, 绝不能计为故障 (否则会误重启游戏)。"""
        results = [CombatResult(flag=ConditionFlag.DOCK_FULL)]
        assert TaskScheduler._is_failed_round(results) is False

    def test_success_is_not_failure(self) -> None:
        results = [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)]
        assert TaskScheduler._is_failed_round(results) is False

    def test_skip_fight_is_not_failure(self) -> None:
        """主动撤退 (SKIP_FIGHT) 是策略行为, 不是故障。"""
        results = [CombatResult(flag=ConditionFlag.SKIP_FIGHT)]
        assert TaskScheduler._is_failed_round(results) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 熔断阈值
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailureCircuitBreaker:
    """连续失败达阈值才重启游戏。"""

    def test_default_threshold_is_five(self) -> None:
        """用户 2026-10-11 指定: 3 改为 5, 加大容错。"""
        assert DEFAULT_MAX_CONSECUTIVE_FAILURES == 5

    def test_below_threshold_only_light_recovery(self) -> None:
        """未达阈值 → 只轻量恢复, 绝不重启游戏。"""
        sched = _make_scheduler()
        task = _task()

        for i in range(1, 5):  # 第 1~4 次
            _fail_round(sched, task)
            assert sched._consecutive_failures == i
            assert sched._restart_game_safely.call_count == 0  # type: ignore[attr-defined]

        # 4 次都只做了轻量恢复
        assert sched._try_recover_to_main.call_count == 4  # type: ignore[attr-defined]

    def test_reaching_threshold_triggers_restart(self) -> None:
        """第 5 次(达阈值) → 触发重启。"""
        sched = _make_scheduler()
        task = _task()

        for _ in range(5):
            _fail_round(sched, task)

        assert sched._restart_game_safely.call_count == 1  # type: ignore[attr-defined]
        # 重启后计数归零, 让新连接从干净状态跑起
        assert sched._consecutive_failures == 0

    def test_restart_resets_counter(self) -> None:
        """重启后连续失败计数归零。"""
        sched = _make_scheduler()
        assert sched._consecutive_failures == 0
        assert sched._last_restart_at == 0.0

    def test_disabled_breaker_never_restarts(self) -> None:
        """max_consecutive_failures=0 → 禁用熔断, 只做轻量恢复。"""
        sched = _make_scheduler(max_consecutive_failures=0)
        task = _task()

        for _ in range(20):
            _fail_round(sched, task)

        assert sched._restart_game_safely.call_count == 0  # type: ignore[attr-defined]
        # 仍会尝试轻量恢复
        assert sched._try_recover_to_main.call_count == 20  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════════
# 冷却期
# ═══════════════════════════════════════════════════════════════════════════════


class TestRestartCooldown:
    """冷却期防止设备真掉线时反复重启。"""

    def test_cooldown_blocks_second_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """冷却期内再次达阈值 → 不重复重启。"""
        sched = _make_scheduler(recovery_cooldown=DEFAULT_RECOVERY_COOLDOWN)
        task = _task()

        clock = {'t': 1000.0}
        monkeypatch.setattr(
            'autowsgr.scheduler.scheduler.time.monotonic',
            lambda: clock['t'],
        )

        # 第一轮: 5 次失败 → 重启
        for _ in range(5):
            _fail_round(sched, task)
        assert sched._restart_game_safely.call_count == 1  # type: ignore[attr-defined]

        # 立刻再来 5 次 (仍在冷却期内) → 不再重启
        for _ in range(5):
            _fail_round(sched, task)
        assert sched._restart_game_safely.call_count == 1  # type: ignore[attr-defined]

        # 冷却期过去后 → 允许再次重启
        clock['t'] += DEFAULT_RECOVERY_COOLDOWN + 1
        for _ in range(5):
            _fail_round(sched, task)
        assert sched._restart_game_safely.call_count == 2  # type: ignore[attr-defined]

    def test_default_cooldown_is_sane(self) -> None:
        """默认冷却期应为正且足够 (给 adb 重连留窗口)。"""
        assert DEFAULT_RECOVERY_COOLDOWN >= 30.0

    def test_first_restart_not_blocked_by_cooldown(self) -> None:
        """首次重启 (last_restart_at=0) 不受冷却期限制。"""
        sched = _make_scheduler()
        task = _task()
        for _ in range(5):
            _fail_round(sched, task)
        assert sched._restart_game_safely.call_count == 1  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════════
# 计数归零
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailureCounterReset:
    """成功轮次必须把连续失败计数清零。"""

    def test_reset_on_success_end_to_end(self) -> None:
        """失败 4 次后成功一次 → 计数归零, 不跨正常轮累积。

        直接跑真实的 ``run_daily`` 循环 (用假触发器控制产出), 而非手工复现
        归零逻辑 —— 后者测的是测试自己的假设, 不是产品代码。

        若归零逻辑缺失, 后续失败会因累积而立即触发重启 (误重启正常游戏)。
        """
        sched = _make_scheduler(idle_sleep=0.0)
        sched._ensure_main_page_clean = MagicMock()  # type: ignore[method-assign]

        outcomes: list[list[CombatResult]] = [
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 1 失败
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 2 失败
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 3 失败
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 4 失败
            [CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)],  # 成功 → 归零
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 失败 (计数应从 1 起)
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 失败 2
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 失败 3
            [CombatResult(flag=ConditionFlag.ACTION_FAILED)],   # 失败 4
        ]

        class _FakeTrigger:
            name = 'fake'
            priority = 10

            def should_fire(self, _ctx):
                if not outcomes:
                    sched._ctx.stop_event.set()
                    return None
                flag_result = outcomes.pop(0)

                class _R:
                    def __init__(self, r):
                        self.run = lambda: [r]

                return types.SimpleNamespace(
                    name='t',
                    times=1,
                    priority=10,
                    runner=_R(flag_result[0]),
                    on_done=None,
                    completed=0,
                    results=[],
                )

        sched.register_trigger(_FakeTrigger())
        sched._check_daily_reset = MagicMock()  # type: ignore[method-assign]
        sched._maybe_collect_expedition = MagicMock()  # type: ignore[method-assign]
        sched._print_summary = MagicMock()  # type: ignore[method-assign]

        sched.run_daily()

        # 关键断言: 4 次失败 + 成功归零 + 4 次失败 = 仍未达阈值 5, 不该重启
        assert sched._restart_game_safely.call_count == 0  # type: ignore[attr-defined]
        # 4 次失败各做过轻量恢复
        assert sched._try_recover_to_main.call_count == 8  # type: ignore[attr-defined]

    def test_fresh_scheduler_starts_at_zero(self) -> None:
        """新调度器计数从 0 开始。"""
        assert _make_scheduler()._consecutive_failures == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 异常韧性
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecoveryResilience:
    """恢复动作自身抛异常时, 主循环不得被中断。"""

    def test_light_recovery_exception_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_try_recover_to_main 内部异常被吞掉。"""
        sched = TaskScheduler(_make_ctx())

        def _boom(*_a, **_k):
            raise RuntimeError('设备不可达')

        monkeypatch.setattr(
            'autowsgr.ops.startup.recover_to_main_or_restart',
            _boom,
        )
        # 不应抛出
        sched._try_recover_to_main()

    def test_restart_exception_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_restart_game_safely 内部异常被吞掉。"""
        sched = TaskScheduler(_make_ctx())

        def _boom(*_a, **_k):
            raise RuntimeError('重启失败')

        monkeypatch.setattr('autowsgr.ops.restart_game', _boom)
        # 不应抛出
        sched._restart_game_safely()


# ═══════════════════════════════════════════════════════════════════════════════
# _run_task 返回值契约
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunTaskReturnContract:
    """_run_task 必须返回结果列表 —— 熔断依赖它判定成败。"""

    def test_returns_results_list(self) -> None:
        """正常执行时返回 runner 的结果列表。"""
        sched = TaskScheduler(_make_ctx())
        sched._maybe_collect_expedition = MagicMock()  # type: ignore[method-assign]
        runner = MagicMock()
        runner.run.return_value = CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)
        task = types.SimpleNamespace(
            name='t', times=1, runner=runner, on_done=None,
            completed=0, results=[],
        )

        out = sched._run_task(task)
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0].flag == ConditionFlag.OPERATION_SUCCESS

    def test_returns_action_failed_on_exception(self) -> None:
        """runner 抛异常 → 返回 ACTION_FAILED (供上层计失败)。"""
        sched = TaskScheduler(_make_ctx())
        sched._maybe_collect_expedition = MagicMock()  # type: ignore[method-assign]
        runner = MagicMock()
        runner.run.side_effect = RuntimeError('页面识别失败')
        task = types.SimpleNamespace(
            name='t', times=1, runner=runner, on_done=None,
            completed=0, results=[],
        )

        out = sched._run_task(task)
        assert len(out) == 1
        assert out[0].flag == ConditionFlag.ACTION_FAILED
        # 这正是「异常被吞 → 主循环无法感知失败」的根源, 有了返回值才能判定
        assert TaskScheduler._is_failed_round(out) is True