"""ADB 设备连接管理 — serial 解析、重试接入、adb server 重启。

从 :class:`ScrcpyController` 中剥离的「连接编排」层。它只回答两个问题：

1. **连哪台设备？** —— serial 解析（显式 → 配置 → 自动检测 → 交互选择）
2. **怎么连上？** —— ``adb connect``（TCP 设备）+ adbutils 取句柄，
   失败则重启 adb server 后重试，最多 3 次

这两件事与视频流、触控、截图都无关，因此单独成模块便于脱离真机测试：
注入 :class:`SerialResolver` 即可在没有设备的机器上覆盖全部解析分支。

失败策略
--------
adb server 会被其它进程杀掉（本项目实测：每次跨bash 调用都可能触发，
见 :mod:`autowsgr.emulator.detector`），表现为``EmulatorConnectionError``。
对策是**第 2 次起先 ``kill-server`` + ``start-server`` 再等 2s 重试**，
因为这类失败重启 server 通常能恢复，而重复同一动作不会。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from autowsgr.infra import EmulatorConnectionError
from autowsgr.infra.logger import get_logger

if TYPE_CHECKING:
    from adbutils import AdbDevice

_log = get_logger('emulator')

MAX_CONNECT_ATTEMPTS = 3
"""连接重试次数（含首次）。"""


class SerialResolver:
    """确定要连接的设备 serial。

    优先级（从高到低）：

    1. ``explicit`` —— 调用方显式指定（usersettings.yaml 的 serial）
    2. ``config`` —— 从 :class:`EmulatorConfig` 解析
    3. 自动检测 —— 恰好一台时直接用；零台则留空（后续连接自然失败并报错）
    4. 交互选择 —— 多台时弹选择器

    Parameters
    ----------
    explicit:
        显式指定的 serial。
    config:
        配置文件；仅在 ``explicit`` 为空时使用。
    detect_emulators, resolve_serial, prompt_user_select:
        检测/解析/选择函数，**默认惰性导入** :mod:`autowsgr.emulator.detector`。
        抽出成构造参数是为了测试时能替换，避免依赖真实环境探测。
    """

    def __init__(
        self,
        explicit: str | None = None,
        config: object | None = None,
        *,
        detect_emulators=None,
        resolve_serial=None,
        prompt_user_select=None,
    ) -> None:
        self._explicit = explicit
        self._config = config
        self._detect = detect_emulators
        self._resolve = resolve_serial
        self._prompt = prompt_user_select

    def _detect_fn(self):
        if self._detect is not None:
            return self._detect
        from ..detector import detect_emulators as fn

        return fn

    def _resolve_fn(self):
        if self._resolve is not None:
            return self._resolve
        from ..detector import resolve_serial as fn

        return fn

    def _prompt_fn(self):
        if self._prompt is not None:
            return self._prompt
        from ..detector import prompt_user_select as fn

        return fn

    def resolve(self) -> str:
        """返回目标 serial；``''`` 表示未指定具体设备。"""
        if self._explicit:
            return self._explicit
        if self._config is not None:
            return self._resolve_fn()(self._config)

        candidates = self._detect_fn()()
        if len(candidates) == 1:
            _log.info('[Emulator] 自动检测到唯一设备: {}', candidates[0].description)
            return candidates[0].serial
        if len(candidates) == 0:
            return ''
        return self._prompt_fn()(candidates)


class AdbDeviceConnector:
    """按 serial 接入 adbutils 设备，失败则重启 adb server 重试。"""

    @staticmethod
    def connect(serial: str | None, *, restart_server=None) -> AdbDevice:
        """连接并返回 adbutils 设备句柄。

        Parameters
        ----------
        serial:
            目标 serial；``None``/``''`` 时取设备列表的第一个。
        restart_server:
            重启 adb server 的回调，默认为 :func:`restart_adb_server`。
            抽出成参数是为了测试时避免真的杀 adb server。

        Raises
        ------
        EmulatorConnectionError
            重试耗尽仍失败。
        """
        import adbutils

        if restart_server is None:
            restart_server = globals()['restart_adb_server']

        last_exc: Exception | None = None
        for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    _log.info('[Emulator] 重试连接 ({}/{})...', attempt, MAX_CONNECT_ATTEMPTS)
                    restart_server()
                    time.sleep(2.0)
                return _try_connect(adbutils, serial)
            except Exception as exc:
                last_exc = exc
                _log.warning(
                    '[Emulator] 连接失败 (尝试 {}/{}): {}', attempt, MAX_CONNECT_ATTEMPTS, exc
                )

        raise EmulatorConnectionError(
            f'连接设备失败（共尝试 {MAX_CONNECT_ATTEMPTS} 次）: {serial}'
        ) from last_exc


def _try_connect(adbutils_module, serial: str | None) -> AdbDevice:
    """单次接入尝试。"""
    if serial:
        # TCP 地址（如 127.0.0.1:16416）需先 adb connect
        if ':' in serial:
            adbutils_module.adb.connect(serial, timeout=5.0)
        return adbutils_module.adb.device(serial=serial)
    devices = adbutils_module.adb.device_list()
    if not devices:
        raise EmulatorConnectionError('未发现已连接的 ADB 设备')
    return devices[0]


def restart_adb_server() -> None:
    """重启 adb server（失败仅记 debug 日志，不向上抛）。"""
    import subprocess

    from ..detector import _find_adb

    try:
        adb = _find_adb()
        subprocess.run([adb, 'kill-server'], timeout=5, capture_output=True, check=False)  # noqa: S603
        time.sleep(1)
        subprocess.run([adb, 'start-server'], timeout=8, capture_output=True, check=False)  # noqa: S603
    except Exception as exc:
        _log.debug('[Emulator] 重启 adb server 失败: {}', exc)