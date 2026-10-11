"""加速通道 (nemu_ipc / PersistentAdbShell) 的无设备单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from autowsgr.emulator.controller.nemu_ipc import (
    NemuIpcUnavailable,
    PersistentAdbShell,
)


class TestPersistentAdbShell:
    def test_run_writes_command_line(self) -> None:
        proc = MagicMock()
        with patch(
            'autowsgr.emulator.controller.nemu_ipc.subprocess.Popen', return_value=proc
        ):
            shell = PersistentAdbShell('adb.exe', '127.0.0.1:16416')
            shell.run('input', 'tap', '480', '529')

        written = b''.join(c.args[0] for c in proc.stdin.write.call_args_list)
        assert written == b'input tap 480 529\n'

    def test_broken_pipe_reopens_and_retries(self) -> None:
        """会话断连 (BrokenPipe) → 重开一次并重写命令, 不抛异常。"""
        broken = MagicMock()
        broken.stdin.write.side_effect = BrokenPipeError
        fresh = MagicMock()
        with patch(
            'autowsgr.emulator.controller.nemu_ipc.subprocess.Popen',
            side_effect=[broken, fresh],
        ):
            shell = PersistentAdbShell('adb.exe', '127.0.0.1:16416')
            shell.run('input', 'tap', '480', '529')  # 不应抛异常

        broken.kill.assert_called_once()
        written = b''.join(c.args[0] for c in fresh.stdin.write.call_args_list)
        assert written == b'input tap 480 529\n'

    def test_close_kills_process(self) -> None:
        proc = MagicMock()
        with patch(
            'autowsgr.emulator.controller.nemu_ipc.subprocess.Popen', return_value=proc
        ):
            shell = PersistentAdbShell('adb.exe', '127.0.0.1:16416')
            shell.close()
        proc.stdin.close.assert_called_once()
        proc.wait.assert_called_once()


class TestNemuIpcGuard:
    """NemuIpcUnavailable 语义: 上层据此永久降级回 scrcpy 流。"""

    def test_unavailable_is_runtime_error(self) -> None:
        assert issubclass(NemuIpcUnavailable, RuntimeError)

    def test_detect_mumu_folder_returns_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os

        from autowsgr.emulator.controller.nemu_ipc import detect_mumu_folder

        monkeypatch.setattr(
            os.path, 'exists', lambda p: False
        )  # 所有候选路径不存在 (adbutils 反推路径也因 exists=False 落空)
        assert detect_mumu_folder() is None
