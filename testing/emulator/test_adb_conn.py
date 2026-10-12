"""ADB 连接编排（serial 解析 + 重试接入）的无设备单元测试。

测试策略：把 ``adbutils`` 模块整体替换为假对象、把重试休眠缩短，
因此不依赖真机也能覆盖全部失败分支。**不 mock 掉重试逻辑本身**——
「失败几次后放弃」「第 2 次起才重启 server」这些都是必须锁住的行为。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autowsgr.emulator.controller.adb_conn import (
    MAX_CONNECT_ATTEMPTS,
    AdbDeviceConnector,
    SerialResolver,
    _try_connect,
)
from autowsgr.infra import EmulatorConnectionError


@pytest.fixture
def fake_adbutils(monkeypatch):
    """注入假的 adbutils 模块（属性级可控）。"""
    mod = SimpleNamespace(adb=MagicMock())
    monkeypatch.setitem(sys.modules, 'adbutils', mod)
    return mod


def _cand(serial: str, desc: str = 'd'):
    return SimpleNamespace(serial=serial, description=desc)


# ══════════════════════════════════════════════
# SerialResolver
# ══════════════════════════════════════════════


class TestSerialResolver:
    def test_explicit_wins(self) -> None:
        r = SerialResolver(
            '127.0.0.1:16416',
            object(),
            detect_emulators=lambda: [_cand('other')],
            resolve_serial=lambda c: 'from-config',
            prompt_user_select=lambda c: 'from-prompt',
        )
        assert r.resolve() == '127.0.0.1:16416'

    def test_config_used_when_no_explicit(self) -> None:
        r = SerialResolver(
            None,
            'CFG',
            detect_emulators=lambda: [_cand('detected')],
            resolve_serial=lambda c: f'from-{c}',
            prompt_user_select=lambda c: 'p',
        )
        assert r.resolve() == 'from-CFG'

    def test_single_detected_device_used_automatically(self) -> None:
        r = SerialResolver(None, None, detect_emulators=lambda: [_cand('only-one')])
        assert r.resolve() == 'only-one'

    def test_zero_devices_returns_empty(self) -> None:
        """零台设备不报错，交由连接层给出明确失败。"""
        r = SerialResolver(None, None, detect_emulators=lambda: [])
        assert r.resolve() == ''

    def test_multiple_devices_prompts_user(self) -> None:
        picked = []
        r = SerialResolver(
            None,
            None,
            detect_emulators=lambda: [_cand('a'), _cand('b')],
            prompt_user_select=lambda c: picked.append(c) or 'chosen',
        )
        assert r.resolve() == 'chosen'
        assert len(picked[0]) == 2

    def test_never_probes_real_environment_when_injected(self) -> None:
        """注入了检测函数时不应再去 import 真的 detector 探测环境。"""
        r = SerialResolver(None, None, detect_emulators=lambda: [_cand('x')])
        assert r.resolve() == 'x'

    def test_lazy_default_imports_exist(self) -> None:
        """未注入时走惰性导入路径（真实 detector 存在即可，不实际探测）。"""
        r = SerialResolver('explicit-serial', None)
        assert r.resolve() == 'explicit-serial'


# ══════════════════════════════════════════════
# _try_connect 单次接入
# ══════════════════════════════════════════════


class TestTryConnect:
    def test_tcp_serial_requires_adb_connect_first(self, fake_adbutils) -> None:
        dev = _try_connect(fake_adbutils, '127.0.0.1:16416')
        fake_adbutils.adb.connect.assert_called_once_with('127.0.0.1:16416', timeout=5.0)
        fake_adbutils.adb.device.assert_called_once_with(serial='127.0.0.1:16416')
        assert dev is fake_adbutils.adb.device.return_value

    def test_usb_serial_skips_adb_connect(self, fake_adbutils) -> None:
        """USB serial（含冒号的才需 connect）不能多调 adb.connect。"""
        _try_connect(fake_adbutils, 'emulator-5554')
        fake_adbutils.adb.connect.assert_not_called()

    def test_no_serial_takes_first_device(self, fake_adbutils) -> None:
        first, second = _cand('dev-a'), _cand('dev-b')
        fake_adbutils.adb.device_list.return_value = [first, second]

        dev = _try_connect(fake_adbutils, '')

        assert dev is first
        fake_adbutils.adb.connect.assert_not_called()

    def test_no_serial_and_no_devices_raises(self, fake_adbutils) -> None:
        fake_adbutils.adb.device_list.return_value = []
        with pytest.raises(EmulatorConnectionError, match='未发现已连接的 ADB 设备'):
            _try_connect(fake_adbutils, '')


# ══════════════════════════════════════════════
# AdbDeviceConnector 重试
# ══════════════════════════════════════════════


class TestConnectorRetry:
    def test_success_on_first_attempt_no_restart(self, fake_adbutils, monkeypatch) -> None:
        monkeypatch.setattr('autowsgr.emulator.controller.adb_conn.time.sleep', lambda s: None)
        restarts: list[int] = []

        dev = AdbDeviceConnector.connect('emulator-5554', restart_server=lambda: restarts.append(1))

        assert dev is fake_adbutils.adb.device.return_value
        assert restarts == [], '首次成功不该重启 adb server'

    def test_restarts_server_on_retry_then_succeeds(self, fake_adbutils, monkeypatch) -> None:
        monkeypatch.setattr('autowsgr.emulator.controller.adb_conn.time.sleep', lambda s: None)
        restarts: list[int] = []
        fake_adbutils.adb.device.side_effect = [ConnectionError('dead'), MagicMock()]

        dev = AdbDeviceConnector.connect(
            'emulator-5554', restart_server=lambda: restarts.append(1)
        )

        assert dev is not None
        assert len(restarts) == 1, '第2 次起必须重启 adb server'

    def test_gives_up_after_max_attempts(self, fake_adbutils, monkeypatch) -> None:
        monkeypatch.setattr('autowsgr.emulator.controller.adb_conn.time.sleep', lambda s: None)
        restarts: list[int] = []
        fake_adbutils.adb.device.side_effect = ConnectionError('always dead')

        with pytest.raises(EmulatorConnectionError, match=f'共尝试 {MAX_CONNECT_ATTEMPTS} 次'):
            AdbDeviceConnector.connect(
                'emulator-5554', restart_server=lambda: restarts.append(1)
            )

        assert len(restarts) == MAX_CONNECT_ATTEMPTS - 1, '首次不重启，其后每次都重启'
        assert fake_adbutils.adb.device.call_count == MAX_CONNECT_ATTEMPTS

    def test_error_message_keeps_serial_for_diagnosis(
        self, fake_adbutils, monkeypatch
    ) -> None:
        """报错必须带 serial，否则挂机日志里看不出连的是哪台。"""
        monkeypatch.setattr('autowsgr.emulator.controller.adb_conn.time.sleep', lambda s: None)
        fake_adbutils.adb.device.side_effect = ConnectionError('x')

        with pytest.raises(EmulatorConnectionError, match='127.0.0.1:16416'):
            AdbDeviceConnector.connect(
                '127.0.0.1:16416', restart_server=lambda: None
            )

    def test_preserves_original_exception_as_cause(self, fake_adbutils, monkeypatch) -> None:
        monkeypatch.setattr('autowsgr.emulator.controller.adb_conn.time.sleep', lambda s: None)
        fake_adbutils.adb.device.side_effect = ConnectionError('root cause')

        with pytest.raises(EmulatorConnectionError) as ei:
            AdbDeviceConnector.connect('s', restart_server=lambda: None)

        assert isinstance(ei.value.__cause__, ConnectionError)


# ══════════════════════════════════════════════
# ScrcpyController 接线
# ══════════════════════════════════════════════


class TestControllerWiring:
    def test_connect_backfills_serial_when_auto_selected(self, monkeypatch) -> None:
        """未指定 serial 时必须回填为实际连上的那台（历史行为）。

        把视频流 open 与加速通道初始化都 stub 掉，让 connect() 跑完整条路径
        而不是靠吞异常蒙混过关。
        """
        from autowsgr.emulator.controller import scrcpy as S

        dev = SimpleNamespace(serial='auto-detected', window_size=lambda: (960, 540))
        monkeypatch.setattr(
            S.AdbDeviceConnector, 'connect', staticmethod(lambda serial: dev)
        )
        monkeypatch.setattr(S.SerialResolver, 'resolve', lambda self: '')
        monkeypatch.setattr(S.ScrcpyVideoStream, 'open', lambda self: None)
        # nemu_ipc / MaaTouch / 常驻 shell 全部不可用，避免真实拉起通道
        import autowsgr.emulator.controller.nemu_ipc as NI

        monkeypatch.setattr(NI, 'detect_mumu_folder', lambda: None)
        monkeypatch.setattr(NI, 'PersistentAdbShell', MagicMock(side_effect=OSError('no adb')))
        monkeypatch.setattr(
            'autowsgr.emulator.controller.maatouch.MaaTouchController',
            MagicMock(side_effect=OSError('no maatouch')),
        )

        ctrl = S.ScrcpyController(serial=None)
        info = ctrl.connect()

        assert ctrl._serial == 'auto-detected'
        assert info.serial == 'auto-detected'
        assert info.resolution == (960, 540)

    def test_explicit_serial_not_overwritten(self, monkeypatch) -> None:
        """显式 serial 连成功后不得被回填覆盖。"""
        from autowsgr.emulator.controller import scrcpy as S

        dev = SimpleNamespace(serial='other-serial', window_size=lambda: (1280, 720))
        monkeypatch.setattr(
            S.AdbDeviceConnector, 'connect', staticmethod(lambda serial: dev)
        )
        monkeypatch.setattr(S.ScrcpyVideoStream, 'open', lambda self: None)
        import autowsgr.emulator.controller.nemu_ipc as NI

        monkeypatch.setattr(NI, 'detect_mumu_folder', lambda: None)
        monkeypatch.setattr(NI, 'PersistentAdbShell', MagicMock(side_effect=OSError('no adb')))
        monkeypatch.setattr(
            'autowsgr.emulator.controller.maatouch.MaaTouchController',
            MagicMock(side_effect=OSError('no maatouch')),
        )

        ctrl = S.ScrcpyController(serial='127.0.0.1:16416')
        ctrl.connect()

        assert ctrl._serial == '127.0.0.1:16416'

    def test_resolution_falls_back_when_window_size_unavailable(self, monkeypatch) -> None:
        """拿不到分辨率时必须回落到 960x540 而不是 0x0（否则坐标全错）。"""
        from autowsgr.emulator.controller import scrcpy as S

        dev = SimpleNamespace(serial='s', window_size=lambda: None)
        monkeypatch.setattr(
            S.AdbDeviceConnector, 'connect', staticmethod(lambda serial: dev)
        )
        monkeypatch.setattr(S.ScrcpyVideoStream, 'open', lambda self: None)
        import autowsgr.emulator.controller.nemu_ipc as NI

        monkeypatch.setattr(NI, 'detect_mumu_folder', lambda: None)
        monkeypatch.setattr(NI, 'PersistentAdbShell', MagicMock(side_effect=OSError('no adb')))
        monkeypatch.setattr(
            'autowsgr.emulator.controller.maatouch.MaaTouchController',
            MagicMock(side_effect=OSError('no maatouch')),
        )

        ctrl = S.ScrcpyController(serial='s')
        ctrl.connect()

        assert ctrl.resolution == (960, 540)