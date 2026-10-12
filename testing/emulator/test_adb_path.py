"""adb 路径解析的回归测试。

背景（2026-10-12 实机发现的既有缺陷）
----------------------------------------
``_resolve_adb_path()`` 曾经只用 ``shutil.which('adb')``，而 Windows 上 adb
通常不在 PATH 里 →恒返回 ``None`` → ``_adb_path`` 一直为空 → 常驻 adb shell
通道恒不可用 → 触控长期落在最慢的**单次** ``adb input``（约 70ms/次），
而不是设计目标的三级链第二级（约 1ms）。

实机证据：probe 基线里 ``touch_chain.persistent_shell`` 一直是 ``false``；
修复后变成 ``true``。

这些用例锁住三级来源的**优先级**与**失败降级**。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from autowsgr.emulator.controller.scrcpy import ScrcpyController


@pytest.fixture
def ctrl() -> ScrcpyController:
    return ScrcpyController(serial='s')


def _patch_which(monkeypatch, value):
    import shutil

    monkeypatch.setattr(shutil, 'which', lambda name: value)


def _patch_find_adb(monkeypatch, value):
    """把 ``_find_adb`` 替换为「调用后返回 value」或「调用即抛 value」。"""
    import autowsgr.emulator.detector as D

    if isinstance(value, BaseException) or callable(value):
        monkeypatch.setattr(D, '_find_adb', value)
    else:
        monkeypatch.setattr(D, '_find_adb', lambda: value)


# ══════════════════════════════════════════════
# 三级来源优先级
# ══════════════════════════════════════════════


class TestAdbPathPriority:
    def test_cached_path_wins_over_everything(self, ctrl, monkeypatch) -> None:
        """已缓存（MuMu 安装目录）时不再重新探测，避免覆盖更精确的路径。"""
        ctrl._adb_path = r'C:\Program Files\Netease\MuMu\nx_main\adb.exe'
        _patch_which(monkeypatch, r'D:\other\adb.exe')
        _patch_find_adb(monkeypatch, r'E:\third\adb.exe')

        assert ctrl._resolve_adb_path() == r'C:\Program Files\Netease\MuMu\nx_main\adb.exe'

    def test_path_used_when_available(self, ctrl, monkeypatch) -> None:
        ctrl._adb_path = None
        _patch_which(monkeypatch, r'D:\system\adb.exe')

        assert ctrl._resolve_adb_path() == r'D:\system\adb.exe'

    def test_falls_back_to_detector_when_path_empty(self, ctrl, monkeypatch) -> None:
        """**核心回归**：PATH 里没有 adb 时必须走 _find_adb（注册表/模拟器目录）。"""
        ctrl._adb_path = None
        _patch_which(monkeypatch, None)
        _patch_find_adb(monkeypatch, r'C:\MuMu\nx_main\adb.exe')

        assert ctrl._resolve_adb_path() == r'C:\MuMu\nx_main\adb.exe'

    def test_detector_failure_returns_none_not_raises(self, ctrl, monkeypatch) -> None:
        """找不到 adb 不该阻断 connect()（只是退回慢速通道）。"""
        ctrl._adb_path = None
        _patch_which(monkeypatch, None)

        def boom() -> str:
            raise FileNotFoundError('未找到 adb')

        _patch_find_adb(monkeypatch, boom)

        assert ctrl._resolve_adb_path() is None

    def test_registry_probe_is_actually_attempted(self, ctrl, monkeypatch) -> None:
        """确保真的调用了_ find_adb，而不是碰巧返回缓存值。"""
        ctrl._adb_path = None
        _patch_which(monkeypatch, None)
        calls: list[int] = []

        def tracked() -> str:
            calls.append(1)
            return r'C:\MuMu\nx_main\adb.exe'

        _patch_find_adb(monkeypatch, tracked)
        ctrl._resolve_adb_path()

        assert calls == [1], 'PATH 为空时必须尝试 _find_adb'


# ══════════════════════════════════════════════
# 常驻 shell 通道可用性（缺陷的直接后果）
# ══════════════════════════════════════════════


class TestPersistentShellWiring:
    def test_connect_attaches_shell_when_adb_found(self, monkeypatch) -> None:
        """能找到 adb 时常驻 shell 必须挂上（缺陷修复的验收点）。"""
        import autowsgr.emulator.controller.nemu_ipc as NI
        from autowsgr.emulator.controller import scrcpy as S

        shell = MagicMock()
        monkeypatch.setattr(
            S.AdbDeviceConnector,
            'connect',
            staticmethod(lambda serial: MagicMock(serial='s', window_size=lambda: (960, 540))),
        )
        monkeypatch.setattr(S.ScrcpyVideoStream, 'open', lambda self: None)
        # nemu 不可用 → _adb_path 只能靠 _resolve_adb_path 的第2/3 级
        monkeypatch.setattr(NI, 'detect_mumu_folder', lambda: None)
        monkeypatch.setattr(NI, 'PersistentAdbShell', lambda p, s: shell)
        monkeypatch.setattr(
            'autowsgr.emulator.controller.maatouch.MaaTouchController',
            MagicMock(side_effect=OSError('no maatouch')),
        )
        _patch_which(monkeypatch, None)
        _patch_find_adb(monkeypatch, r'C:\MuMu\nx_main\adb.exe')

        c = S.ScrcpyController(serial='s')
        c.connect()

        assert c._touch.persistent_shell is shell
        assert c._adb_path == r'C:\MuMu\nx_main\adb.exe'

    def test_no_adb_leaves_shell_unattached(self, monkeypatch) -> None:
        """找不到 adb 时不该硬造通道（宁可慢速也不假装快）。"""
        import autowsgr.emulator.controller.nemu_ipc as NI
        from autowsgr.emulator.controller import scrcpy as S

        monkeypatch.setattr(
            S.AdbDeviceConnector,
            'connect',
            staticmethod(lambda serial: MagicMock(serial='s', window_size=lambda: (960, 540))),
        )
        monkeypatch.setattr(S.ScrcpyVideoStream, 'open', lambda self: None)
        monkeypatch.setattr(NI, 'detect_mumu_folder', lambda: None)
        monkeypatch.setattr(
            'autowsgr.emulator.controller.maatouch.MaaTouchController',
            MagicMock(side_effect=OSError('no maatouch')),
        )
        _patch_which(monkeypatch, None)

        def boom() -> str:
            raise FileNotFoundError('no adb')

        _patch_find_adb(monkeypatch, boom)

        c = S.ScrcpyController(serial='s')
        c.connect()

        assert c._touch.persistent_shell is None

    def test_nemu_folder_overrides_adb_path(self, monkeypatch, tmp_path) -> None:
        """MuMu 自带的 adb.exe 优先于 PATH/注册表里的其它版本。"""
        import autowsgr.emulator.controller.nemu_ipc as NI
        from autowsgr.emulator.controller import scrcpy as S

        nx = tmp_path / 'nx_main'
        nx.mkdir()
        bundled_adb = nx / 'adb.exe'
        bundled_adb.write_text('')

        monkeypatch.setattr(
            S.AdbDeviceConnector,
            'connect',
            staticmethod(lambda serial: MagicMock(serial='s', window_size=lambda: (960, 540))),
        )
        monkeypatch.setattr(S.ScrcpyVideoStream, 'open', lambda self: None)
        monkeypatch.setattr(NI, 'detect_mumu_folder', lambda: str(tmp_path))
        monkeypatch.setattr(
            NI, 'NemuIpcCapture', MagicMock(side_effect=OSError('no ipc'))
        )
        monkeypatch.setattr(NI, 'PersistentAdbShell', lambda p, s: MagicMock())
        monkeypatch.setattr(
            'autowsgr.emulator.controller.maatouch.MaaTouchController',
            MagicMock(side_effect=OSError('no maatouch')),
        )
        _patch_which(monkeypatch, r'D:\some\other\adb.exe')

        c = S.ScrcpyController(serial='s')
        c.connect()

        assert c._adb_path == str(bundled_adb)