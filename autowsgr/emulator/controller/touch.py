"""触控注入通道 — 把「按下/滑动」从设备控制器中剥离。

背景
----
MuMu 12 等模拟器内置了自己的 scrcpy 协议服务（占用 ``scrcpy`` abstract socket，
且不会真正执行我们部署的 server），其实现支持 INJECT_KEYCODE 但**不支持**
INJECT_TOUCH_EVENT（v1/v2 格式均无效，实测 2026-10-08）。因此触控统一走 adb
``input`` 命令，截图（视频流）与按键（keycode）仍走 scrcpy 通道以保持低延迟。

adb ``input`` 每次调用都要重启一个进程（约 70ms），故实际使用三级降级链：

===========================  ==========================================
优先级通道                   说明
===========================  ==========================================
1. :class:`MaaTouchController`  subprocess 常驻服务，写入即注入（~0ms）
2. :class:`PersistentAdbShell`   常驻 ``adb shell``，逐行写入（~1ms）
3. 单次 ``adb shell input``      最后兜底（~70ms）
===========================  ==========================================

关键性质：**某一级失败时永久降级到下一级**（关闭该通道并置空引用），
单次调用的失败不会污染后续调用 —— 这也是本模块独立于视频流代码的原因：
触控链路的生命周期与 scrcpy socket 无关。

Usage
-----
::

    chain = TouchChannelChain(device_provider=lambda: ctrl._require_device())
    chain.attach_maatouch(maatouch)
    chain.attach_persistent_shell(shell)
    chain.inject(['tap', '480', '270'])
    chain.close()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from autowsgr.infra.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger('emulator')


@runtime_checkable
class TouchInjector(Protocol):
    """触控注入器协议。

    实现方只需提供 :meth:`inject`：把一条 ``adb shell input`` 的参数列表
    （不含 ``input`` 前缀）注入到设备。调用方不应假设具体走哪一级通道。
    """

    def inject(self, args: list[str], timeout: float = 10.0) -> None:
        """注入一条 adb ``input`` 命令（``args`` 不含 ``'input'`` 本身）。"""
        ...


class TouchChannelChain:
    """三级降级的触控注入链（MaaTouch → 常驻 shell → 单次 adb）。

    Parameters
    ----------
    device_provider:
        返回 :class:`adbutils.AdbDevice` 的可调用对象。**仅在所有通道都不可用时
        才被调用**，因此可以传一个只有真正需要兜底时才求值的 lambda
        （例如 ``lambda: ctrl._require_device()``），避免构造期就要求设备已连接。

    Notes
    -----
    降级是**单向且永久**的：某一级抛异常即 close 并清空该级引用，
    之后不再尝试。这是有意为之 —— 一个持续失败的通道每次都重试只会
    放大延迟，且掩盖真实故障点。
    """

    def __init__(self, device_provider: Callable[[], object]) -> None:
        self._device_provider = device_provider
        self._maatouch: object | None = None
        self._persistent_shell: object | None = None

    # ── 通道装配（由连接层在 connect 阶段调用） ──

    def attach_maatouch(self, maatouch: object | None) -> None:
        """挂载 MaaTouch 通道（``None`` 表示不可用）。"""
        self._maatouch = maatouch

    def attach_persistent_shell(self, shell: object | None) -> None:
        """挂载常驻 adb shell 通道（``None`` 表示不可用）。"""
        self._persistent_shell = shell

    # ── 查询 ──

    @property
    def maatouch(self) -> object | None:
        """当前 MaaTouch 通道（已降级时为 ``None``）。"""
        return self._maatouch

    @property
    def persistent_shell(self) -> object | None:
        """当前常驻 shell 通道（已降级时为 ``None``）。"""
        return self._persistent_shell

    # ── 注入 ──

    def inject(self, args: list[str], timeout: float = 10.0) -> None:
        """按优先级注入一条 adb ``input`` 命令。

        Parameters
        ----------
        args:
            ``adb shell input`` 的参数列表，**不含** ``'input'``，例如
            ``['tap', '480', '270']``。
        timeout:
            单次 adb 调用的超时（秒），仅最后一级的兜底通道使用。
        """
        if self._maatouch is not None and args[0] == 'tap':
            try:
                self._maatouch.click(int(args[1]), int(args[2]))
                return
            except Exception as exc:
                _log.warning('[Emulator] MaaTouch 持续失败, 降级 persistent/adb: {}', exc)
                self._drop_maatouch()
        if self._persistent_shell is not None:
            try:
                self._persistent_shell.run(*args)
                return
            except Exception as exc:
                _log.warning('[Emulator] 常驻 shell 持续失败, 回退单次 adb: {}', exc)
                self._drop_persistent_shell()
        dev = self._device_provider()
        try:
            dev.shell(['input', *args], timeout=timeout)
        except Exception as exc:
            _log.warning('[Emulator] adb input {} 失败: {}', args, exc)

    # ── 降级 / 释放 ──

    def _drop_maatouch(self) -> None:
        mt = self._maatouch
        self._maatouch = None
        if mt is not None:
            try:
                mt.close()
            except Exception:
                pass

    def _drop_persistent_shell(self) -> None:
        sh = self._persistent_shell
        self._persistent_shell = None
        if sh is not None:
            try:
                sh.close()
            except Exception:
                pass

    def close(self) -> None:
        """关闭全部通道（幂等）。设备断开时调用。"""
        self._drop_maatouch()
        self._drop_persistent_shell()