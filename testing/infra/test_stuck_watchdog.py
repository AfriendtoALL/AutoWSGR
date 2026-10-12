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
    REPEAT_LIMIT,
    REPEAT_WINDOW_SECONDS,
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
    c._recent_clicks = {}
    c._last_click_caller = '<test>'
    c._adb_input = MagicMock()  # type: ignore[method-assign]
    return c


class TestRepeatDetection:
    def test_single_click_never_raises(self, ctrl: ScrcpyController) -> None:
        for _ in range(3):
            ctrl.click(0.5, 0.5, delay=False)
        ctrl._adb_input.assert_called()  # 点击本身照常执行

    def test_repeated_same_coord_raises(self, ctrl: ScrcpyController) -> None:
        """同坐标在窗口内超阈值 → 抛异常。"""
        for _ in range(REPEAT_LIMIT):
            ctrl.click(0.26, 0.963, delay=False)

        with pytest.raises(RepeatedActionError) as exc:
            ctrl.click(0.26, 0.963, delay=False)

        msg = str(exc.value)
        assert '0.260' in msg and '0.963' in msg, f'异常信息应含坐标: {msg}'
        assert str(REPEAT_LIMIT) in msg, f'异常信息应含阈值: {msg}'

    def test_different_coords_do_not_accumulate(self, ctrl: ScrcpyController) -> None:
        """不同坐标各点多次不应误报 —— 正常流程会点很多不同位置。"""
        for i in range(REPEAT_LIMIT * 2):
            ctrl.click(0.1 + (i % 20) * 0.03, 0.5, delay=False)

    def test_slow_repeat_does_not_raise(self, ctrl: ScrcpyController) -> None:
        """间隔足够大的重复不触发 —— 这正是正常等待轮询的形态。

        实测加速点击平均间隔 1.52s，10 秒内最多约 7 次，远低于阈值 20。
        这里用 0.01s 间隔模拟「慢速」，验证时间维度真的参与判定。
        """
        base = ctrl._recent_clicks
        for _ in range(REPEAT_LIMIT * 2):
            # 每次把窗口往前推 REPEAT_WINDOW_SECONDS，模拟「很久才点一次」
            key = (0.26, 0.963)
            base[key] = base.get(key, __import__('collections').deque(maxlen=REPEAT_LIMIT + 1))
            base[key].append(time.monotonic() - REPEAT_WINDOW_SECONDS - 1)
        # 最后一次是「当前时刻」，窗口内只剩它自己
        ctrl.click(0.26, 0.963, delay=False)

    def test_exception_carries_last_caller(self, ctrl: ScrcpyController) -> None:
        """异常信息应带上「上次是谁点的」，便于定位是哪段逻辑在死磕。"""
        # 直接驱动 _note_action, 绕开 click() 对 _last_click_caller 的覆写,
        # 以便精确断言异常里带的是调用来源
        ctrl._last_click_caller = 'combat/actions.py:211 in click_speed_up'
        with pytest.raises(RepeatedActionError, match='click_speed_up'):
            for _ in range(REPEAT_LIMIT + 2):
                ctrl._note_action(0.26, 0.963)

    def test_real_incident_rate_is_below_threshold(self) -> None:
        """⚠️ 真实故障的点击速率其实**低于**阈值 —— 记下这个事实。

        实测: 123 次 / 185.4s = **0.66次/秒**，而阈值是 20次/10s =
        2.0次/秒。说明真实故障的特征不是「极高频」，而是**长时间持续重复**
        （185 秒死磕同一处）。

        这意味着当前的窗口式判定**抓不到**该次真实故障 —— 它只在
        「10 秒内猛点 20 下」时才触发。这是个已知局限：本模块拦的是
        高频抖动型无进展，而不是慢速死磕型。
        断言如实记录，避免后人误以为阈值已覆盖实测样本。
        """
        real_incident_rate = 123 / 185.4
        threshold_rate = REPEAT_LIMIT / REPEAT_WINDOW_SECONDS
        assert real_incident_rate < threshold_rate, (
            f'实测速率 {real_incident_rate:.2f}/s 若已超阈值 '
            f'{threshold_rate:.2f}/s，请更新本注释的结论'
        )

    def test_slide_window_forgets_old_clicks(self, ctrl: ScrcpyController) -> None:
        """窗口外的旧点击不应继续计数（否则长期运行会无限累积）。"""
        from collections import deque

        key = (0.26, 0.963)
        # 塞满窗口外的旧记录
        ctrl._recent_clicks[key] = deque(
            (time.monotonic() - REPEAT_WINDOW_SECONDS - 5,) * (REPEAT_LIMIT + 1),
            maxlen=REPEAT_LIMIT + 1,
        )
        ctrl.click(0.26, 0.963, delay=False)  # 不应抛


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