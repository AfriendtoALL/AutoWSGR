"""MuMu 模拟器实例管理 (CLI 封装)。

用于 L3 级恢复: 当**确认模拟器进程已退出**时, 可选地由脚本拉起实例。

⚠️ **副作用警告**
--------------
拉起模拟器是**有副作用**的操作: 占用内存/显存、可能抢占焦点窗口、可能与
用户手动操作冲突。因此本模块**不提供"默认开启"的能力** —— 是否允许脚本
拉起模拟器必须由用户显式决定 (CLI 参数 ``--auto-launch-emulator``)。

L2 级恢复 (仅重建 adb/视频流会话, 不碰任何用户进程) 见
:mod:`autowsgr.emulator.health`, 那一层是安全的、可自动执行的。

CLI 语法 (经实机 ``MuMuManager.exe --help`` 确认)
--------------------------------------------------
::

    MuMuManager.exe info -v all              # 列出所有实例 (JSON)
    MuMuManager.exe control -v <index> launch    # 启动实例
    MuMuManager.exe control -v <index> shutdown  # 关闭实例

``info -v all`` 输出为 JSON, 关键字段:

- ``adb_port``: 实例的 adb 端口 (用于和配置的 serial 比对)
- ``is_process_started``: 模拟器进程是否在运行
- ``is_android_started``: Android 是否已启动完成
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autowsgr.infra.logger import get_logger


_log = get_logger('emulator.mumu')


#: MuMu 安装目录下 CLI 的候选路径 (按优先级)
_MUMU_MANAGER_CANDIDATES = (
    Path(r'C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe'),
    Path(r'D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe'),
    Path(r'C:\Program Files\Netease\MuMuPlayer\nx_main\MuMuManager.exe'),
)


@dataclass(frozen=True)
class MuMuInstance:
    """一个 MuMu 实例的运行状态。"""

    index: str
    """实例索引 (对应 CLI 的 ``--vmindex``)。"""

    name: str
    """实例名称。"""

    adb_port: int | None
    """adb 端口; 未运行时可能为 None。"""

    process_started: bool
    """模拟器宿主进程是否在运行。"""

    android_started: bool
    """Android 是否已启动完成。"""

    @property
    def alive(self) -> bool:
        """进程是否在运行。"""
        return self.process_started

    @property
    def ready(self) -> bool:
        """是否已完全就绪 (进程 + Android 均已启动)。"""
        return self.process_started and self.android_started


def find_manager() -> Path | None:
    """定位 MuMuManager.exe。找不到返回 ``None``。"""
    for candidate in _MUMU_MANAGER_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _run_manager(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """执行 MuMuManager, 返回 (returncode, stdout, stderr)。"""
    manager = find_manager()
    if manager is None:
        return -1, '', '未找到 MuMuManager.exe'

    try:
        proc = subprocess.run(  # noqa: S603
            [str(manager), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return -1, '', str(exc)

    return (
        proc.returncode,
        proc.stdout.decode('utf-8', errors='replace'),
        proc.stderr.decode('utf-8', errors='replace'),
    )


def _instance_from_dict(index: str, item: dict) -> MuMuInstance:
    """把 ``info`` 的单条 JSON 转为 :class:`MuMuInstance`。"""
    port = item.get('adb_port')
    return MuMuInstance(
        index=index,
        name=str(item.get('name', '?')),
        adb_port=int(port) if isinstance(port, int) else None,
        process_started=bool(item.get('is_process_started', False)),
        android_started=bool(item.get('is_android_started', False)),
    )


def _parse_info(raw: str) -> dict[str, MuMuInstance]:
    """解析 ``info -v all`` 的 JSON 输出。

    输出形态不稳定 (可能夹带 ``current select player index: 0`` 等提示行),
    故先定位第一个 ``{`` 再截取。
    """
    start = raw.find('{')
    if start < 0:
        return {}
    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        _log.warning('[MuMu] 解析 info 输出失败: {}', exc)
        return {}

    if not isinstance(data, dict):
        return {}

    instances: dict[str, MuMuInstance] = {}
    for idx, item in data.items():
        if not isinstance(item, dict):
            continue
        instances[str(idx)] = _instance_from_dict(str(idx), item)
    return instances


def list_instances() -> dict[str, MuMuInstance]:
    """列出所有 MuMu 实例及其运行状态。失败返回空字典。"""
    rc, out, err = _run_manager(['info', '-v', 'all'])
    if rc != 0 and not out.strip():
        _log.warning('[MuMu] 获取实例列表失败 (rc={}): {}', rc, err.strip()[:200])
        return {}
    return _parse_info(out)


def index_for_serial(serial: str | None) -> str | None:
    """由 adb serial 反查实例索引。

    ``serial`` 形如 ``127.0.0.1:16416``, 实例的 ``adb_port`` 为 ``16416``,
    故按端口号匹配。匹配不到返回 ``None``。
    """
    if not serial or ':' not in serial:
        return None
    port = serial.rsplit(':', 1)[-1]
    if not port.isdigit():
        return None

    for idx, inst in list_instances().items():
        if inst.adb_port == int(port):
            return idx
    return None


def launch_instance(index: str, timeout: float = 120.0) -> bool:
    """启动指定实例。**有副作用**, 仅在用户显式允许时调用。

    Returns
    -------
    bool
        ``True`` = 启动命令已成功下发。
    """
    _log.warning('[MuMu] 正在拉起模拟器实例 {} (可能占用较多内存)...', index)
    rc, out, err = _run_manager(['control', '-v', str(index), 'launch'], timeout=timeout)
    if rc != 0:
        _log.error('[MuMu] 启动实例 {} 失败 (rc={}): {}', index, rc, (err or out)[:300])
        return False
    _log.info('[MuMu] 实例 {} 启动命令已下发', index)
    return True


def shutdown_instance(index: str) -> bool:
    """关闭指定实例。**有副作用**, 需显式调用。"""
    _log.warning('[MuMu] 正在关闭模拟器实例 {}', index)
    rc, out, err = _run_manager(['control', '-v', str(index), 'shutdown'])
    if rc != 0:
        _log.error('[MuMu] 关闭实例 {} 失败 (rc={}): {}', index, rc, (err or out)[:300])
        return False
    return True


def is_instance_alive(serial: str | None) -> bool | None:
    """判断 serial 对应的实例是否还活着。

    Returns
    -------
    bool | None
        ``True``/``False`` = 明确存活/已死; ``None`` = **无法判断**
        (找不到 MuMuManager / serial 格式不支持 / 实例未登记)。

    返回 ``None`` 时调用方**不应**贸然拉起模拟器 —— 那是有副作用的操作。
    """
    idx = index_for_serial(serial)
    if idx is None:
        return None
    inst = list_instances().get(idx)
    if inst is None:
        return False
    return inst.alive