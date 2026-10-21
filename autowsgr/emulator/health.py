"""设备可用性诊断与分层恢复。

背景 (实机2026-10-11暴露)
--------------------------
关闭模拟器实例 (MuMu 16416) 后, 调度器进入高速死循环:

- ``adbutils.errors.AdbError: device offline`` 在**推送 scrcpy-server.jar**
  阶段抛出, 一轮战斗都进不去
- 因截图瞬间失败, 连续 5 次「失败轮」在 **0.1 秒内**跑完, 60 秒冷却期
  形同虚设
- 结果: 8 分钟写出 10MB 日志 (132450 行/ 1940 次 device offline),
  照此速度一晚可写满磁盘

更关键的是, **「游戏崩溃」与「设备离线」是两个不同层级的问题**,
但上一版调度器用同一套熔断处理:

============  ==========================  ==========================
场景          adb 设备                能重启游戏吗
============  ==========================  ==========================
游戏崩溃      online                    能 ✅
模拟器关闭    offline / 设备消失         不能 ❌ (adb 指令必失败)
============  ==========================  ==========================

本模块提供**无副作用**的 L2 级恢复 (仅重建 adb/视频流会话, 不重启任何
用户进程)。是否拉起模拟器进程属于**有副作用**操作, 不在此模块, 由调用方
显式决定 (见 :class:`RecoveryPolicy`)。
"""

from __future__ import annotations

import enum
import subprocess
import time

from autowsgr.infra.exceptions import EmulatorConnectionError
from autowsgr.infra.logger import get_logger


_log = get_logger('emulator.health')


class DeviceHealth(enum.Enum):
    """设备健康状态。"""

    OK = 'ok'
    """设备在线, 截图正常。"""

    STREAM_DEAD = 'stream_dead'
    """adb 在线但 scrcpy 视频流中断 —— 可无副作用重连。"""

    DEVICE_OFFLINE = 'device_offline'
    """adb 设备离线/消失 —— 需重建 adb 会话; 模拟器进程可能仍在。"""

    DEVICE_GONE = 'device_gone'
    """确认无此 adb 设备 —— 模拟器进程可能已退出, 需重启模拟器才能恢复。"""


#: adbutils 在设备不可用时抛出的异常类型名 (不同版本类名不一致, 按名匹配)
_ADB_OFFLINE_MARKERS = (
    'device offline',
    'device not found',
    'no devices/emulators found',
    'device still connecting',
    'protocol fault',
    'connection reset',
)


