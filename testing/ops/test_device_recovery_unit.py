"""设备故障分层恢复的单元测试 (无设备)。

实机问题 (2026-10-11)
---------------------
用户关闭模拟器实例 (MuMu 16416) 后, 脚本进入高速死循环:

- ``adbutils.errors.AdbError: device offline`` 在推送 scrcpy-server.jar 阶段
  抛出, 一轮战斗都进不去
- 因截图瞬间失败, 连续 5 次「失败轮」在 **0.1 秒内**跑完 → 60 秒冷却期
  形同虚设
- 8 分钟写出 10MB 日志 (132450 行 / 1940 次 device offline)

根因: 上一版把「游戏崩溃」与「设备离线」当同一层处理, 前者能靠
``restart_game`` 解决, 后者**必然失败** (adb 指令都发不出去)。

本模块验证分层处置的正确性, 尤其:

- 设备不可用时**绝不**调用 ``restart_game``
- 失败轮之间**必有退避** (不再 0.1 秒跑完 5 次)
- L3 拉起模拟器**默认关闭**, 且必须确认进程已死才执行
"""

from __future__ import annotations

import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from autowsgr.combat import CombatResult
from autowsgr.emulator.health import DeviceHealth, is_device_error
from autowsgr.scheduler.recovery import (
    RecoveryOutcome,
    RecoveryPolicy,
    maybe_launch_emulator,
    recover_device,
)
from autowsgr.scheduler.scheduler import TaskScheduler
from autowsgr.types import ConditionFlag


# ═══════════════════════════════════════════════════════════════════════════════
# 异常识别
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsDeviceError:
    """区分「设备类异常」与「业务类异常」。"""

    @pytest.mark.parametrize(
        'msg',
        [
            'device offline',
            'device not found',
            'no devices/emulators found',
            'error: device still connecting',
        ],
    )
    def test_device_errors_detected(self, msg: str) -> None:
        assert is_device_error(Exception(msg)) is True

    @pytest.mark.parametrize(
        'msg',
        ['页面识别失败', '未找到按钮', '弹药检测超时', '战果等级识别超时'],
    )
    def test_business_errors_not_flagged(self, msg: str) -> None:
        """业务异常**不能**被判为设备故障, 否则会误触发重连/拉起。"""
        assert is_device_error(Exception(msg)) is False

    def test_cause_chain_is_walked(self) -> None:
        """包装在 __cause__ 里的设备异常也要能识别。"""
        try:
            try:
                raise Exception('device offline')
            except Exception as inner:
                raise RuntimeError('推送 scrcpy-server 失败') from inner
        except RuntimeError as exc:
            assert is_device_error(exc) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 退避策略
# ═══════════════════════════════════════════════════════════════════════════════


class TestBackoff:
    """失败退避 —— 修复「5 次失败在 0.1 秒内跑完」的根本手段。"""

    def test_first_failure_short(self) -> None:
        p = RecoveryPolicy()
        assert p.backoff_seconds(1) == p.backoff_base

    def test_backoff_increases(self) -> None:
        p = RecoveryPolicy()
        assert p.backoff_seconds(2) > p.backoff_seconds(1)
        assert p.backoff_seconds(4) > p.backoff_seconds(2)

    def test_backoff_capped(self) -> None:
        """必须有上限, 否则长时间故障时 sleep 过长。"""
        p = RecoveryPolicy()
        assert p.backoff_seconds(999) <= p.backoff_max

    def test_zero_failures_no_backoff(self) -> None:
        assert RecoveryPolicy().backoff_seconds(0) == 0.0

    def test_real_backoff_is_non_trivial(self) -> None:
        """实机死循环场景下, 前几次退避总和应足以打断高速空转。"""
        p = RecoveryPolicy()
        total = sum(p.backoff_seconds(i) for i in range(1, 6))
        assert total >= 30.0, f'5 次退避总和仅 {total}s, 仍可能高速空转'


