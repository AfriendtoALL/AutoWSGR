"""设备故障分层恢复策略。

回答「设备没了该抛异常中止, 还是想办法重启模拟器?」—— **两者都不直接用**。

为什么不用「抛异常中止」
    挂机脚本半夜静默死掉, 早上才发现白跑一夜, 且现场日志已被覆盖。
    对7x24 场景, 无声退出比死循环更糟 (死循环至少有日志可查)。

为什么不用「直接重启模拟器」
    拉起模拟器是**有副作用**的操作 (占内存/显存、可能抢焦点窗口、可能与
    用户手动操作冲突)。脚本擅自拉起实例是越权行为 —— 模拟器可能是用户
    主动关掉的 (不想让它跑) 或正在被调试。

本模块的答案: **按「副作用从小到大」分级, 每级都有明确判据与退出条件**。

============  ==========================  ====================  ==========
层级          判据                动作            副作用
============  ==========================  ====================  ==========
L1 页面恢复    设备在线, 只是页面异常回主页面          无
------------  -------------------------  -------------------- ----------
L2 设备重连    adb 离线/流死, 模拟器**可能还在**  adb connect + 重建   无
                会话                              流
------------  -------------------------  -------------------- ----------
L2.5 等待      设备消失                被动等一会儿          无
------------  -------------------------  -------------------- ----------
L3 拉起模拟器  确认进程已死              MuMu CLI 启动     **有**
------------  -------------------------  -------------------- ----------
L4 中止        超过总时限/ 不允许 L3  抛异常 + 留证据      结束运行
============  ==========================  ====================  ==========

L2 是关键: 很多「设备离线」只是 adb 会话断了, 模拟器进程还活着, 此时
``adb connect`` 就能救回, 且**不碰任何用户进程**, 可以安全自动执行。
L3 默认**关闭**, 需显式 ``--auto-launch-emulator``。

实测依据 (2026-10-11日志)
-------------------------
关闭模拟器实例后, 5 次「失败轮」在 **0.1 秒内**跑完 (截图瞬间抛
``adbutils.errors.AdbError: device offline``), 60 秒冷却期完全失效,
8 分钟写出 10MB 日志。本模块的退避与总时限正是针对这一现象。
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from autowsgr.emulator.health import (
    DeviceHealth,
    is_emulator_process_alive,
    probe_device,
    reconnect_device,
    wait_for_device,
)
from autowsgr.infra.logger import get_logger


_log = get_logger('scheduler.recovery')


class RecoveryOutcome(enum.Enum):
    """单次恢复尝试的结果。"""

    RECOVERED = 'recovered'
    """已恢复, 可继续。"""

    NEEDS_GAME_RESTART = 'needs_game_restart'
    """设备正常但页面/游戏异常 —— 应走原有的「重启游戏」。"""

    DEVICE_UNAVAILABLE = 'device_unavailable'
    """设备不可用且无法自愈 —— 需升级处置或中止。"""


@dataclass
class RecoveryPolicy:
    """设备故障恢复策略配置。

    Parameters
    ----------
    auto_launch_emulator:
        是否允许脚本**拉起模拟器实例**。默认 ``False`` —— 因为这是有副作用
        的操作, 可能是用户主动关掉模拟器 (不想让它跑)。必须由用户显式开启。
    max_reconnect_attempts:
        L2 设备重连尝试次数。
    wait_before_launch:
        L3 之前先被动等待多少秒。给「用户可能正在手动启动模拟器」留窗口,
        避免脚本抢先拉起造成冲突。
    device_wait_timeout:
        被动等待设备的总时限 (秒)。
    escalate_after:
        同一设备故障累计几次后升级到 L3 (仅 ``auto_launch_emulator=True`` 时生效)。
    backoff_base:
        失败退避基数 (秒), 按连续失败次数指数增长。
    backoff_max:
        单次退避上限 (秒)。
    """

    auto_launch_emulator: bool = False
    max_reconnect_attempts: int = 3
    wait_before_launch: float = 30.0
    device_wait_timeout: float = 120.0
    escalate_after: int = 2
    backoff_base: float = 5.0
    backoff_max: float = 60.0

    _device_fault_count: int = field(default=0, init=False, repr=False)
    _last_fault_at: float = field(default=0.0, init=False, repr=False)

    def note_device_fault(self) -> int:
        """记录一次设备故障, 返回累计次数。"""
        now = time.monotonic()
        # 距上次故障很久 → 认为是新问题, 计数归零
        if now - self._last_fault_at > self.device_wait_timeout * 2:
            self._device_fault_count = 0
        self._device_fault_count += 1
        self._last_fault_at = now
        return self._device_fault_count

    def reset_device_faults(self) -> None:
        """设备恢复后清零计数。"""
        self._device_fault_count = 0

    def backoff_seconds(self, consecutive_failures: int) -> float:
        """按连续失败次数计算退避时长 (指数增长, 有上限)。

        修复前的致命问题: 截图瞬间失败时, 5 次「失败轮」在 0.1 秒内跑完,
        60 秒冷却期形同虚设 → 高速死循环 (8 分钟 10MB 日志)。
        这个退避保证失败轮之间有真实等待。
        """
        if consecutive_failures <= 0:
            return 0.0
        return min(
            self.backoff_base * (2 ** min(consecutive_failures - 1, 10)),
            self.backoff_max,
        )


def recover_device(ctx, policy: RecoveryPolicy) -> RecoveryOutcome:
    """按 L2 → L2.5 → L3 顺序尝试恢复设备。

    这是**无副作用部分**: 只重建 adb 会话/视频流, 以及**被动等待**。
    不会启动或关闭任何用户进程。
    """
    controller = ctx.ctrl
    health = probe_device(controller)

    if health is DeviceHealth.OK:
        policy.reset_device_faults()
        return RecoveryOutcome.NEEDS_GAME_RESTART

    faults = policy.note_device_fault()
    serial = getattr(controller, '_serial', None)
    _log.error(
        '[Recovery] 设备异常: serial={} health={} (第 {} 次设备故障)',
        serial,
        health.value,
        faults,
    )

    # ── L2: 无副作用重连 ──
    if health in (DeviceHealth.STREAM_DEAD, DeviceHealth.DEVICE_OFFLINE):
        _log.info('[Recovery] L2: 尝试重连 adb/视频流 (无副作用)')
        if reconnect_device(controller, attempts=policy.max_reconnect_attempts):
            if probe_device(controller) is DeviceHealth.OK:
                policy.reset_device_faults()
                _log.info('[Recovery] L2 成功, 设备已恢复')
                return RecoveryOutcome.RECOVERED
        _log.warning('[Recovery] L2 重连失败')

    # ── L2.5: 被动等待 ──
    # 设备已消失时, 模拟器进程可能还在重启中, 或用户正在手动启动。
    # 先等一会儿, **不主动拉起** —— 避免与用户操作冲突。
    if policy.wait_before_launch > 0:
        _log.info(
            '[Recovery] L2.5: 被动等待设备 {:.0f}s (不拉起模拟器)',
            policy.wait_before_launch,
        )
        if wait_for_device(controller, timeout=policy.wait_before_launch):
            policy.reset_device_faults()
            return RecoveryOutcome.RECOVERED

    return RecoveryOutcome.DEVICE_UNAVAILABLE


def maybe_launch_emulator(ctx, policy: RecoveryPolicy) -> RecoveryOutcome:
    """L3: 拉起模拟器实例。**有副作用**, 仅在用户显式允许时执行。

    执行前必须确认进程确实已死 —— 若进程还活着却去启动, 会重复启动。
    """
    controller = ctx.ctrl
    serial = getattr(controller, '_serial', None)

    if not policy.auto_launch_emulator:
        _log.error(
            '[Recovery] 设备不可用且未开启自动拉起 (--auto-launch-emulator), 不做进一步操作',
        )
        return RecoveryOutcome.DEVICE_UNAVAILABLE

    alive = is_emulator_process_alive(serial)
    if alive is None:
        _log.error(
            '[Recovery] 无法判断模拟器进程状态 (serial={}), 为安全起见不拉起',
            serial,
        )
        return RecoveryOutcome.DEVICE_UNAVAILABLE

    if alive:
        # 进程还活着 → 说明是 adb/会话问题, 拉起毫无意义 (甚至重复启动)
        _log.error(
            '[Recovery] 模拟器进程仍存活但设备不可用, 属会话层问题, 不执行拉起',
        )
        return RecoveryOutcome.DEVICE_UNAVAILABLE

    from autowsgr.emulator.mumu_manager import index_for_serial, launch_instance

    index = index_for_serial(serial)
    if index is None:
        _log.error('[Recovery] 无法由 serial={} 反查实例索引, 不拉起', serial)
        return RecoveryOutcome.DEVICE_UNAVAILABLE

    _log.warning('[Recovery] L3: 确认进程已退出, 拉起实例 {}', index)
    if not launch_instance(index):
        return RecoveryOutcome.DEVICE_UNAVAILABLE

    # 拉起后等待就绪 (启动需要时间)
    if wait_for_device(controller, timeout=policy.device_wait_timeout):
        _log.info('[Recovery] L3 成功, 模拟器已拉起并就绪')
        policy.reset_device_faults()
        # 模拟器起来了, 但游戏可能没启动 → 交给上层重启游戏
        return RecoveryOutcome.NEEDS_GAME_RESTART

    return RecoveryOutcome.DEVICE_UNAVAILABLE