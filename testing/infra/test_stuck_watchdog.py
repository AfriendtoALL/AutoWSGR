"""卡死诊断测试（2026-10-13 更新）。

本文件现在只覆盖 :mod:`autowsgr.infra.stuck_watchdog` —— faulthandler 停滞
dump，**纯诊断、不改变控制流**。

已移除：控制器的「重复动作检测」（``RepeatedActionError``）
--------------------------------------------------------
该检测 2026-10-12 加入、2026-10-13 移除，因为**实现有根本缺陷**：

它记的是「从第一次见到该坐标起过了多久」，而不是「**连续**点击了多久」。
于是同一条命令在整场战斗中反复使用时，只要跨度超过阈值就误报 ——
实测日志（``logs/2026-10-13_00-29-58``）：``click_speed_up`` 坐标
``(0.260, 0.963)`` 首次点击 08:30:12，报错于 08:33:20（跨度正好 188s），
而这 188 秒里脚本**正在正常推进**（报错前 0.05 秒刚记录 `PROCEED | 节点=C`）。

用户反馈「模拟器在正常运行，脚本还是报错连续点击 180S」即源于此。
结论：该判据不适合在控制器层做，已连同测试一并移除。

"""
from __future__ import annotations

import threading
import time
import pytest

from autowsgr.infra import stuck_watchdog


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


def test_watchdog_wired_into_launcher() -> None:
    """启动器必须真的调用 arm_stall_watchdog —— 否则功能形同没写。"""
    import inspect

    from autowsgr.scheduler.launcher import Launcher

    assert 'arm_stall_watchdog' in inspect.getsource(Launcher.load_config)


