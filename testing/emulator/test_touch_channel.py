"""TouchChannelChain 三级降级链的单元测试（无设备）。

策略：在**通道边界**断言「哪一级被调用、以什么参数、按什么顺序」，
而不是断言源码字符串。任何降级逻辑的改动（去掉某级、跳过close、
改判定条件）都必须让这里的用例失败。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from autowsgr.emulator.controller.touch import TouchChannelChain


@pytest.fixture
def device() -> MagicMock:
    dev = MagicMock()
    dev.shell.return_value = ''
    return dev


@pytest.fixture
def chain(device: MagicMock) -> TouchChannelChain:
    return TouchChannelChain(device_provider=lambda: device)


# ── 单级可用时不得越级 ──


class TestSingleChannelRouting:
    def test_tap_uses_maatouch_and_skips_shell(self, chain: TouchChannelChain) -> None:
        mt = MagicMock()
        sh = MagicMock()
        chain.attach_maatouch(mt)
        chain.attach_persistent_shell(sh)

        chain.inject(['tap', '480', '270'])

        mt.click.assert_called_once_with(480, 270)
        sh.run.assert_not_called()

    def test_tap_without_maatouch_falls_to_shell(
        self, chain: TouchChannelChain, device: MagicMock
    ) -> None:
        sh = MagicMock()
        chain.attach_persistent_shell(sh)

        chain.inject(['tap', '480', '270'])

        sh.run.assert_called_once_with('tap', '480', '270')
        device.shell.assert_not_called()

    def test_no_channel_falls_to_single_adb(self, chain: TouchChannelChain, device: MagicMock) -> None:
        chain.inject(['tap', '1', '2'])

        device.shell.assert_called_once_with(['input', 'tap', '1', '2'], timeout=10.0)

    def test_timeout_is_forwarded_to_adb(self, chain: TouchChannelChain, device: MagicMock) -> None:
        chain.inject(['tap', '1', '2'], 3.5)
        device.shell.assert_called_once_with(['input', 'tap', '1', '2'], timeout=3.5)

    def test_non_tap_never_uses_maatouch(self, chain: TouchChannelChain) -> None:
        """MaaTouch 只实现了 click，swipe 必须绕过它。"""
        mt = MagicMock()
        sh = MagicMock()
        chain.attach_maatouch(mt)
        chain.attach_persistent_shell(sh)

        chain.inject(['swipe', '1', '2', '3', '4', '500'])

        mt.click.assert_not_called()
        sh.run.assert_called_once_with('swipe', '1', '2', '3', '4', '500')

    def test_device_provider_not_called_when_channel_available(self) -> None:
        """设备只在最后兜底时才取用（否则未连接构造就炸）。"""
        calls = []
        c = TouchChannelChain(device_provider=lambda: calls.append(1))
        mt = MagicMock()
        c.attach_maatouch(mt)

        c.inject(['tap', '0', '0'])

        assert calls == []


# ── 降级：失败必须永久落到下一级 ──


class TestFallback:
    def test_maatouch_failure_drops_to_shell_and_closes_maatouch(
        self, chain: TouchChannelChain
    ) -> None:
        mt = MagicMock()
        mt.click.side_effect = OSError('broken pipe')
        sh = MagicMock()
        chain.attach_maatouch(mt)
        chain.attach_persistent_shell(sh)

        chain.inject(['tap', '10', '20'])

        mt.close.assert_called_once()
        sh.run.assert_called_once_with('tap', '10', '20')
        # 永久降级：第二次不再尝试 maatouch
        chain.inject(['tap', '30', '40'])
        assert mt.click.call_count == 1
        assert sh.run.call_count == 2

    def test_shell_failure_drops_to_adb_and_closes_shell(
        self, chain: TouchChannelChain, device: MagicMock
    ) -> None:
        sh = MagicMock()
        sh.run.side_effect = OSError('eof')
        chain.attach_persistent_shell(sh)

        chain.inject(['swipe', '0', '0', '1', '1', '10'])

        sh.close.assert_called_once()
        device.shell.assert_called_once_with(
            ['input', 'swipe', '0', '0', '1', '1', '10'], timeout=10.0
        )

    def test_both_fail_reaches_adb(self, chain: TouchChannelChain, device: MagicMock) -> None:
        mt = MagicMock()
        mt.click.side_effect = RuntimeError('x')
        sh = MagicMock()
        sh.run.side_effect = RuntimeError('y')
        chain.attach_maatouch(mt)
        chain.attach_persistent_shell(sh)

        chain.inject(['tap', '5', '5'])

        mt.close.assert_called_once()
        sh.close.assert_called_once()
        device.shell.assert_called_once()

    def test_close_failure_does_not_break_degradation(self, chain: TouchChannelChain) -> None:
        """通道 close() 抛异常时仍须完成降级，否则会连锁阻断兜底。"""
        mt = MagicMock()
        mt.click.side_effect = OSError('dead')
        mt.close.side_effect = OSError('close failed')
        sh = MagicMock()
        chain.attach_maatouch(mt)
        chain.attach_persistent_shell(sh)

        chain.inject(['tap', '1', '1'])

        sh.run.assert_called_once()

    def test_adb_failure_is_swallowed(self, chain: TouchChannelChain, device: MagicMock) -> None:
        """最底层 adb 失败只记日志，不向上抛（否则战斗流程会被单次点击打断）。"""
        device.shell.side_effect = OSError('offline')
        chain.inject(['tap', '1', '1'])  # 不抛

    def test_channel_properties_reflect_degradation(self, chain: TouchChannelChain) -> None:
        mt = MagicMock()
        mt.click.side_effect = OSError('x')
        chain.attach_maatouch(mt)
        assert chain.maatouch is mt

        chain.inject(['tap', '0', '0'])

        assert chain.maatouch is None


# ── 生命周期 ──


class TestLifecycle:
    def test_close_closes_all_and_is_idempotent(self, chain: TouchChannelChain) -> None:
        mt, sh = MagicMock(), MagicMock()
        chain.attach_maatouch(mt)
        chain.attach_persistent_shell(sh)

        chain.close()
        chain.close()

        assert mt.close.call_count == 1
        assert sh.close.call_count == 1
        assert chain.maatouch is None
        assert chain.persistent_shell is None

    def test_close_on_empty_chain_is_safe(self, chain: TouchChannelChain) -> None:
        chain.close()

    def test_reattach_after_close_works(self, chain: TouchChannelChain, device: MagicMock) -> None:
        """disconnect() 后再 connect() 必须能重新挂上通道。"""
        chain.attach_maatouch(MagicMock())
        chain.close()
        chain.attach_maatouch(None)

        chain.inject(['tap', '7', '8'])
        device.shell.assert_called_once()


# ── 与 ScrcpyController 的接线 ──


class TestScrcpyWiring:
    def test_controller_delegates_to_chain(self) -> None:
        ctrl = MagicMock()
        real = TouchChannelChain(device_provider=lambda: ctrl)
        sent: list[list[str]] = []
        real.inject = lambda args, timeout=10.0: sent.append(args)  # type: ignore[method-assign]

        from autowsgr.emulator.controller.scrcpy import ScrcpyController

        c = ScrcpyController(serial='s')
        c._touch = real  # type: ignore[assignment]
        c._resolution = (960, 540)

        c.click(0.5, 0.5, delay=False)

        assert sent == [['tap', '480', '270']]

    def test_controller_exposes_touch_chain_attribute(self) -> None:
        from autowsgr.emulator.controller.scrcpy import ScrcpyController

        assert isinstance(ScrcpyController(serial='s')._touch, TouchChannelChain)


# ── 静态约束：触控链不得反向依赖 scrcpy ──


def test_touch_module_does_not_import_scrcpy() -> None:
    import ast
    import pathlib

    src = pathlib.Path(TouchChannelChain.__module__.replace('.', '/') + '.py')
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / 'autowsgr'
        / 'emulator'
        / 'controller'
        / 'touch.py'
    )
    assert src.name == path.name  # 路径推导自检
    tree = ast.parse(path.read_text(encoding='utf-8'))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or '')
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    assert not any('scrcpy' in m for m in imported), imported