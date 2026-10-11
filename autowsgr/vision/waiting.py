"""等待条件成立的通用轮询 (2026-10-11 T3-2 步骤 4)。

背景
----
``ui/utils/navigation.py::wait_leave_page`` 提供了「反复截图直到某条件不再
成立」的等待能力, 但它被 ``combat/handlers.py`` 反向 import 使用 ——
战斗域跨越到 UI 层。

实际拆开看, 它的核心循环**只依赖设备截图与一个 callable**:

    screenshot → checker(screen) → 成立则返回, 否则 sleep 重试

``get_current_page`` 仅在日志里出现(打印"到达了哪个页面")。因此核心逻辑
可以下沉到 :mod:`autowsgr.vision`, 让:

- ``combat`` 直接用 vision 版 (不碰 ui)
- ``ui`` 保留 :func:`wait_leave_page` 作为薄封装, 补上页面名日志

保持原有语义完全不变: 同样的默认超时/间隔、同样的 ``probe`` 行为、同样的
``NavigationError`` 抛出时机。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from autowsgr.infra.exceptions import NavigationError
from autowsgr.infra.logger import get_logger


_log = get_logger('vision.waiting')


@dataclass(frozen=True)
class WaitConfig:
    """等待参数 (与 ``ui.utils.navigation.NavConfig`` 保持一致默认值)。"""

    timeout: float = 5.0
    interval: float = 1.0


DEFAULT_WAIT_CONFIG = WaitConfig()


def wait_until_leaving(
    ctrl,
    checker: Callable[[object], bool],
    *,
    timeout: float = DEFAULT_WAIT_CONFIG.timeout,
    interval: float = DEFAULT_WAIT_CONFIG.interval,
    source: str = '',
    target: str = '',
    probe: bool = False,
    on_departed=None,
    error_factory: Callable[[str, object], BaseException] | None = None,
) -> object | None:
    """反复截图, 直到 ``checker(screen)`` 返回 ``False`` (已离开)。

    Parameters
    ----------
    ctrl:
        设备控制器 (需支持 ``screenshot()``)。
    checker:
        判定函数 —— ``True`` 表示"仍在原页面", ``False`` 表示"已离开"。
    timeout:
        最长等待秒数。
    interval:
        两次轮询间隔秒数。
    source / target:
        仅用于日志的方向标识。
    probe:
        探测模式 —— 超时是**预期结果之一**(如"确认已不在某页")。
        超时不抛异常、不保存截图, 返回 ``None`` 交调用方判定。
    on_departed:
        已离开时的回调 ``(screen) -> Any``, 用于由上层补充页面名日志。
        返回值被忽略。
    error_factory:
        超时时抛异常的构造器 ``(msg, screen) -> Exception``。
        默认抛 :class:`~autowsgr.infra.exceptions.NavigationError`; ui 层应传入
        自己的 ``NavigationError`` (构造时自动保存 NavError 截图)。

    Returns
    -------
    object | None
        到达新页面时的截图; ``probe=True`` 且超时则返回 ``None``。

    Raises
    ------
    NavigationError
        超时仍在原页面 (仅 ``probe=False``)。
    """
    if error_factory is None:

        def error_factory(msg: str, screen) -> BaseException:  # noqa: ARG001
            # 兜底用infra 的 NavigationError (签名 source/target/reason)。
            # ui 层会传入自己的 NavigationError —— 它构造时自动保存
            # NavError 截图, 那是导航失败的取证手段, 不能丢。
            return NavigationError(
                source or '?',
                target or '?',
                reason=f'离开超时: {msg}',
            )

    deadline = time.monotonic() + timeout
    attempt = 0
    _log.debug('[Wait] 等待离开: {} -> {} (超时 {:.1f}s)', source or '?', target or '?', timeout)

    while True:
        attempt += 1
        screen = ctrl.screenshot()

        if not checker(screen):
            detail = None
            if on_departed is not None:
                try:
                    detail = on_departed(screen)
                except Exception:  # noqa: BLE001 - 日志补充失败不应影响主流程
                    detail = None
            _log.debug(
                '[Wait] 已离开: {} -> {} (第 {} 次截图, 到达={})',
                source or '?',
                target or '?',
                attempt,
                detail or '未知',
            )
            return screen

        _log.debug('[Wait] 等待离开 #{}: 仍在 {}', attempt, source or '?')

        if time.monotonic() >= deadline:
            if probe:
                _log.debug(
                    '[Wait] 离开超时 (探测): {} -> {} ({} 次截图后仍在 {})',
                    source or '?',
                    target or '?',
                    attempt,
                    source or '?',
                )
                return None
            msg = (
                f'离开超时: {source or "?"} -> {target or "?"}, '
                f'{attempt} 次截图后仍在 {source or "?"}'
            )
            _log.error('[Wait] {}', msg)
            # 异常类型由调用方注入 —— ``ui.utils.navigation.NavigationError``
            # 构造时会自动保存 NavError 截图, 是导航失败取证的关键, 不能
            # 换成 infra 那个同名但无此行为的异常。
            raise error_factory(msg, screen)

        time.sleep(interval)