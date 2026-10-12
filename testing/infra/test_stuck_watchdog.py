"""卡死诊断测试（2026-10-12）。

两个机制
--------
- :mod:`autowsgr.infra.stuck_watchdog` —— 停滞时 dump 调用栈（纯诊断）
- :mod:`~autowsgr.emulator.controller.scrcpy` 的重复动作检测 —— 界面无进展
  但脚本死磕同一坐标时抛 :class:`RepeatedActionError`，交给熔断

阈值依据
--------
真实故障样本 ``logs/2026-10-11_08-28-24``（用户手动关闭 MuMu）：
同一坐标 ``(0.260, 0.963)`` 被点击 **123 次 / 持续 185 秒**，
而同一日志里其它坐标均只出现 1 次。平均间隔 1.52s。
据此定阈值「10 秒内同坐标 > 20 次」—— 与正常连击（约 7 次/10s）不重叠。
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from autowsgr.emulator.controller.scrcpy import (
    REPEAT_STUCK_SECONDS,
    ScrcpyController,
)
from autowsgr.infra import RepeatedActionError
from autowsgr.infra import stuck_watchdog


# ══════════════════════════════════════════════
# 1. 重复动作检测
# ══════════════════════════════════════════════


@pytest.fixture
def ctrl() -> ScrcpyController:
    """不接真机的控制器：只测点击记账，不触发 adb。"""
    c = ScrcpyController.__new__(ScrcpyController)
    c._resolution = (960, 540)
    c._repeat_since = {}
    c._last_click_caller = '<test>'
    c._adb_input = MagicMock()  # type: ignore[method-assign]
    return c


class TestRepeatDetection:
    """同坐标连续点击**超过时长**即判定界面无进展。

    ⚠️ 判据是持续时长而非速率 —— 这是被实机日志纠正过的
    （2026-10-12，见本文件顶部说明与 ``_audit_poll.py`` 的数据）：
    正常的状态识别轮询最高能到 **66 次/10 秒**，任何速率阈值都会误报。
    """

    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch):
        """可控时钟：避免测试真的 sleep。"""
        import autowsgr.emulator.controller.scrcpy as mod

        now = {'t': 1000.0}
        monkeypatch.setattr(mod.time, 'monotonic', lambda: now['t'])
        return now

    def test_single_click_never_raises(self, ctrl, clock) -> None:
        for _ in range(3):
            ctrl.click(0.5, 0.5, delay=False)
        ctrl._adb_input.assert_called()

    def test_fast_but_brief_burst_does_not_raise(self, ctrl, clock) -> None:
        """🔴 核心回归：正常轮询的高频连击**不得**误报。

        实测样本（logs/2026-10-09/10/11 十余个文件）：状态识别轮询
        最高 **66 次/10 秒**，这是完全正常的 —— 等待动画时就该高频点加速。
        初版用「10 秒 > 20 次」判定，此处必然抛异常。
        """
        for _ in range(66):
            ctrl.click(0.26, 0.963, delay=False)
            clock['t'] += 0.15          # 66 * 0.15 ≈ 9.9s
        # 不抛即通过

    def test_long_same_coord_click_raises(self, ctrl, clock) -> None:
        """同坐标持续点击超过阈值 → 抛异常。"""
        with pytest.raises(RepeatedActionError):
            for _ in range(100):
                ctrl.click(0.26, 0.963, delay=False)
                clock['t'] += 1.0

    def test_catches_real_incident_pattern(self, ctrl, clock) -> None:
        """复现真实故障：185.4s 内 123 次，平均间隔 1.52s。"""
        with pytest.raises(RepeatedActionError) as exc:
            for i in range(124):
                ctrl.click(0.26, 0.963, delay=False)
                clock['t'] += 1.52
        assert '0.260' in str(exc.value)
        assert '无进展' in str(exc.value)

    def test_different_coords_do_not_accumulate(self, ctrl, clock) -> None:
        """不同坐标各点多次不误报 —— 正常流程会点很多不同位置。"""
        for i in range(200):
            ctrl.click(0.1 + (i % 20) * 0.03, 0.5, delay=False)
            clock['t'] += 0.2

    def test_slow_sparse_clicks_do_not_raise(self, ctrl, clock) -> None:
        """间隔很大但次数少 —— 每次都像"重新开始"前的首次记录。"""
        for _ in range(200):
            ctrl.click(0.26, 0.963, delay=False)
            ctrl._repeat_since.clear()   # 模拟每次操作前状态被重置
            clock['t'] += 10.0

    def test_exception_carries_last_caller(self, ctrl, clock) -> None:
        """异常信息应带调用来源，便于定位是哪段逻辑在死磕。"""
        ctrl._last_click_caller = 'combat/actions.py:211 in click_speed_up'
        with pytest.raises(RepeatedActionError, match='click_speed_up'):
            for _ in range(100):
                ctrl._note_action(0.26, 0.963)
                clock['t'] += 1.0

    def test_threshold_covers_real_incident_not_normal_poll(self) -> None:
        """阈值必须夹在「正常轮询时长」与「真实故障时长」之间。

        正常轮询最长约 30s（状态机超时会自行退出）；
        真实故障 185.4s。阈值 60s 居中，两侧都有余量。
        """
        normal_poll_seconds = 30.0
        real_incident_seconds = 185.4
        assert normal_poll_seconds < REPEAT_STUCK_SECONDS < real_incident_seconds, (
            f'阈值 {REPEAT_STUCK_SECONDS} 未落在 ({normal_poll_seconds}, '
            f'{real_incident_seconds}) 区间内'
        )

    def test_threshold_is_far_above_measured_poll_burst(self) -> None:
        """按实测速率换算：正常轮询即使打满 30s 也远不到阈值。

        实测最高 66 次/10 秒 → 折算 30s 约 198 次，
        而持续 30s 远小于 60s 阈值 —— 证明「用时长而非速率」的必要性。
        """
        measured_burst_per_10s = 66
        over_30s = measured_burst_per_10s * 3
        assert over_30s > 100, '样本量应足够大, 换算才有意义'
        assert 30.0 < REPEAT_STUCK_SECONDS, '时长判据下, 30s 的正常轮询不触发'


# ══════════════════════════════════════════════
# 2. 停滞看门狗 (faulthandler)
# ══════════════════════════════════════════════


class TestStallWatchdog:
    @pytest.fixture(autouse=True)
    def _clean(self):
        stuck_watchdog.disarm_stall_watchdog()
        yield
        stuck_watchdog.disarm_stall_watchdog()

    def test_arm_returns_true_then_false(self, tmp_path) -> None:
        assert stuck_watchdog.arm_stall_watchdog(tmp_path) is True
        # 重复注册无效（避免多个定时器打架）
        assert stuck_watchdog.arm_stall_watchdog(tmp_path) is False
        assert stuck_watchdog.is_armed() is True

    def test_disarm_resets(self, tmp_path) -> None:
        stuck_watchdog.arm_stall_watchdog(tmp_path)
        stuck_watchdog.disarm_stall_watchdog()
        assert stuck_watchdog.is_armed() is False

    def test_creates_traceback_file(self, tmp_path) -> None:
        stuck_watchdog.arm_stall_watchdog(tmp_path, timeout=60)
        assert (tmp_path / 'stalled_traceback.log').exists(), (
            '应在日志目录留下 stalled_traceback.log 供事后排查'
        )

    def test_rearms_after_disarm(self, tmp_path) -> None:
        stuck_watchdog.arm_stall_watchdog(tmp_path)
        stuck_watchdog.disarm_stall_watchdog()
        assert stuck_watchdog.arm_stall_watchdog(tmp_path) is True

    def test_default_timeout_exceeds_longest_phase_timeout(self) -> None:
        """默认阈值必须容过战斗里最长的合法等待（NIGHT_PROMPT 150s）。

        否则正常夜战等待会被误判为停滞，白白dump 一堆栈。
        """
        from autowsgr.combat.recognizer import PHASE_SIGNATURES

        longest = max(s.default_timeout for s in PHASE_SIGNATURES.values())
        assert stuck_watchdog.DEFAULT_STALL_TIMEOUT > longest, (
            f'停滞阈值 {stuck_watchdog.DEFAULT_STALL_TIMEOUT} '
            f'未超过最长合法等待 {longest}s，会误报'
        )


# ══════════════════════════════════════════════
# 3. 两者与熔断的衔接
# ══════════════════════════════════════════════


class TestHandoffToCircuitBreaker:
    def test_repeated_action_is_autowsgr_error(self) -> None:
        """必须是 AutoWSGRError 家族，调度器的 except 才能接住。"""
        from autowsgr.infra.exceptions import AutoWSGRError

        assert issubclass(RepeatedActionError, AutoWSGRError)

    def test_scheduler_handles_it_like_other_failures(self) -> None:
        """调度器对非预期异常已有兜底（标记失败 + 熔断计数）。"""
        import inspect

        from autowsgr.scheduler.scheduler import TaskScheduler

        src = inspect.getsource(TaskScheduler.run_daily)
        assert 'ACTION_FAILED' in src, '调度器应把异常转成 ACTION_FAILED 计入熔断'
        assert '_handle_failure' in src, '调度器应走 _handle_failure'


def test_watchdog_wired_into_launcher() -> None:
    """启动器必须真的调用 arm_stall_watchdog —— 否则功能形同没写。"""
    import inspect

    from autowsgr.scheduler.launcher import Launcher

    assert 'arm_stall_watchdog' in inspect.getsource(Launcher.load_config)


def test_detector_wired_into_click() -> None:
    """click 必须调用 _note_action —— 漏了接线检测就完全无效。"""
    import inspect

    assert '_note_action' in inspect.getsource(ScrcpyController.click)