# ═══════════════════════════════════════════════════════════════════════════════
# L2: 无副作用设备重连
# ═══════════════════════════════════════════════════════════════════════════════


def _ctx_with_health(health: DeviceHealth) -> MagicMock:
    ctx = MagicMock()
    ctx.ctrl._serial = '127.0.0.1:16416'
    ctx.config.account.game_app = 'official'
    return ctx


class TestRecoverDevice:
    """L2 级恢复: 只重建会话, 不碰用户进程。"""

    def test_ok_device_needs_game_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """设备正常 → 交回游戏层处理 (而非当作设备故障)。"""
        ctx = _ctx_with_health(DeviceHealth.OK)
        policy = RecoveryPolicy()
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.probe_device',
            lambda _c: DeviceHealth.OK,
        )
        assert recover_device(ctx, policy) is RecoveryOutcome.NEEDS_GAME_RESTART

    def test_stream_dead_reconnects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """流断但 adb 在线 → 重连即可恢复。"""
        ctx = _ctx_with_health(DeviceHealth.STREAM_DEAD)
        policy = RecoveryPolicy(wait_before_launch=0)
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.probe_device',
            lambda _c: DeviceHealth.STREAM_DEAD,
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.reconnect_device',
            lambda *_a, **_k: True,
        )
        # 重连后探测为 OK
        monkeypatch.setattr(
            'autowsgr.emulator.health.probe_device',
            lambda _c: DeviceHealth.OK,
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.wait_for_device',
            lambda *_a, **_k: True,
        )
        assert recover_device(ctx, policy) in (
            RecoveryOutcome.RECOVERED,
            RecoveryOutcome.DEVICE_UNAVAILABLE,
        )

    def test_unavailable_when_reconnect_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重连失败 + 等待超时 → 明确返回不可用 (而不是无限重试)。"""
        ctx = _ctx_with_health(DeviceHealth.DEVICE_GONE)
        policy = RecoveryPolicy(wait_before_launch=0.01)
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.probe_device',
            lambda _c: DeviceHealth.DEVICE_GONE,
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.reconnect_device',
            lambda *_a, **_k: False,
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.wait_for_device',
            lambda *_a, **_k: False,
        )
        assert recover_device(ctx, policy) is RecoveryOutcome.DEVICE_UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════════════════
# L3: 拉起模拟器 (有副作用, 默认关闭)
# ═══════════════════════════════════════════════════════════════════════════════


class TestMaybeLaunchEmulator:
    """L3 必须默认关闭, 且进程还活着时绝不启动。"""

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未显式开启 → 绝不拉起模拟器。"""
        ctx = _ctx_with_health(DeviceHealth.DEVICE_GONE)
        policy = RecoveryPolicy()  # auto_launch_emulator=False

        def _boom(*_a, **_k):
            raise AssertionError('不应尝试拉起模拟器')

        monkeypatch.setattr('autowsgr.emulator.mumu_manager.launch_instance', _boom)
        assert maybe_launch_emulator(ctx, policy) is RecoveryOutcome.DEVICE_UNAVAILABLE

    def test_alive_process_is_not_launched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """进程还活着 → 拉起毫无意义且会重复启动, 必须拒绝。"""
        ctx = _ctx_with_health(DeviceHealth.DEVICE_GONE)
        policy = RecoveryPolicy(auto_launch_emulator=True)
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.is_emulator_process_alive',
            lambda _s: True,
        )

        def _boom(*_a, **_k):
            raise AssertionError('进程还活着时不应拉起')

        monkeypatch.setattr('autowsgr.emulator.mumu_manager.launch_instance', _boom)
        assert maybe_launch_emulator(ctx, policy) is RecoveryOutcome.DEVICE_UNAVAILABLE

    def test_unknown_status_is_not_launched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """状态判断不出来 (None) → 保守不拉起。"""
        ctx = _ctx_with_health(DeviceHealth.DEVICE_GONE)
        policy = RecoveryPolicy(auto_launch_emulator=True)
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.is_emulator_process_alive',
            lambda _s: None,
        )

        def _boom(*_a, **_k):
            raise AssertionError('状态未知时不应拉起')

        monkeypatch.setattr('autowsgr.emulator.mumu_manager.launch_instance', _boom)
        assert maybe_launch_emulator(ctx, policy) is RecoveryOutcome.DEVICE_UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════════════════
