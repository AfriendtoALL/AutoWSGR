"""测试 emulator.controller 模块。

由于 ScrcpyController 依赖物理设备/模拟器，测试策略：
1. DeviceInfo — 不可变数据类
2. ScrcpyController — ABC 接口约束
3. ScrcpyController — 坐标转换（纯函数 _to_pixels，独立验证）
4. ScrcpyController — 控制流编排（在 _inject_* 边界断言动作序列与路由）

控制流的二进制线缆格式（struct 布局、TYPE_* 常量）属于 scrcpy 外部协议，在
scrcpy.py 中定义。这里不重复声明该格式来校验实现自身——那样只能保证“打包与解包
用了同一份格式”，无法发现“对真实 scrcpy 协议的误解”。改为只验证控制器自身的逻辑：
相对坐标→像素映射、动作序列（DOWN/UP/MOVE）、文本路由。动作码取自 Android 官方
定义（MotionEvent / KeyEvent），以字面量形式独立断言，不导入实现侧常量。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, call

import numpy as np
import pytest

from autowsgr.emulator import ScrcpyController
from autowsgr.emulator.controller.video_stream import ScrcpyVideoStream
from autowsgr.infra import EmulatorConnectionError


# ═══════════════════════════════════════════════
# ScrcpyController — 初始化 / 状态
# ═══════════════════════════════════════════════


class TestScrcpyControllerInit:
    """ScrcpyController 初始化行为。"""

    def test_disconnect_resets_state(self):
        ctrl = ScrcpyController(serial='s')
        ctrl._resolution = (1920, 1080)
        ctrl._device = MagicMock()
        ctrl.disconnect()
        assert ctrl._device is None
        assert ctrl._resolution == (0, 0)


# ═══════════════════════════════════════════════
# ScrcpyController — 坐标转换
# ═══════════════════════════════════════════════


class TestScrcpyControllerCoordinates:
    """测试相对坐标 [0, 1] → 像素坐标的转换（纯函数 _to_pixels）。

    预期像素值由 int(x * w) / int(y * h) 手工计算，不依赖任何线缆格式。
    """

    @pytest.fixture
    def ctrl(self) -> ScrcpyController:
        c = ScrcpyController(serial='test')
        c._resolution = (960, 540)
        return c

    def test_center(self, ctrl: ScrcpyController):
        assert ctrl._to_absolute(0.5, 0.5) == (480, 270)

    def test_top_left(self, ctrl: ScrcpyController):
        assert ctrl._to_absolute(0.0, 0.0) == (0, 0)

    def test_bottom_right(self, ctrl: ScrcpyController):
        assert ctrl._to_absolute(1.0, 1.0) == (960, 540)

    def test_quarter(self, ctrl: ScrcpyController):
        assert ctrl._to_absolute(0.25, 0.75) == (240, 405)

    def test_high_resolution(self):
        """1920x1080 分辨率下转换正确。"""
        c = ScrcpyController(serial='test')
        c._resolution = (1920, 1080)
        assert c._to_absolute(0.5, 0.5) == (960, 540)


# ═══════════════════════════════════════════════
# ScrcpyController — 控制流编排
# ═══════════════════════════════════════════════


class TestScrcpyControllerControlFlow:
    """测试 click/swipe/long_tap/key_event/text 的控制流编排。

    触控 (click/swipe/long_tap) 走 adb ``input`` 命令 (MuMu 内置 scrcpy 服务
    不支持 INJECT_TOUCH_EVENT，见 scrcpy.py 触控段注释)，在 ``_adb_input`` 边界
    上断言语义正确的命令与像素坐标；按键/文本仍走 scrcpy 控制流，在
    ``_inject_keycode`` / ``_inject_text`` / ``_set_clipboard`` 边界上断言，
    而不关心底层字节如何打包。

    动作码取自 Android 官方定义（不导入实现侧常量，从而能捕获实现侧常量写错的 bug）：
    - KeyEvent:    ACTION_DOWN=0, ACTION_UP=1
    """

    @pytest.fixture
    def ctrl(self) -> ScrcpyController:
        c = ScrcpyController(serial='test')
        c._resolution = (960, 540)
        c._device = MagicMock()
        return c

    def test_click_sends_adb_tap(self, ctrl: ScrcpyController):
        """click = adb input tap，坐标换算为设备像素。"""
        ctrl._adb_input = MagicMock()
        ctrl.click(0.5, 0.5, delay=False)
        ctrl._adb_input.assert_called_once_with(['tap', '480', '270'])

    def test_swipe_sends_adb_swipe(self, ctrl: ScrcpyController):
        """swipe = adb input swipe，两端点像素坐标 + 毫秒时长。"""
        ctrl._adb_input = MagicMock()
        ctrl.swipe(0.1, 0.2, 0.9, 0.8, duration=0.1, delay=False)
        ctrl._adb_input.assert_called_once_with(
            ['swipe', '96', '108', '864', '432', '100']
        )

    def test_scroll_routes_fractional_delta_to_protocol_injector(
        self,
        ctrl: ScrcpyController,
    ) -> None:
        ctrl._inject_scroll = MagicMock()

        ctrl.scroll(0.5, 0.6, vertical=-0.25, delay=False)

        ctrl._inject_scroll.assert_called_once_with(0.5, 0.6, horizontal=0.0, vertical=-0.25)

    def test_long_tap_sends_same_point_adb_swipe(self, ctrl: ScrcpyController):
        """long_tap = adb input swipe 同点等时长 (系统保持按压)。"""
        ctrl._adb_input = MagicMock()
        ctrl.long_tap(0.5, 0.5, duration=0.01)
        ctrl._adb_input.assert_called_once_with(
            ['swipe', '480', '270', '480', '270', '10']
        )

    def test_key_event_down_then_up(self, ctrl: ScrcpyController):
        """key_event = 同一 keycode 先 DOWN 后 UP。"""
        ctrl._inject_keycode = MagicMock()
        ctrl.key_event(4, delay=False)  # KEYCODE_BACK
        assert ctrl._inject_keycode.call_args_list == [
            call(4, action=0),  # ACTION_DOWN
            call(4, action=1),  # ACTION_UP
        ]

    def test_text_ascii_uses_inject_text(self, ctrl: ScrcpyController):
        """纯 ASCII 文本走 INJECT_TEXT 低延迟路径。"""
        ctrl._inject_text = MagicMock()
        ctrl._set_clipboard = MagicMock()
        ctrl.text('hello', delay=False)
        ctrl._inject_text.assert_called_once_with('hello')
        ctrl._set_clipboard.assert_not_called()

    def test_text_non_ascii_uses_clipboard(self, ctrl: ScrcpyController):
        """含中文等多字节字符走 SET_CLIPBOARD + paste。"""
        ctrl._inject_text = MagicMock()
        ctrl._set_clipboard = MagicMock()
        ctrl.text('你好', delay=False)
        ctrl._set_clipboard.assert_called_once_with('你好', paste=True)
        ctrl._inject_text.assert_not_called()

    def test_send_control_raises_when_disconnected(self, ctrl: ScrcpyController):
        """控制通道未连接时 _send_control 抛异常。"""
        ctrl._control_socket = None
        ctrl._alive = False
        ctrl._ensure_stream_alive = MagicMock(side_effect=EmulatorConnectionError('mock'))
        with pytest.raises(EmulatorConnectionError):
            ctrl._send_control(b'\x00')


# ═══════════════════════════════════════════════
# ScrcpyController — 截图
# ═══════════════════════════════════════════════


class TestScrcpyControllerScreenshot:
    """测试截图功能（使用 mock）。

    帧与存活状态现由 :class:`ScrcpyVideoStream` 持有，故这里构造一个**真实**
    stream 实例但不调 ``open()``（避免拉起真实 scrcpy），再手动注入帧状态。
    被测对象是``screenshot()`` 的等待/重试循环本身。
    """

    @staticmethod
    def _attach_stream(ctrl, *, alive: bool = True):
        """给 controller 挂一个未打开的 stream，并设定存活状态。"""
        stream = ScrcpyVideoStream(
            device_provider=lambda: None,
            native_resolution=ctrl._native_resolution or (960, 540),
            screenshot_timeout=ctrl._screenshot_timeout,
        )
        stream._alive = alive
        ctrl._stream = stream
        return stream

    def test_screenshot_returns_last_frame(self):
        """screenshot() 返回 stream 中的最新帧。"""
        ctrl = ScrcpyController(serial='test')
        ctrl._resolution = (4, 3)
        stream = self._attach_stream(ctrl)

        img = np.zeros((3, 4, 3), dtype=np.uint8)
        stream._last_frame = img

        result = ctrl.screenshot()
        assert result.shape == (3, 4, 3)
        assert result is img

    def test_screenshot_timeout(self):
        """截图超时应抛异常。"""
        ctrl = ScrcpyController(serial='test', screenshot_timeout=0.2)
        ctrl._resolution = (4, 3)
        stream = self._attach_stream(ctrl)
        stream._last_frame = None  # 始终无帧

        with pytest.raises(EmulatorConnectionError, match='截图超时'):
            ctrl.screenshot()

    def test_screenshot_retry_on_initial_none(self):
        """首次返回 None 后重试成功。"""
        ctrl = ScrcpyController(serial='test', screenshot_timeout=5.0)
        ctrl._resolution = (2, 2)
        stream = self._attach_stream(ctrl)

        img = np.zeros((2, 2, 3), dtype=np.uint8)

        # 直接测试逻辑：先 None 后成功
        stream._last_frame = None
        # 在 screenshot() 循环中手动注入帧
        import threading

        def _inject_frame():
            time.sleep(0.05)
            stream._last_frame = img

        threading.Thread(target=_inject_frame, daemon=True).start()
        result = ctrl.screenshot()
        assert result.shape == (2, 2, 3)
        assert result is img

    def test_screenshot_requires_connected_stream(self):
        """未 connect() 就截图必须报连接错误，而不是 AttributeError。"""
        ctrl = ScrcpyController(serial='test')
        with pytest.raises(EmulatorConnectionError, match='视频流未初始化'):
            ctrl.screenshot()
