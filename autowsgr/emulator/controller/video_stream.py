"""scrcpy 视频流生命周期 — 部署 server、建socket、解码、断开、重连。

为什么单独抽
------------
:class:`~autowsgr.emulator.controller.scrcpy.ScrcpyController` 里混装了
「视频流生命周期」和「业务语义（截图/点击/按键）」两件事，而前者的状态机
相当自洽：:meth:`ScrcpyVideoStream.open` → :meth:`close` → :meth:`reopen`
→ :meth:`ensure_alive`，与 ADB 设备对象、坐标换算、业务日志都无关。

抽出来之后好处是这段最容易出错的代码（重连、双重检查锁、帧高补齐）
可以脱离真机做单元测试：只需注入一个假的 device对象即可。

不变量（改动时必须保持）
------------------------
1. **帧高补齐**：scrcpy 把解码帧高裁到 8 的整数倍（960x540 → 960x536），
   而页面像素签名按设备真实分辨率标定。解码循环必须把底部补齐回真实高度，
   否则锚点像素整体错位。**只补高、不缩放**（缩放会扭曲投影）。
2. **重连幂等**：:meth:`ensure_alive` 在多线程下可能并发进入，用
   ``_reconnect_lock`` + 双重检查串行化，避免同时开两条流。
3. **分辨率可变**：解码帧实际尺寸可能与设备真实分辨率不同（补齐后即为
   真实值），业务侧的坐标换算读的是这里的 ``resolution``。
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from autowsgr.infra import EmulatorConnectionError
from autowsgr.infra.logger import get_logger

if TYPE_CHECKING:
    import numpy as np
    from adbutils import AdbConnection, AdbDevice


_log = get_logger('emulator')

# scrcpy-server.jar 打包在 autowsgr/data/bin/ 下
SCRCPY_SERVER_JAR = Path(__file__).resolve().parents[2] / 'data' / 'bin' / 'scrcpy-server.jar'
SCRCPY_SERVER_VERSION = '2.7'
DEVICE_JAR_PATH = '/data/local/tmp/scrcpy-server.jar'


class ScrcpyVideoStream:
    """一条 scrcpy 视频流 + 控制流及其解码线程。

    Parameters
    ----------
    device_provider:
        返回 :class:`adbutils.AdbDevice` 的可调用对象（方法引用，不在构造期求值）。
    native_resolution:
        设备**真实**分辨率 ``(w, h)``，用于帧高补齐。
    max_size, bitrate, max_fps:
        原样传给 scrcpy-server 的视频参数。
    screenshot_timeout:
        等待首帧的超时（秒）。
    """

    def __init__(
        self,
        *,
        device_provider: Callable[[], AdbDevice],
        native_resolution: tuple[int, int],
        max_size: int = 0,
        bitrate: int = 8_000_000,
        max_fps: int = 0,
        screenshot_timeout: float = 10.0,
    ) -> None:
        self._device_provider = device_provider
        self._native_resolution = native_resolution
        self._max_size = max_size
        self._bitrate = bitrate
        self._max_fps = max_fps
        self._screenshot_timeout = screenshot_timeout

        self._resolution: tuple[int, int] = native_resolution
        self._last_frame: np.ndarray | None = None

        self._alive = False
        self._server_stream: AdbConnection | None = None
        self._video_socket: socket.socket | None = None
        self._control_socket: socket.socket | None = None
        self._decode_thread: threading.Thread | None = None
        self._frame_ready = threading.Event()  # 首帧就绪信号
        self._frame_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._control_lock = threading.Lock()  # 控制流写串行化

    # ── 只读属性 ──

    @property
    def resolution(self) -> tuple[int, int]:
        """当前帧分辨率 ``(w, h)``（解码得到，可能已与 native 不同）。"""
        return self._resolution

    @property
    def native_resolution(self) -> tuple[int, int]:
        """设备真实分辨率（帧高补齐的目标）。"""
        return self._native_resolution

    @property
    def alive(self) -> bool:
        """视频流是否在运行。"""
        return self._alive

    @property
    def control_connected(self) -> bool:
        """控制通道是否就绪。"""
        return self._control_socket is not None

    def latest_frame(self) -> np.ndarray | None:
        """取最近一帧（线程安全）。"""
        with self._frame_lock:
            return self._last_frame

    # ── 打开 / 关闭 ──

    def open(self) -> None:
        """部署并启动 scrcpy-server，建流并等待首帧。

        Raises
        ------
        EmulatorConnectionError
            jar 缺失、socket 超时、或 ``screenshot_timeout`` 内无首帧。
        """
        self._deploy_server()
        self._start_server()
        self._connect_video_socket()
        self._connect_control_socket()
        self._start_decode_thread()
        if not self._frame_ready.wait(timeout=self._screenshot_timeout):
            raise EmulatorConnectionError(
                f'scrcpy 视频流未能在 {self._screenshot_timeout}s 内产生首帧'
            )

    def close(self) -> None:
        """关闭所有流与解码线程（幂等）。"""
        for sock in (self._control_socket, self._video_socket):
            if sock is not None:
                try:
                    sock.close()
                except Exception as exc:
                    _log.debug('[Scrcpy] 关闭 socket 失败: {}', exc)
        self._control_socket = None
        self._video_socket = None

        if self._server_stream is not None:
            try:
                self._server_stream.close()
            except Exception as exc:
                _log.debug('[Scrcpy] 关闭 server stream 失败: {}', exc)
            self._server_stream = None

        if self._decode_thread is not None:
            self._decode_thread.join(timeout=3.0)
            self._decode_thread = None

        self._alive = False
        self._frame_ready.clear()

    def reopen(self) -> None:
        """在保持 ADB 设备连接的前提下重建视频流与控制流。"""
        self.close()
        self._last_frame = None
        self.open()
        _log.info('[Emulator] scrcpy 视频流已重连')

    def ensure_alive(self) -> None:
        """确保视频流可用，不可用时尝试一次重连（线程安全）。"""
        if self._alive:
            return
        with self._reconnect_lock:
            if self._alive:
                return
            _log.warning('[Emulator] 检测到视频流未运行，尝试自动重连')
            self.reopen()

    def reset(self) -> None:
        """断开后清空帧与分辨率（设备级断开用，不重建流）。"""
        self.close()
        self._last_frame = None
        self._resolution = (0, 0)

    # ── 控制流 ──

    def send_control(self, data: bytes) -> None:
        """向控制 socket 串行写入一帧消息（线程安全，失败自动重连重试）。"""
        sock = self._require_control_socket()
        with self._control_lock:
            try:
                sock.sendall(data)
            except (ConnectionError, OSError) as exc:
                _log.warning('[Emulator] 控制流发送失败，触发重连: {}', exc)
                self._alive = False
                self.ensure_alive()
                sock = self._require_control_socket()
                sock.sendall(data)

    def _require_control_socket(self) -> socket.socket:
        self.ensure_alive()
        if self._control_socket is None:
            raise EmulatorConnectionError('scrcpy 控制通道未连接')
        return self._control_socket

    # ── 内部：部署与启动 ──

    def _require_device(self) -> AdbDevice:
        dev = self._device_provider()
        if dev is None:
            raise EmulatorConnectionError('设备未连接，请先调用 connect()')
        return dev

    def _deploy_server(self) -> None:
        if not SCRCPY_SERVER_JAR.exists():
            raise EmulatorConnectionError(
                f'找不到 scrcpy-server.jar: {SCRCPY_SERVER_JAR}\n请确认 autowsgr 包数据完整'
            )
        _log.debug('[Emulator] 推送 scrcpy-server.jar 到设备...')
        self._require_device().push(str(SCRCPY_SERVER_JAR), DEVICE_JAR_PATH)

    def _start_server(self) -> None:
        cmd = [
            f'CLASSPATH={DEVICE_JAR_PATH}',
            'app_process',
            '/',
            'com.genymobile.scrcpy.Server',
            SCRCPY_SERVER_VERSION,
            'log_level=info',
            'tunnel_forward=true',
            'video=true',
            'audio=false',
            'control=true',
            f'max_size={self._max_size}',
            f'video_bit_rate={self._bitrate}',
            f'max_fps={self._max_fps}',
            'video_codec=h264',
            'send_device_meta=false',
            'send_frame_meta=false',
            'send_codec_meta=false',
            'send_dummy_byte=true',
        ]
        _log.debug('[Emulator] 启动 scrcpy-server: {}', ' '.join(cmd))
        self._server_stream = self._require_device().shell(cmd, stream=True)
        time.sleep(0.5)  # 给 server 一点启动时间

    def _connect_video_socket(self) -> None:
        import adbutils

        dev = self._require_device()
        for _attempt in range(30):
            try:
                self._video_socket = dev.create_connection(
                    adbutils.Network.LOCAL_ABSTRACT,
                    'scrcpy',
                )
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise EmulatorConnectionError('无法连接 scrcpy-server 视频通道（3s 超时）')

        dummy = self._video_socket.recv(1)
        if not dummy:
            raise EmulatorConnectionError('未收到 scrcpy dummy byte，连接可能已断开')
        _log.debug('[Emulator] scrcpy 视频通道已连接')

    def _connect_control_socket(self) -> None:
        import adbutils

        dev = self._require_device()
        for _attempt in range(30):
            try:
                self._control_socket = dev.create_connection(
                    adbutils.Network.LOCAL_ABSTRACT,
                    'scrcpy',
                )
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise EmulatorConnectionError('无法连接 scrcpy-server 控制通道（3s 超时）')

        # 禁用 Nagle，降低控制指令延迟
        try:
            self._control_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        _log.debug('[Emulator] scrcpy 控制通道已连接')

    # ── 内部：解码 ──

    def _start_decode_thread(self) -> None:
        self._alive = True
        self._decode_thread = threading.Thread(
            target=self._stream_loop,
            name='scrcpy-decode',
            daemon=True,
        )
        self._decode_thread.start()

    def _stream_loop(self) -> None:
        import av

        codec = av.CodecContext.create('h264', 'r')
        video_sock = self._video_socket
        assert video_sock is not None
        while self._alive:
            try:
                raw = video_sock.recv(0x10000)
                if not raw:
                    if self._alive:
                        _log.warning('[Emulator] scrcpy 视频流已断开')
                        self._alive = False
                    break

                for packet in codec.parse(raw):
                    for frame in codec.decode(packet):
                        rgb = self._postprocess(frame.to_ndarray(format='rgb24'))
                        h, w = rgb.shape[:2]
                        with self._frame_lock:
                            self._last_frame = rgb
                            if self._resolution != (w, h):
                                _log.info('[Emulator] scrcpy 视频分辨率: {}x{}', w, h)
                                self._resolution = (w, h)
                        if not self._frame_ready.is_set():
                            self._frame_ready.set()

            except BlockingIOError:
                time.sleep(0.01)
            except (ConnectionError, OSError) as exc:
                if self._alive:
                    _log.warning('[Emulator] scrcpy 视频流异常: {}', exc)
                    self._alive = False
                break
            except BaseException as exc:
                # ⚠️ 兜底: 解码库的异常类型**不在** (ConnectionError, OSError) 之内
                # (实测 `av.FFmpegError` 不是 OSError 子类), 上面那个 except 捕不到,
                # 会让本线程**静默死亡**而 `_alive` 仍是 True ——
                # 结果是 screenshot() 永远返回过期帧, ensure_alive() 也认为健康,
                # 表现为「界面卡住不动」而非报错, 极难排查。
                # 任何未预料的异常都必须把 _alive 置否, 让上层走重连。
                if self._alive:
                    _log.opt(exception=True).warning(
                        '[Emulator] scrcpy 解码线程异常 ({}), 标记视频流不可用',
                        type(exc).__name__,
                    )
                    self._alive = False
                break

    def _postprocess(self, rgb: np.ndarray) -> np.ndarray:
        """把解码帧高度补齐到设备真实分辨率（不变量1）。"""
        native_h = self._native_resolution[1]
        if rgb.shape[0] >= native_h:
            return rgb
        import numpy as np

        return np.pad(
            rgb,
            ((0, native_h - rgb.shape[0]), (0, 0), (0, 0)),
            mode='edge',
        )