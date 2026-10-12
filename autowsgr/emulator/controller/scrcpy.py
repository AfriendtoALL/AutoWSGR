"""scrcpy 设备控制器 — 基于 adbutils + scrcpy-server 的实现。

通过 scrcpy 协议获取 H264 视频流并解码为截图；按键与文本输入走 scrcpy
控制流（INJECT_KEYCODE / INJECT_TEXT / SET_CLIPBOARD）以降低延迟。

**触控（click/swipe/long_tap）例外**：MuMu 12 等模拟器内置的 scrcpy 服务不支持
INJECT_TOUCH_EVENT，触控统一走 adb ``input`` 命令，并通过
:class:`~autowsgr.emulator.controller.touch.TouchChannelChain` 做三级降级
（MaaTouch → 常驻 shell → 单次 adb）。截图与应用启停仍走各自原有通道。

使用方式::

    from autowsgr.emulator.controller import ScrcpyController

    ctrl = ScrcpyController(serial="emulator-5554")
    info = ctrl.connect()
    screen = ctrl.screenshot()
    ctrl.click(0.5, 0.5)
    ctrl.disconnect()

分层结构（T3-1 拆分后）
--------------------
本类只保留**业务语义**（截图/点击/按键/文本/应用管理）与编排，四种底层机制
各自独立成模块，互不依赖：

===========================  ==========================================
模块                          职责
===========================  ==========================================
:mod:`.adb_conn`              serial 解析、adb server 重启、3 次重试接入
:mod:`.video_stream`          scrcpy server 部署、建流、H264 解码、自动重连
:mod:`.touch`                 触控三级降级链（MaaTouch → 常驻 shell → adb）
:mod:`.protocol`              :class:`AndroidController` 抽象接口
:mod:`.nemu_ipc`              MuMu 渲染共享内存直读（截图加速旁路）
===========================  ==========================================
"""

from __future__ import annotations

import struct
import time
from typing import TYPE_CHECKING

from autowsgr.infra import EmulatorConfig, EmulatorConnectionError, RepeatedActionError
from autowsgr.infra.config import operation_delay
from autowsgr.infra.logger import caller_info, get_logger

from .adb_conn import AdbDeviceConnector, SerialResolver
from .protocol import AndroidController, DeviceInfo
from .touch import TouchChannelChain
from .video_stream import ScrcpyVideoStream


if TYPE_CHECKING:
    import numpy as np
    from adbutils import AdbDevice

_log = get_logger('emulator')

# ── 重复动作检测 (2026-10-12, 卡死诊断) ──
#: 同一坐标连续点击超过该秒数即判定界面无进展(秒)。
#:
#: 为什么不用「速率」判据（2026-10-12 修正）：实测正常的状态识别轮询
#: 最高可达 **66 次/10 秒**（遍及 2026-10-09/10/11 十余个日志），
#: 「10 秒 20 次」这种速率阈值会**必然误报**正常战斗。轮询本来就该高频，
#: 真正说明无进展的是「点了这么久还没匹配上」，故改用**持续时长**。
#:
#: 60 的依据：战斗层各状态超时上限为 30s（``FIGHT_PERIOD``）、
#: 90s（``RESULT``）—— 但那些超时触发时状态机已自行退出并判失败，
#: 不会再点下去；这里只兜住「超时机制之外仍死磕」的情形。
#: 60s 远大于任一单次轮询窗口，又远小于真实故障的 185s。
REPEAT_STUCK_SECONDS = 60.0

# ── scrcpy 2.7 控制流协议常量 ──
# 参考：scrcpy app/src/control_msg.h / control_msg.c (v2.7)

# 控制消息类型
_TYPE_INJECT_KEYCODE = 0
_TYPE_INJECT_TEXT = 1
_TYPE_INJECT_SCROLL_EVENT = 3
_TYPE_SET_CLIPBOARD = 9

# SET_CLIPBOARD 文本上限（SC_CONTROL_MSG_CLIPBOARD_TEXT_MAX_LENGTH = 1<<18 - 14）
_CLIPBOARD_TEXT_MAX_LENGTH = (1 << 18) - 14

# Android KeyEvent 动作码
_KEY_ACTION_DOWN = 0
_KEY_ACTION_UP = 1

# 同一手势内相邻控制消息（如 DOWN→UP、按键 DOWN→UP）之间的最小间隔。
_MIN_GESTURE_INTERVAL = 0.01  # 10ms