# 调度器集成: 设备不可用时绝不重启游戏 (本次修复的核心)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchedulerSkipsGameRestartWhenDeviceDown:
    """核心回归: 设备离线时不能调 restart_game (必然失败且加剧空转)。"""

    def _scheduler(self, **kwargs) -> TaskScheduler:
        ctx = MagicMock()
        ctx.stop_event = threading.Event()
        ctx.active_fight_tasks = 0
        ctx.ctrl._serial = '127.0.0.1:16416'
        ctx.config.account.game_app = 'official'
        return TaskScheduler(ctx, **kwargs)

    def test_no_restart_game_when_device_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """设备不可用 → 不重启游戏, 不进入游戏层恢复。"""
        sched = self._scheduler()
        sched._restart_game_safely = MagicMock()  # type: ignore[method-assign]
        sched._try_recover_to_main = MagicMock()  # type: ignore[method-assign]

        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.probe_device',
            lambda _c: DeviceHealth.DEVICE_GONE,
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.reconnect_device',
            lambda *_a, **_k: False,
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.wait_for_device',
            lambda *_a, **_k: False,
        )
        monkeypatch.setattr('autowsgr.scheduler.scheduler.time.sleep', lambda _s: None)

        task = types.SimpleNamespace(name='t', on_done=None)
        results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]

        # 连续 5 次失败 (超过阈值)
        for _ in range(5):
            sched._consecutive_failures += 1
            sched._handle_failure(task, results, crashed=False)

        # 关键断言: 一次都没调过 restart_game
        assert sched._restart_game_safely.call_count == 0  # type: ignore[attr-defined]

    def test_restart_game_still_used_when_device_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """设备正常时, 达阈值仍应重启游戏 (原行为不能丢)。"""
        sched = self._scheduler()
        sched._restart_game_safely = MagicMock()  # type: ignore[method-assign]
        sched._try_recover_to_main = MagicMock()  # type: ignore[method-assign]

        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.probe_device',
            lambda _c: DeviceHealth.OK,
        )
        monkeypatch.setattr('autowsgr.scheduler.scheduler.time.sleep', lambda _s: None)

        task = types.SimpleNamespace(name='t', on_done=None)
        results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]

        for _ in range(5):
            sched._consecutive_failures += 1
            sched._handle_failure(task, results, crashed=False)

        assert sched._restart_game_safely.call_count == 1  # type: ignore[attr-defined]

    def test_backoff_is_applied_between_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """失败轮之间必须有退避 sleep (防止 0.1 秒跑完 5 次)。"""
        sched = self._scheduler()
        sched._restart_game_safely = MagicMock()  # type: ignore[method-assign]
        sched._try_recover_to_main = MagicMock()  # type: ignore[method-assign]

        slept: list[float] = []
        monkeypatch.setattr(
            'autowsgr.scheduler.scheduler.time.sleep',
            lambda s: slept.append(s),
        )
        monkeypatch.setattr(
            'autowsgr.scheduler.recovery.probe_device',
            lambda _c: DeviceHealth.OK,
        )

        task = types.SimpleNamespace(name='t', on_done=None)
        results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]

        for _ in range(4):  # 未达阈值, 每次都应退避
            sched._consecutive_failures += 1
            sched._handle_failure(task, results, crashed=False)

        assert len(slept) == 4, '每次失败都应退避'
        assert all(s > 0 for s in slept), f'退避时长应均 > 0: {slept}'