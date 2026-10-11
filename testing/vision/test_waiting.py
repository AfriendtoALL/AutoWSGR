"""等待条件成立 (vision.waiting) 的单元测试 + 战斗域独立性验证。

背景 (T3-2 步骤 4)
------------------
``ui/utils/navigation.py::wait_leave_page`` 被 ``combat/handlers.py``
反向 import 使用 —— 战斗域跨越到 UI 层。

实际拆开看, 它的核心循环只依赖「设备截图 + 一个 callable」,
``get_current_page`` 仅用于日志。故核心逻辑下沉到
:mod:`autowsgr.vision.waiting`, ui 层保留薄封装。

最容易在这次重构中丢掉的��容
----------------------------
``ui.utils.navigation.NavigationError`` 的构造函数会**自动保存
NavError 截图** (``save_image(screen, tag='NavError')``) —— 那是导航失败
的取证手段, 换成 ``infra`` 里同名但无此行为的异常会静默丢失证据。
故下沉版通过 ``error_factory`` 注入异常构造器。
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from autowsgr.vision.waiting import DEFAULT_WAIT_CONFIG, wait_until_leaving


# ═══════════════════════════════════════════════════════════════════════════════
# 等待逻辑
# ═══════════════════════════════════════════════════════════════════════════════


class _Ctrl:
    """可控截图的假设备: 第 n 次起返回指定 checker 结果。"""

    def __init__(self, leave_after: int) -> None:
        self.leave_after = leave_after
        self.calls = 0

    def screenshot(self) -> np.ndarray:
        self.calls += 1
        return np.full((4, 4, 3), self.calls, np.uint8)


class TestWaitUntilLeaving:
    """「等到 checker 不再成立」的核心语义。"""

    def test_returns_screen_on_first_success(self, monkeypatch) -> None:
        """第一次 checker 就为 False → 立即返回, 不 sleep。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=0)

        result = wait_until_leaving(ctrl, lambda _s: False, timeout=5.0)

        assert result is not None
        assert ctrl.calls == 1

    def test_polls_until_leaving(self, monkeypatch) -> None:
        """前两次仍在, 第三次离开 → 返回第三次截图。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=3)

        result = wait_until_leaving(ctrl, lambda screen: screen[0, 0, 0] <= 2, timeout=5.0)

        assert result is not None
        assert int(result[0, 0, 0]) == 3
        assert ctrl.calls == 3

    def test_timeout_raises(self, monkeypatch) -> None:
        """一直不离开 → 超时抛异常。"""
        from autowsgr.infra.exceptions import NavigationError

        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=999)

        with pytest.raises(NavigationError):
            wait_until_leaving(ctrl, lambda _s: True, timeout=0.0, source='A', target='B')

    def test_probe_returns_none_on_timeout(self, monkeypatch) -> None:
        """probe 模式: 超时是预期结果, 返回 None 而不抛。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=999)

        result = wait_until_leaving(ctrl, lambda _s: True, timeout=0.0, probe=True)

        assert result is None

    def test_error_factory_is_used(self, monkeypatch) -> None:
        """异常必须由注入的工厂构造 (ui 层靠它自动存截图)。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=999)

        captured: list[tuple] = []

        class _MyError(Exception):
            def __init__(self, msg: str, screen) -> None:
                super().__init__(msg)
                captured.append((msg, screen))

        def _factory(msg: str, screen) -> Exception:
            return _MyError(msg, screen)

        with pytest.raises(_MyError):
            wait_until_leaving(
                ctrl, lambda _s: True, timeout=0.0, error_factory=_factory
            )

        assert len(captured) == 1, 'error_factory 应被调用一次'
        assert captured[0][1] is not None, '异常应带上超时时的截图'

    def test_on_departed_callback_invoked(self, monkeypatch) -> None:
        """离开时应调用 on_departed (ui 层用它补页面名日志)。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=0)
        seen: list = []

        wait_until_leaving(
            ctrl,
            lambda _s: False,
            on_departed=lambda screen: seen.append(screen) or 'MAIN',
        )

        assert len(seen) == 1
        assert seen[0] is not None

    def test_on_departed_exception_is_contained(self, monkeypatch) -> None:
        """on_departed 抛异常不应影响主流程 (它只用于日志)。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        ctrl = _Ctrl(leave_after=0)

        def _boom(_screen):
            raise RuntimeError('日志失败')

        result = wait_until_leaving(ctrl, lambda _s: False, on_departed=_boom)

        assert result is not None, '日志回调异常不应影响返回结果'

    def test_default_config(self) -> None:
        assert DEFAULT_WAIT_CONFIG.timeout > 0
        assert DEFAULT_WAIT_CONFIG.interval > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 战斗域独立性 (本次重构的核心价值)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCombatLayerIsIndependent:
    """``combat`` 不应加载任何 ``ui`` 模块。"""

    def test_importing_combat_loads_no_ui(self) -> None:
        """导入整个 combat 包后, ``sys.modules`` 里不应有 autowsgr.ui.*。

        这是比静态 grep 更强的保证 —— 静态检查只能看到显式 import,
        这里能捕获**间接**导入链 (combat → ui → combat 的环等)。
        """
        for name in [m for m in sys.modules if m.startswith('autowsgr.combat')]:
            del sys.modules[name]

        import autowsgr.combat  # noqa: F401
        import autowsgr.combat.engine  # noqa: F401
        import autowsgr.combat.handlers  # noqa: F401
        import autowsgr.combat.recognizer  # noqa: F401
        import autowsgr.combat.node_tracker  # noqa: F401

        ui_mods = sorted(m for m in sys.modules if m.startswith('autowsgr.ui'))
        assert not ui_mods, f'导入 combat 却加载了 ui 模块: {ui_mods[:5]}'

    def test_no_static_import_of_ui(self) -> None:
        """静态检查: combat 包不得 import ui / ops (AST 精确判定)。"""
        import ast
        from pathlib import Path

        import autowsgr.combat as combat_pkg

        offenders: list[str] = []
        for py in Path(combat_pkg.__file__).parent.glob('*.py'):
            tree = ast.parse(py.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = (node.module or '').split('.')
                    if module[:2] in (['autowsgr', 'ui'], ['autowsgr', 'ops']):
                        offenders.append(f'{py.name}:{node.lineno}: {node.module}')
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split('.')
                        if parts[:2] in (['autowsgr', 'ui'], ['autowsgr', 'ops']):
                            offenders.append(f'{py.name}:{node.lineno}: {alias.name}')

        assert not offenders, (
            'combat 层不应依赖 ui/ops:\n' + '\n'.join(offenders)
        )

    def test_handlers_uses_vision_waiting(self) -> None:
        """handlers 应使用 vision 版等待, 而非 ui.utils.wait_leave_page。"""
        from pathlib import Path

        import autowsgr.combat as combat_pkg

        src = (Path(combat_pkg.__file__).parent / 'handlers.py').read_text(
            encoding='utf-8'
        )
        assert 'wait_until_leaving' in src, 'handlers 应调用 vision.wait_until_leaving'
        assert 'wait_leave_page' not in src, 'handlers 不应再引用 ui 的 wait_leave_page'


# ═══════════════════════════════════════════════════════════════════════════════
# ui 层薄封装行为不变
# ═══════════════════════════════════════════════════════════════════════════════


class TestUiWrapperPreservesBehavior:
    """ui 层薄封装必须保持原有语义, 尤其 NavError 截图。"""

    def test_raises_ui_navigation_error_with_screenshot(
        self, monkeypatch
    ) -> None:
        """超时抛出的应是 **ui 层** 的 NavigationError (自动存截图)。"""
        from autowsgr.ui.utils.navigation import NavigationError

        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        saved: list = []
        monkeypatch.setattr(
            'autowsgr.infra.logger.save_image',
            lambda screen, tag='': saved.append((screen, tag)),
        )

        ctrl = _Ctrl(leave_after=999)
        with pytest.raises(NavigationError):
            from autowsgr.ui.utils import wait_leave_page

            wait_leave_page(ctrl, lambda _s: True, timeout=0.0)

        assert saved, 'NavigationError 构造应触发 NavError 截图保存'
        assert saved[0][1] == 'NavError'

    def test_probe_returns_none(self, monkeypatch) -> None:
        """probe 模式行为不变。"""
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        from autowsgr.ui.utils import wait_leave_page

        ctrl = _Ctrl(leave_after=999)
        assert wait_leave_page(ctrl, lambda _s: True, timeout=0.0, probe=True) is None

    def test_success_returns_screen(self, monkeypatch) -> None:
        monkeypatch.setattr('autowsgr.vision.waiting.time.sleep', lambda _s: None)
        from autowsgr.ui.utils import wait_leave_page

        ctrl = _Ctrl(leave_after=0)
        assert wait_leave_page(ctrl, lambda _s: False, timeout=5.0) is not None