def is_device_error(exc: BaseException) -> bool:
    """判断异常是否为「设备不可用」类 (而非游戏逻辑失败)。

    adbutils 各版本抛出的异常类型不稳定 (``AdbError`` / ``AdbTimeoutError`` /
    ``EnvironmentError`` 混用), 因此**按异常链上的消息文本匹配**, 比按类型
    匹配更稳。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        if any(marker in msg for marker in _ADB_OFFLINE_MARKERS):
            return True
        # adbutils 常把底层原因包在 .args / .cause / .ret 之类, 统一看字符串
        for attr in ('args', 'cause', 'reason', 'message'):
            val = getattr(cur, attr, None)
            if isinstance(val, str) and any(
                marker in val.lower() for marker in _ADB_OFFLINE_MARKERS
            ):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def _adb_path(controller) -> str | None:
    """取控制器已解析的 adb 路径 (复用其查找逻辑)。"""
    resolver = getattr(controller, '_resolve_adb_path', None)
    if callable(resolver):
        try:
            return resolver()
        except Exception:  # noqa: BLE001 - 诊断路径不应因查找失败而抛
            return None
    import shutil

    return shutil.which('adb')


def probe_device(controller) -> DeviceHealth:
    """探测设备当前健康状态 (只读, 不做任何恢复)。

    依次检查: 截图是否可用 → adb 设备列表里是否还有这台设备。
    截图成功即视为 OK (最快路径, 避免每次故障都跑 adb 命令)。
    """
    try:
        frame = controller.screenshot()
    except Exception as exc:  # noqa: BLE001 - 任何失败都归为待诊断
        if not is_device_error(exc):
            # 非设备问题 (如解码失败): 仍视为流失效, 可尝试重连
            _log.debug('[DeviceHealth] 截图失败 (非设备类): {}', exc)
            return DeviceHealth.STREAM_DEAD

        # 截图失败且是设备类 → 再用 adb devices 确认设备是否还在
        return _probe_via_adb(controller)

    if frame is None or getattr(frame, 'size', 0) == 0:
        return DeviceHealth.STREAM_DEAD
    return DeviceHealth.OK


def _probe_via_adb(controller) -> DeviceHealth:
    """用 ``adb devices`` 判断设备是否还存在。"""
    adb = _adb_path(controller)
    serial: str | None = getattr(controller, '_serial', None)

    if not adb:
        # 没有 adb 可执行文件, 无法进一步判断, 保守当作设备消失
        return DeviceHealth.DEVICE_GONE

    try:
        proc = subprocess.run(  # noqa: S603
            [adb, 'devices'],
            capture_output=True,
            timeout=10,
            check=False,
        )
        out = proc.stdout.decode('utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001
        _log.warning('[DeviceHealth] 执行 adb devices 失败: {}', exc)
        return DeviceHealth.DEVICE_GONE

    # 输出形如:
    #   List of devices attached
    #   127.0.0.1:16416   device
    #   127.0.0.1:5557    offline
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        dev_serial, state = parts[0], parts[1]
        if serial and dev_serial != serial:
            continue
        if state == 'device':
            # 设备在, 只是流/会话问题 → 可无副作用重连
            return DeviceHealth.STREAM_DEAD
        return DeviceHealth.DEVICE_OFFLINE  # offline / unauthorized

    # adb devices 里找不到这台设备
    return DeviceHealth.DEVICE_GONE


def reconnect_device(controller, *, attempts: int = 3, delay: float = 3.0) -> bool:
    """L2 级无副作用恢复: 重建 adb 会话与视频流。

    **不重启任何用户进程** —— 只重连 adb、重建 scrcpy 会话。因此可以
    安全自动执行 (不像拉起模拟器那样有副作用)。

    Parameters
    ----------
    controller:
        :class:`~autowsgr.emulator.controller.ScrcpyController` 实例。
    attempts:
        尝试次数。
    delay:
        每次尝试前的等待秒数 (给模拟器恢复时间)。

    Returns
    -------
    bool
        ``True`` = 设备已恢复可用。
    """
    serial = getattr(controller, '_serial', None)
    _log.info('[DeviceHealth] 尝试重连设备 {} (最多 {} 次)', serial, attempts)

    for attempt in range(1, attempts + 1):
        time.sleep(delay)

        # 视频流侧
        try:
            if hasattr(controller, '_reopen_stream'):
                controller._reopen_stream()  # noqa: SLF001 - 同一包内的受控调用
            else:
                controller.connect()
            _log.info('[DeviceHealth] 设备已恢复 (第 {} 次重连)', attempt)
            return True
        except Exception as exc:  # noqa: BLE001, PERF203 - 逐次尝试不同恢复路径
            last_exc = exc
            _log.warning(
                '[DeviceHealth] 视频流重连失败 (第 {}/{}): {}',
                attempt,
                attempts,
                exc,
            )

        # adb 会话侧: 设备若已 offline, 需重新 adb connect
        try:
            controller._connect_adb_device(serial)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            _log.debug('[DeviceHealth] adb 重连失败 (第 {}/{}): {}', attempt, attempts, exc)

    _log.error('[DeviceHealth] 设备重连 {} 次均失败', attempts)
    return False


def wait_for_device(controller, timeout: float = 120.0, interval: float = 5.0) -> bool:
    """等待设备变为可用 (用于「用户可能正在手动启动模拟器」的场景)。

    这是一个**被动等待**: 只观察, 不主动拉起模拟器。若用户在别处手动
    启动了模拟器, 这里能等到它; 若没人操作, 超时返回 ``False`` 由调用方
    决定后续 (中止或拉起)。

    典型用途: 检测到设备消失后先等一会儿, 避免「用户正在手动重启模拟器,
    脚本却抢先拉起」这种冲突。
    """
    deadline = time.monotonic() + timeout
    _log.info('[DeviceHealth] 等待设备就绪 (最多 {:.0f}s)...', timeout)

    while time.monotonic() < deadline:
        health = probe_device(controller)
        if health is DeviceHealth.OK:
            _log.info('[DeviceHealth] 设备已就绪')
            return True
        if health in (DeviceHealth.STREAM_DEAD, DeviceHealth.DEVICE_OFFLINE):
            if reconnect_device(controller, attempts=1, delay=0.0):
                if probe_device(controller) is DeviceHealth.OK:
                    _log.info('[DeviceHealth] 设备已就绪')
                    return True
        time.sleep(interval)

    _log.error('[DeviceHealth] 等待设备就绪超时 ({:.0f}s)', timeout)
    return False


def is_emulator_process_alive(serial: str | None) -> bool | None:
    """判断 serial 对应的模拟器进程是否还活着。

    委托给 :mod:`autowsgr.emulator.mumu_manager` 的 MuMu CLI 精确查询。
    若查询不出 (非 MuMu / CLI 不可用), 返回 ``None`` = **无法判断**,
    调用方不应据此贸然拉起模拟器 (有副作用的操作)。

    返回 ``True`` 时**绝不能**去启动模拟器 —— 会重复启动已有实例。
    """
    from autowsgr.emulator.mumu_manager import is_instance_alive

    return is_instance_alive(serial)


def raise_device_unavailable(ctx, reason: str) -> None:
    """设备经恢复后仍不可用 → 抛不可恢复异常, 由顶层决定退出。

    留证据: 打印 serial / 设备健康状态 / 恢复尝试次数, 便于事后判断是
    模拟器被关、手动重启中, 还是环境层面的问题。
    """
    controller = getattr(ctx, 'ctrl', None)
    health = probe_device(controller) if controller is not None else None
    serial = getattr(controller, '_serial', None)

    _log.error(
        '[DeviceHealth] 设备不可用且无法自愈。serial={} health={} 原因={}',
        serial,
        health.value if health else 'unknown',
        reason,
    )
    raise EmulatorConnectionError(
        f'设备 {serial} 不可用且无法自动恢复: {reason}。'
        f'请确认模拟器已启动 (或改用 --auto-launch-emulator 允许脚本拉起)。'
    )