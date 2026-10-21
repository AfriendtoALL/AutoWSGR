"""ScrcpyVideoStream 的无设备单元测试。

设计要点：只注入假的 ``device``（``push`` / ``shell`` / ``create_connection``），
socket 用 :class:`socket.socketpair` 或轻量假对象替代，因此可以在 CI 上跑。

重点覆盖 :meth:`ScrcpyVideoStream._postprocess` —— **帧高补齐**是整个项目里
最容易被静默破坏的不变量：scrcpy 把帧高裁到 8 的整数倍（960x540 → 960x536），
而页面像素签名按真实分辨率标定；一旦不补齐，所有锚点像素整体上移，
表现为「所有模板识别莫名失效」，但截图本身仍然「看起来正常」。
"""

from __future__ import annotations

import socket
import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from autowsgr.emulator.controller.video_stream import (
    DEVICE_JAR_PATH,
    SCRCPY_SERVER_JAR,
    SCRCPY_SERVER_VERSION,
    ScrcpyVideoStream,
)
from autowsgr.infra import EmulatorConnectionError


def make_stream(
    *,
    native: tuple[int, int] = (960, 540),
    timeout: float = 0.2,
    device: object | None = None,
) -> ScrcpyVideoStream:
    return ScrcpyVideoStream(
        device_provider=lambda: device if device is not None else MagicMock(),
        native_resolution=native,
        screenshot_timeout=timeout,
    )


# ══════════════════════════════════════════════
# 帧高补齐（最关键的不变量）
# ══════════════════════════════════════════════


class TestFrameHeightPadding:
    def test_pads_540_to_536_up_to_540(self) -> None:
        """960x536 (8的整数倍) → 补齐为 960x540。"""
        s = make_stream(native=(960, 540))
        rgb = np.zeros((536, 960, 3), dtype=np.uint8)

        out = s._postprocess(rgb)

        assert out.shape == (540, 960, 3)

    def test_pad_uses_edge_replication(self) -> None:
        """补齐必须用边缘复制（缩放会扭曲投影）。"""
        s = make_stream(native=(960, 540))
        rgb = np.zeros((536, 960, 3), dtype=np.uint8)
        rgb[535, :, :] = 200  # 最后一行

        out = s._postprocess(rgb)

        assert out[536, 0, 0] == 200
        assert out[539, 0, 0] == 200

    def test_does_not_shrink_frames_taller_than_native(self) -> None:
        """解码帧比native 高时不裁剪（宁可多给像素也不丢）。"""
        s = make_stream(native=(960, 540))
        rgb = np.zeros((600, 960, 3), dtype=np.uint8)

        out = s._postprocess(rgb)

        assert out.shape == (600, 960, 3)

    def test_exact_height_passes_through_unchanged(self) -> None:
        """已是真实高度时必须是同一个对象（零拷贝）。"""
        s = make_stream(native=(960, 540))
        rgb = np.zeros((540, 960, 3), dtype=np.uint8)

        assert s._postprocess(rgb) is rgb

    def test_padding_preserves_width_and_dtype(self) -> None:
        s = make_stream(native=(1280, 720))
        rgb = np.zeros((712, 1280, 3), dtype=np.uint8)

        out = s._postprocess(rgb)

        assert out.shape == (720, 1280, 3)
        assert out.dtype == np.uint8


# ══════════════════════════════════════════════
# open / close 生命周期
# ══════════════════════════════════════════════


class TestLifecycle:
    def test_initial_state(self) -> None:
        s = make_stream()
        assert s.alive is False
        assert s.control_connected is False
        assert s.latest_frame() is None
        assert s.resolution == (960, 540)
        assert s.native_resolution == (960, 540)

    def test_close_is_idempotent(self) -> None:
        s = make_stream()
        s.close()
        s.close()
        assert s.alive is False

    def test_close_closes_both_sockets_and_server_stream(self) -> None:
        s = make_stream()
        v, c, srv = MagicMock(), MagicMock(), MagicMock()
        s._video_socket, s._control_socket, s._server_stream = v, c, srv

        s.close()

        v.close.assert_called_once()
        c.close.assert_called_once()
        srv.close.assert_called_once()
        assert s.control_connected is False
        assert s._video_socket is None
        assert s._server_stream is None

    def test_socket_close_error_does_not_break_close(self) -> None:
        """socket close 抛异常不能阻断其余资源的释放。"""
        s = make_stream()
        bad, good, srv = MagicMock(), MagicMock(), MagicMock()
        bad.close.side_effect = OSError('already closed')
        s._video_socket, s._control_socket, s._server_stream = bad, good, srv

        s.close()

        good.close.assert_called_once()
        srv.close.assert_called_once()
        assert s._server_stream is None

    def test_reset_clears_frame_and_resolution(self) -> None:
        """设备级断开(reset)后分辨率归零，与历史行为一致。"""
        s = make_stream()
        s._last_frame = np.zeros((540, 960, 3), dtype=np.uint8)
        s._alive = True

        s.reset()

        assert s.latest_frame() is None
        assert s.resolution == (0, 0)
        assert s.alive is False

    def test_open_deploys_correct_jar_and_args(self) -> None:
        """必须推包内 jar，且路径与协议版本不能被改错。"""
        assert SCRCPY_SERVER_JAR.exists(), '打包的 scrcpy-server.jar 缺失'
        assert SCRCPY_SERVER_VERSION == '2.7'
        assert DEVICE_JAR_PATH == '/data/local/tmp/scrcpy-server.jar'

        dev = MagicMock()
        s = make_stream(device=dev)
        s._deploy_server()
        dev.push.assert_called_once_with(str(SCRCPY_SERVER_JAR), DEVICE_JAR_PATH)

    def test_start_server_command_contract(self) -> None:
        """scrcpy server 启动参数是与设备端协议契约，不能随意改。"""
        dev = MagicMock()
        s = make_stream(device=dev, native=(960, 540))
        s._start_server()

        cmd = dev.shell.call_args[0][0]
        assert cmd[0] == f'CLASSPATH={DEVICE_JAR_PATH}'
        assert 'com.genymobile.scrcpy.Server' in cmd
        assert 'video=true' in cmd and 'control=true' in cmd
        # 这三个 meta 关闭后 video socket 才需要读 dummy byte
        assert 'send_device_meta=false' in cmd
        assert 'send_frame_meta=false' in cmd
        assert 'send_dummy_byte=true' in cmd
        assert dev.shell.call_args[1]['stream'] is True

    def test_require_device_raises_when_provider_returns_none(self) -> None:
        s = make_stream(device=None)
        s._device_provider = lambda: None
        with pytest.raises(EmulatorConnectionError, match='设备未连接'):
            s._require_device()

    def test_send_control_without_control_socket_raises(self) -> None:
        """流活着但控制通道缺失 → 明确报控制通道错误（不重连）。"""
        s = make_stream()
        s._alive = True
        s._control_socket = None
        with pytest.raises(EmulatorConnectionError, match='控制通道未连接'):
            s.send_control(b'x')