class ScrcpyController(AndroidController):
    """基于 scrcpy 协议的 Android 设备控制器。

    截图通过 scrcpy-server 提供的 H264 视频流解码获得（30+ fps）；
    按键与文本输入通过 scrcpy 控制流实现，延迟低。触控（click/swipe/long_tap）
    走 adb ``input`` 命令（MaaTouch 等模拟器的内置 scrcpy 服务不支持
    INJECT_TOUCH_EVENT），由 :class:`TouchChannelChain` 三级降级提速。
    仅应用管理类操作仍走 ADB。

    Parameters
    ----------
    serial:
        设备的 ADB serial 地址（如 ``"emulator-5554"``、``"127.0.0.1:16384"``）。
        为 None 时自动检测。
    config:
        :class:`~autowsgr.infra.config.EmulatorConfig` 实例。
    max_size:
        视频流最大尺寸（宽或高的上限，0 = 不限制）。
    bitrate:
        视频流码率（bps），默认 8Mbps。
    max_fps:
        视频流最大帧率（0 = 不限制）。
    screenshot_timeout:
        截图超时（秒），超过仍无帧时抛出异常。
    """

    def __init__(
        self,
        serial: str | None = None,
        config: EmulatorConfig | None = None,
        max_size: int = 0,
        bitrate: int = 8_000_000,
        max_fps: int = 0,
        screenshot_timeout: float = 10.0,
    ) -> None:
        self._serial = serial
        self._config = config
        self._max_size = max_size
        self._bitrate = bitrate
        self._max_fps = max_fps
        self._screenshot_timeout = screenshot_timeout

        self._device: AdbDevice | None = None
        self._resolution: tuple[int, int] = (0, 0)
        self._native_resolution: tuple[int, int] = (0, 0)
        self._stream: ScrcpyVideoStream | None = None

        # 重复动作检测 (2026-10-12): 键=归一化坐标, 值=连续点击的起始时刻
        self._repeat_since: dict[tuple[float, float], float] = {}
        self._last_click_caller = '<unknown>'

        # 加速通道 (connect() 中初始化; 未连接前为 None)
        self._nemu: 'NemuIpcCapture | None' = None
        self._adb_path: str | None = None
        # 触控注入链 (MaaTouch → 常驻 shell → 单次 adb), 通道在 connect() 中挂载
        self._touch = TouchChannelChain(device_provider=self._require_device)

    # ── 连接 ──

    def connect(self) -> DeviceInfo:
        # ── serial 解析 ──
        resolved = SerialResolver(self._serial, self._config).resolve()
        self._serial = resolved or None

        # ── 连接 adbutils 设备 ──
        self._device = AdbDeviceConnector.connect(resolved)
        if not resolved:
            # 未指定设备时按发现的第一台回填 serial（历史行为）
            self._serial = self._device.serial

        # ── 获取初始分辨率 ──
        wsize = self._device.window_size()
        if wsize:
            self._resolution = (wsize[0], wsize[1])
        else:
            self._resolution = (960, 540)  # fallback
            _log.warning('[Emulator] 无法获取设备分辨率，使用默认值 960x540')
        # 记录设备真实分辨率：scrcpy 会把解码帧高度裁剪为 8 的整数倍
        # （如 960x540 → 960x536），而页面像素签名按真实分辨率标定，
        # 后续在解码循环中据此把帧高度补齐回真实值。
        self._native_resolution = self._resolution

        # ── 打开 scrcpy 视频流 ──
        self._stream = ScrcpyVideoStream(
            device_provider=self._require_device,
            native_resolution=self._native_resolution,
            max_size=self._max_size,
            bitrate=self._bitrate,
            max_fps=self._max_fps,
            screenshot_timeout=self._screenshot_timeout,
        )
        self._stream.open()

        # ── 加速通道初始化 (可选, 失败自动回退) ──
        # 先解析一次 adb 路径给下面两个触控通道共用 (MaaTouch 需要它启动服务,
        # 常驻 shell 需要它拉管道)。nemu 探测成功时会用安装目录覆盖为更精确的路径。
        self._adb_path = self._resolve_adb_path()

        # nemu_ipc: 直读 MuMu 渲染共享内存 (实测 ~3ms vs 流链路 50-150ms)
        self._nemu: NemuIpcCapture | None = None
        try:
            import os as _os

            from .nemu_ipc import NemuIpcCapture, detect_mumu_folder

            folder = detect_mumu_folder()
            if folder is not None:
                # 先锁定 adb 路径再构造捕获器：捕获器构造可能失败，
                # 若放在其后就会连带跳过 adb 覆盖（常驻 shell 又退化没了）。
                adb_candidate = _os.path.join(folder, 'nx_main', 'adb.exe')
                if _os.path.exists(adb_candidate):
                    self._adb_path = adb_candidate
                self._nemu = NemuIpcCapture(folder, self._serial or '', self._resolution)
        except Exception as exc:
            self._nemu = None
            _log.info('[Emulator] nemu_ipc 不可用, 截图继续走 scrcpy 流: {}', exc)
        # MaaTouch: socket 流注入 (写入即注入 ~0ms, 断连写抛异常可感知)
        try:
            import os as _os

            from .maatouch import MaaTouchController

            local = 'bin/MaaTouch/maatouch'
            local = local if _os.path.exists(local) else None
            self._touch.attach_maatouch(
                MaaTouchController(
                    adb_path=self._adb_path or 'adb',
                    serial=self._serial or '',
                    local_path=local,
                    screen_size=self._resolution,
                )
            )
        except Exception as exc:
            self._touch.attach_maatouch(None)
            _log.info('[Emulator] MaaTouch 不可用, 点击走 persistent/adb: {}', exc)
        # 常驻 adb shell: 点击免进程开销 (实测提交 ~0ms vs 单次 70ms)
        try:
            from .nemu_ipc import PersistentAdbShell

            adb_path = self._adb_path
            shell = PersistentAdbShell(adb_path, self._serial or '') if adb_path else None
            self._touch.attach_persistent_shell(shell)
            if shell is not None:
                _log.info('[Emulator] 常驻 shell 已就绪 (adb={})', adb_path)
        except Exception as exc:
            self._touch.attach_persistent_shell(None)
            _log.info('[Emulator] 常驻 shell 不可用, 点击继续走单次 adb: {}', exc)

        _log.info(
            '[Emulator] 已连接设备 (scrcpy): {} ({}x{})',
            self._serial or 'auto',
            *self._resolution,
        )
        return DeviceInfo(
            serial=self._serial or 'auto',
            resolution=self._resolution,
        )

    def _resolve_adb_path(self) -> str | None:
        """定位 adb 可执行文件路径。

        三级来源，按可靠性降序：

        1. ``connect()`` 中从 MuMu 安装目录（nemu_ipc 探测）锁定的路径
        2. 系统 PATH（``shutil.which('adb')``）
        3. 注册表里的模拟器安装目录（:func:`_find_adb`，Windows）

        第3 级是**必须的**：Windows 上 ``shutil.which('adb')`` 几乎总是返回
        ``None``（adb 不在 PATH 里），此前只做第 2 级导致常驻 shell 通道恒不可用，
        触控长期落在最慢的单次 ``adb input`` 上（约 70ms/次）。

        找不到时返回 ``None``（由调用方决定降级），不抛异常 —— 这是
        ``connect()`` 的可选加速路径，找不到 adb 不该阻断整个连接。
        """
        if self._adb_path:
            return self._adb_path

        import shutil

        found = shutil.which('adb')
        if found:
            return found

        try:
            from ..detector import _find_adb

            return _find_adb()
        except Exception as exc:
            _log.debug('[Emulator] 无法定位 adb: {}', exc)
            return None

    def disconnect(self) -> None:
        serial = self._serial or 'auto'
        if self._stream is not None:
            self._stream.reset()

        # 释放加速通道
        if self._nemu is not None:
            try:
                self._nemu.close()
            except Exception:
                pass
            self._nemu = None
        self._touch.close()

        self._device = None
        self._resolution = (0, 0)
        _log.info('[Emulator] 已断开设备连接: {}', serial)

    @property
    def resolution(self) -> tuple[int, int]:
        return self._resolution

    def _require_device(self) -> AdbDevice:
        """返回已连接的设备实例，未连接时抛出异常。"""
        if self._device is None:
            raise EmulatorConnectionError('设备未连接，请先调用 connect()')
        return self._device

    # ── 控制流消息发送 ──

    def _send_control(self, data: bytes) -> None:
        """向控制 socket 串行写入一帧消息（转发到视频流）。"""
        self._require_stream().send_control(data)

    def _require_stream(self) -> ScrcpyVideoStream:
        """返回视频流对象；未连接时抛出异常。"""
        if self._stream is None:
            raise EmulatorConnectionError('视频流未初始化，请先调用 connect()')
        return self._stream

    @staticmethod
    def _float_to_u16fp(value: float) -> int:
        """将 [0.0, 1.0] 压力值编码为 scrcpy u16 定点数。"""
        clamped = max(0.0, min(1.0, value))
        return round(clamped * 0xFFFF)

    @staticmethod
    def _float_to_i16fp(value: float) -> int:
        """将 [-1.0, 1.0] 滚动量编码为 scrcpy i16 定点数。"""
        clamped = max(-1.0, min(1.0, value))
        return round(clamped * 0x7FFF)

    def _to_absolute(self, x: float, y: float) -> tuple[int, int]:
        """将相对坐标 [0.0, 1.0] 转换为设备像素坐标。"""
        w, h = self._resolution
        return int(x * w), int(y * h)

    def _inject_keycode(self, key_code: int, action: int = _KEY_ACTION_UP) -> None:
        """发送 INJECT_KEYCODE 控制消息（scrcpy 2.7，14 字节）。

        布局：type(1) | action(1) | keycode(4) | repeat(4) | metastate(4)
        """
        data = struct.pack(
            '>BBIII',
            _TYPE_INJECT_KEYCODE,
            action,
            key_code,
            0,  # repeat
            0,  # metastate
        )
        self._send_control(data)

    def _inject_scroll(
        self,
        x: float,
        y: float,
        *,
        horizontal: float,
        vertical: float,
    ) -> None:
        """发送 INJECT_SCROLL_EVENT 控制消息（scrcpy 2.7，21 字节）。"""
        width, height = self._resolution
        px, py = self._to_absolute(x, y)
        data = struct.pack(
            '>BIIHHhhI',
            _TYPE_INJECT_SCROLL_EVENT,
            px,
            py,
            width,
            height,
            self._float_to_i16fp(horizontal),
            self._float_to_i16fp(vertical),
            0,
        )
        self._send_control(data)

    def _inject_text(self, content: str) -> None:
        """发送 INJECT_TEXT 控制消息（scrcpy 2.7）。

        布局：type(1) | length(4) | utf8_bytes
        最大长度 300 字节。

        注意：INJECT_TEXT 通过 InputConnection.commitText 注入，
        仅对 ASCII/拉丁字符可靠；中文等多字节字符请使用 :meth:`_set_clipboard`。
        """
        raw = content.encode('utf-8')[:300]
        data = struct.pack('>BI', _TYPE_INJECT_TEXT, len(raw)) + raw
        self._send_control(data)

    def _set_clipboard(self, content: str, paste: bool = True) -> None:
        """发送 SET_CLIPBOARD 控制消息（scrcpy 2.7）。

        布局：type(1) | sequence(8) | paste(1) | length(4) | utf8_bytes

        ``paste=True`` 时，server 在设置剪贴板后会自动注入
        ``KEYCODE_PASTE``（Android ≥ 7），实现立即粘贴。
        该路径通过系统剪贴板 + 粘贴键工作，可正确输入中文等
        INJECT_TEXT 无法处理的字符。

        Parameters
        ----------
        content:
            要输入的文本（UTF-8，最长 262130 字节）。
        paste:
            是否在设置剪贴板后立即粘贴。
        """
        raw = content.encode('utf-8')[:_CLIPBOARD_TEXT_MAX_LENGTH]
        # sequence=0 表示 SEQUENCE_INVALID，不请求 ack
        data = struct.pack('>BQBI', _TYPE_SET_CLIPBOARD, 0, 1 if paste else 0, len(raw)) + raw
        self._send_control(data)

    # ── 截图 ──

    def screenshot(self) -> np.ndarray:
        stream = self._require_stream()
        stream.ensure_alive()

        # ── 加速通道: nemu_ipc 共享内存直读 (失败永久降级回流) ──
        if self._nemu is not None:
            try:
                return self._nemu.capture()
            except Exception as exc:
                _log.warning(
                    '[Emulator] nemu_ipc 持续失败, 永久降级回 scrcpy 流: {}', exc)
                try:
                    self._nemu.close()
                except Exception:
                    pass
                self._nemu = None

        start = time.monotonic()
        while True:
            frame = stream.latest_frame()

            if frame is not None:
                elapsed = time.monotonic() - start
                h, w = frame.shape[:2]
                _log.trace(
                    '[Emulator] 截图完成 {}x{} 耗时={:.3f}s',
                    w,
                    h,
                    elapsed,
                )
                return frame

            if not stream.alive:
                stream.ensure_alive()
                start = time.monotonic()
                continue

            if time.monotonic() - start > self._screenshot_timeout:
                raise EmulatorConnectionError(
                    f'截图超时 ({self._screenshot_timeout}s)，scrcpy 视频流无数据'
                )
            time.sleep(0.01)

    # ── 触控 ──
    # 注意：MuMu 12 等模拟器内置了自己的 scrcpy 协议服务（占用 "scrcpy" abstract
    # socket，且不会真正执行我们部署的 server），其实现支持 INJECT_KEYCODE 但
    # 不支持 INJECT_TOUCH_EVENT（v1/v2 格式均无效，实测 2026-10-08）。
    # 因此触控统一走 adb `input` 命令（约 50ms 开销，稳定性已实测），
    # 截图（视频流）与按键（keycode）仍走 scrcpy 通道保持低延迟。
    # 详见 TouchChannelChain。

    def _adb_input(self, args: list[str], timeout: float = 10.0) -> None:
        """注入触控事件（转发到触控通道链）。

        实现见 :class:`~autowsgr.emulator.controller.touch.TouchChannelChain`：
        MaaTouch (socket 流注入, 仅 tap) → 常驻 shell 会话 → 单次 adb input，
        每级失败自动永久降级到下一级。
        """
        self._touch.inject(args, timeout)

    # 引入一个开关，当参数为：click(x, y, delay=False) 时关闭延迟，该方法默认打开全局延迟，全局延迟可以在 config.py 内设置
    def click(self, x: float, y: float, *, delay: bool = True) -> None:
        self._last_click_caller = caller_info()
        self._note_action(x, y)
        w, h = self._resolution
        px, py = self._to_absolute(x, y)
        _log.debug(
            '[Emulator] click({:.3f}, {:.3f}) → pixel({}, {})  res={}x{}  {}',
            x,
            y,
            px,
            py,
            w,
            h,
            caller_info(),
        )
        self._adb_input(['tap', str(px), str(py)])

        if delay:  # True 才走延迟
            time.sleep(operation_delay())

    def _note_action(self, x: float, y: float) -> None:
        """记录一次点击, 若同坐标持续重复且**无任何进展**则抛异常。

        为什么需要它
        ------------
        7x24 挂机有一种故障逃过了所有超时与熔断：**界面无进展但脚本仍在
        按节奏重复点同一处**。实测（2026-10-11 用户手动关 MuMu，日志
        ``logs/2026-10-11_08-28-24``）该坐标被点了 **123 次 / 持续 185 秒**，
        而同一日志里其它坐标均只出现 1 次。

        ⚠️ 判据是**持续时长**而非速率（2026-10-12 修正）
        --------------------------------------------
        初版用「10 秒内超过 20 次」判定，实测日志证明**必然误报**：
        正常的状态识别轮询（``recognizer.wait_for_phase`` 在等待动画时
        每 100~180ms 点一次加速）最高能到**66 次/10 秒**，样本遍及
        2026-10-09/10/11 十多个日志文件。速率阈值无法区分二者
        —— 因为轮询本来就该高频，「点了多少次」不是无进展的证据，
        **「点了这么久还没匹配上」才是**。

        故改为：同坐标连续点击持续超过 :data:`REPEAT_STUCK_SECONDS`
        即判定无进展。185 秒的真实故障 vs 30 秒的状态超时有充分余量。
        """
        now = time.monotonic()
        key = (round(x, 3), round(y, 3))
        first = self._repeat_since.get(key)
        if first is None:
            self._repeat_since[key] = now
            return

        span = now - first
        if span > REPEAT_STUCK_SECONDS:
            raise RepeatedActionError(
                f'同一坐标 ({x:.3f}, {y:.3f}) 已连续点击 {span:.1f}s '
                f'(阈值 {REPEAT_STUCK_SECONDS:g}s) 仍未匹配到新状态，'
                f'判定界面无进展 —— 上次调用: {self._last_click_caller}'
            )

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration: float = 0.5,
        *,
        delay: bool = True,
    ) -> None:
        px1, py1 = self._to_absolute(x1, y1)
        px2, py2 = self._to_absolute(x2, y2)
        ms = int(duration * 1000)
        _log.debug(
            '[Emulator] swipe({:.3f},{:.3f}→{:.3f},{:.3f}) → pixel({},{}→{},{}) {}ms  {}',
            x1,
            y1,
            x2,
            y2,
            px1,
            py1,
            px2,
            py2,
            ms,
            caller_info(),
        )
        # adb input swipe 由系统内部插值，节奏平滑
        self._adb_input(['swipe', str(px1), str(py1), str(px2), str(py2), str(ms)])

        # 增加延迟，改动同 click_delay
        if delay:  # True 才走延迟
            time.sleep(operation_delay())

    def scroll(
        self,
        x: float,
        y: float,
        *,
        horizontal: float = 0.0,
        vertical: float = 0.0,
        delay: bool = True,
    ) -> None:
        _log.trace(
            '[Emulator] scroll({:.3f}, {:.3f}, h={:.3f}, v={:.3f})  {}',
            x,
            y,
            horizontal,
            vertical,
            caller_info(),
        )
        self._inject_scroll(x, y, horizontal=horizontal, vertical=vertical)
        if delay:
            time.sleep(operation_delay())

    def long_tap(self, x: float, y: float, duration: float = 1.0) -> None:
        px, py = self._to_absolute(x, y)
        ms = int(duration * 1000)
        _log.debug(
            '[Emulator] long_tap({:.3f}, {:.3f}) → pixel({},{}) {}ms  {}',
            x,
            y,
            px,
            py,
            ms,
            caller_info(),
        )
        # 长按 = 同点 swipe（系统按 duration 保持按压）
        self._adb_input(['swipe', str(px), str(py), str(px), str(py), str(ms)])

    # ── 按键 ──
    def key_event(self, key_code: int, *, delay: bool = True) -> None:
        _log.debug('[Emulator] key_event({})  {}', key_code, caller_info())
        # 发送 DOWN + UP 完成一次按键
        self._inject_keycode(key_code, action=_KEY_ACTION_DOWN)
        time.sleep(_MIN_GESTURE_INTERVAL)  # DOWN/UP 间留出最小间隔
        self._inject_keycode(key_code, action=_KEY_ACTION_UP)

        # 增加延迟，改动同 click_delay
        if delay:  # True 才走延迟
            time.sleep(operation_delay())

    def text(self, content: str, *, delay: bool = True) -> None:
        _log.debug("[Emulator] text('{}')  {}", content, caller_info())
        if content.isascii():
            # 纯 ASCII/拉丁字符走 INJECT_TEXT（延迟更低）
            self._inject_text(content)
        else:
            # 含中文等多字节字符 → SET_CLIPBOARD + paste（Android ≥ 7）
            self._set_clipboard(content, paste=True)

        # 增加延迟，改动同 click_delay
        if delay:  # True 才走延迟
            time.sleep(operation_delay())

    # ── 应用管理 ──

    def start_app(self, package: str, *, delay: bool = True) -> None:
        dev = self._require_device()
        _log.info('[Emulator] 启动应用: {}  {}', package, caller_info())
        dev.app_start(package)

        # 增加延迟，改动同 click_delay
        if delay:  # True 才走延迟
            time.sleep(operation_delay())

    def stop_app(self, package: str, *, delay: bool = True) -> None:
        dev = self._require_device()
        _log.info('[Emulator] 停止应用: {}  {}', package, caller_info())
        dev.app_stop(package)

        # 增加延迟，改动同 click_delay
        if delay:  # True 才走延迟
            time.sleep(operation_delay())

    def is_app_running(self, package: str) -> bool:
        try:
            dev = self._require_device()
            ps_output = dev.shell('ps -A')
        except Exception as exc:
            _log.debug(
                "[Emulator] is_app_running('{}') → False (设备异常: {})  {}",
                package,
                exc,
                caller_info(),
            )
            return False
        running = package in ps_output
        _log.debug("[Emulator] is_app_running('{}') → {}  {}", package, running, caller_info())
        return running

    # ── Shell ──

    def shell(self, cmd: str) -> str:
        dev = self._require_device()
        result = dev.shell(cmd)
        if not isinstance(result, str):
            return str(result)
        return result