# ══════════════════════════════════════════════
# 控制流发送
# ══════════════════════════════════════════════


class TestSendControl:
    def test_send_control_writes_to_socket(self) -> None:
        s = make_stream()
        s._alive = True
        s._control_socket = MagicMock()

        s.send_control(b'abc')

        s._control_socket.sendall.assert_called_once_with(b'abc')

    def test_send_control_failure_triggers_reconnect_and_retry(self) -> None:
        """发送失败 → 标记死亡 → 重连 → 重发一次（只一次）。"""
        s = make_stream()
        s._alive = True
        first, second = MagicMock(), MagicMock()
        first.sendall.side_effect = ConnectionResetError('peer reset')
        s._control_socket = first

        reconnects: list[int] = []

        def fake_reopen() -> None:
            reconnects.append(1)
            s._alive = True
            s._control_socket = second

        s.reopen = fake_reopen  # type: ignore[method-assign]

        s.send_control(b'payload')

        assert len(reconnects) == 1
        second.sendall.assert_called_once_with(b'payload')

    def test_send_control_retry_is_not_infinite(self) -> None:
        """重连后仍失败必须抛出，不能无限重试。"""
        s = make_stream()
        s._alive = True
        sock = MagicMock()
        sock.sendall.side_effect = ConnectionResetError('always down')
        s._control_socket = sock

        def fake_reopen() -> None:
            s._alive = True
            s._control_socket = sock

        s.reopen = fake_reopen  # type: ignore[method-assign]

        with pytest.raises(ConnectionResetError):
            s.send_control(b'x')


# ══════════════════════════════════════════════
# ensure_alive 并发重连
# ══════════════════════════════════════════════


class TestEnsureAlive:
    def test_noop_when_alive(self) -> None:
        s = make_stream()
        s._alive = True
        s.reopen = MagicMock()  # type: ignore[method-assign]
        s.ensure_alive()
        s.reopen.assert_not_called()

    def test_reopens_when_dead(self) -> None:
        s = make_stream()
        s.reopen = MagicMock()  # type: ignore[method-assign]
        s.ensure_alive()
        s.reopen.assert_called_once()

    def test_concurrent_ensure_alive_reopens_only_once(self) -> None:
        """并发重连必须被串行化（否则同时开两条流）。"""
        s = make_stream()
        s._alive = False
        calls: list[int] = []
        barrier = threading.Barrier(8)

        def slow_reopen() -> None:
            calls.append(1)
            time.sleep(0.05)
            s._alive = True

        s.reopen = slow_reopen  # type: ignore[method-assign]

        threads = []
        for _ in range(8):
            t = threading.Thread(target=lambda: (barrier.wait(), s.ensure_alive()))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        assert len(calls) == 1, f'并发重连了 {len(calls)} 次，应为 1'


import time  # noqa: E402  (放在末尾以免打断上方阅读)


# ══════════════════════════════════════════════
# 模块常量契约
# ══════════════════════════════════════════════


def test_control_socket_sets_nodelay() -> None:
    """控制 socket 必须关 Nagle，否则按键延迟被合并。"""
    s = make_stream()
    real, peer = socket.socketpair()
    dev = MagicMock()
    dev.create_connection.side_effect = [real, peer]
    s._require_device = lambda: dev  # type: ignore[method-assign]

    s._connect_control_socket()

    assert real.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1
    real.close()
    peer.close()


def test_video_socket_requires_dummy_byte() -> None:
    """dummy byte 缺失说明连接已断，必须报错而非静默继续。"""
    s = make_stream()
    a, b = socket.socketpair()
    b.close()  # 对端关闭 → recv 返回 b''
    dev = MagicMock()
    dev.create_connection.return_value = a
    s._require_device = lambda: dev  # type: ignore[method-assign]

    with pytest.raises(EmulatorConnectionError, match='dummy byte'):
        s._connect_video_socket()
    a.close()