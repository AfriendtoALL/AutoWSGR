"""EVENT_MAP_PAGE 终态识别签名 — 关卡详情弹层态回归测试。

背景 (实机 2026-10-08, H5「最强之舟」困难 ACD 三连):
  最终节点 D 夜战结算完毕后, 游戏自动回港并直接跳回上次选关的关卡详情
  弹层 (Ex-11「四面合围(困难)」, 右下角"出击准备"按钮)。弹层的暗化遮罩
  盖住顶部活动标题 → 旧签名 (仅活动标题图) 识别失败 → 引擎等待
  ``['PROCEED','EVENT_MAP_PAGE','FLAGSHIP_SEVERE_DAMAGE']`` 超时 7.5s,
  错误恢复 (sleep 3s + 复检) 仍失败 → 该轮被记 SL (节点数=2), 共浪费
  约 18s。游戏内三场夜战实际全部打完, 仅引擎结果误标。

修复: EVENT_MAP_PAGE 签名扩展为 "活动标题 OR 出击准备按钮" (与
``BaseEventPage.is_current_page`` 的锚点逻辑一致 — 页面识别器早已把
弹层态算作活动页), 引擎在弹层态立即判定回港结束, 零额外等待。

二次回归 (同日): 出征瞬间出征准备页淡出, 底下弹层短暂露出 1~2 帧,
按钮锚点同样命中 → 引擎 (START_FIGHT 候选含 EVENT_MAP_PAGE) 刚启动
210ms 就误判回港, 游戏实际已出征 → 后续导航死循环。故
``wait_for_phase`` 增加 ``allow_event_overlay_anchor`` 开关, 引擎在
START_FIGHT 阶段关闭按钮锚点 (仅标题), 战斗流程后段保持开启。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import numpy as np
import pytest

from autowsgr.combat.recognizer import (
    CombatRecognitionTimeoutError,
    CombatRecognizer,
    PHASE_SIGNATURES,
    _get_event_map_templates,
    _is_event_overlay_button,
)
from autowsgr.combat.state import CombatPhase

if TYPE_CHECKING:
    import numpy.typing as npt


_SCREEN = np.zeros((540, 960, 3), dtype=np.uint8)


def _template_names(templates) -> set[str]:
    return {t.name for t in templates}


def test_combat_layer_does_not_import_ui_private_helpers() -> None:
    """combat 不得从 ui 层 import **私有模板函数** (分层铁律, T3-2)。

    迁移前 ``recognizer._get_event_map_templates`` 直接 import
    ``ui.event.event_page`` 的两个**下划线私有函数**。模板数据本就归
    ``data/event/*.yaml`` 所有, 现改为读数据层。

    .. note::
       本项只查「私有符号」。``handlers.py`` 目前仍 import
       ``ui.utils.wait_leave_page`` (公共工具), 该项在 T3-2 步骤 4 下沉,
       届时再加全面检查。
    """
    import ast
    from pathlib import Path

    import autowsgr.combat as combat_pkg

    offenders: list[str] = []
    for py in Path(combat_pkg.__file__).parent.glob('*.py'):
        tree = ast.parse(py.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ''
            if not module.startswith('autowsgr.ui'):
                continue
            for alias in node.names:
                # 只认下划线开头的**私有符号**(与 PEP 8 一致)
                if alias.name.startswith('_'):
                    offenders.append(
                        f'{py.name}:{node.lineno}: from {module} import {alias.name}'
                    )

    assert not offenders, (
        'combat 层不应从 ui 导入私有符号 (应读 data.event_config):\n'
        + '\n'.join(offenders)
    )


def test_event_map_templates_read_from_data_layer() -> None:
    """合并后的模板集合应与 data 层一致 (单一事实来源)。"""
    from autowsgr.combat.recognizer import _get_event_map_templates
    from autowsgr.data.event_config import (
        event_title_templates,
        fight_button_templates,
    )

    merged = _get_event_map_templates()
    expected = [*event_title_templates(), *fight_button_templates()]

    assert len(merged) == len(expected)
    assert {t.name for t in merged} == {t.name for t in expected}


class TestEventMapSignature:
    def test_signature_includes_fight_button_templates(self):
        """签名模板 = 活动标题图 + 出击准备按钮 (弹层态锚点)。"""
        from autowsgr.ui.event.event_page import (
            _get_event_title_templates,
            _get_fight_button_templates,
        )

        sig = PHASE_SIGNATURES[CombatPhase.EVENT_MAP_PAGE]
        assert sig.image_templates is not None
        assert _template_names(_get_event_title_templates()) <= _template_names(
            sig.image_templates
        )
        assert _template_names(_get_fight_button_templates()) <= _template_names(
            sig.image_templates
        )

    def test_helper_returns_merged_list(self):
        """_get_event_map_templates 返回标题与按钮模板的合并列表。"""
        from autowsgr.ui.event.event_page import (
            _get_event_title_templates,
            _get_fight_button_templates,
        )

        merged = _get_event_map_templates()
        assert len(merged) == len(_get_event_title_templates()) + len(
            _get_fight_button_templates()
        )

    def test_overlay_button_predicate(self):
        """_is_event_overlay_button 仅识别出击按钮模板 (按 name 前缀)。"""
        from autowsgr.ui.event.event_page import (
            _get_event_title_templates,
            _get_fight_button_templates,
        )

        assert all(_is_event_overlay_button(t) for t in _get_fight_button_templates())
        assert not any(_is_event_overlay_button(t) for t in _get_event_title_templates())


class TestEventMapOverlayMatch:
    """弹层态 (标题被遮罩盖住, 仅出击按钮可见) 行为验证。"""

    @pytest.fixture(autouse=True)
    def _real_templates(self):
        """确保签名模板已加载 (image_templates 在模块导入时已求值)。"""
        assert _get_event_map_templates(), '活动地图模板加载失败'

    @staticmethod
    def _patch_find_any(monkeypatch, hit_names: set[str]):
        """伪造 ImageChecker.find_any: 仅当命中模板名在 *hit_names* 中才成功。"""

        def fake_find_any(screen, templates, confidence):
            for t in templates:
                if t.name in hit_names:
                    return t
            return None

        from autowsgr.combat import recognizer as rec

        monkeypatch.setattr(rec.ImageChecker, 'find_any', staticmethod(fake_find_any))

    def test_overlay_frame_matches_via_fight_button(self, monkeypatch):
        """弹层态: 标题全部不可见, 出击按钮可见 → EVENT_MAP_PAGE 可匹配。

        实机场景: D 点回港落在 Ex-11 详情弹层, 标题被暗化遮罩盖住。
        """
        from autowsgr.ui.event.event_page import _get_fight_button_templates

        self._patch_find_any(
            monkeypatch, _template_names(_get_fight_button_templates())
        )
        sig = PHASE_SIGNATURES[CombatPhase.EVENT_MAP_PAGE]
        screen: npt.NDArray[np.uint8] = _SCREEN
        assert CombatRecognizer._match_phase(screen, sig) is True

    def test_clean_frame_matches_via_title(self, monkeypatch):
        """干净态: 标题可见, 出击按钮不可见 → 仍可匹配 (原行为保持)。"""
        from autowsgr.ui.event.event_page import _get_event_title_templates

        self._patch_find_any(monkeypatch, _template_names(_get_event_title_templates()))
        sig = PHASE_SIGNATURES[CombatPhase.EVENT_MAP_PAGE]
        screen: npt.NDArray[np.uint8] = _SCREEN
        assert CombatRecognizer._match_phase(screen, sig) is True

    def test_neither_anchor_visible_no_match(self, monkeypatch):
        """标题与按钮都不可见 (非活动页) → 不匹配。"""
        self._patch_find_any(monkeypatch, set())
        sig = PHASE_SIGNATURES[CombatPhase.EVENT_MAP_PAGE]
        screen: npt.NDArray[np.uint8] = _SCREEN
        assert CombatRecognizer._match_phase(screen, sig) is False


class TestWaitForPhaseAnchorGate:
    """wait_for_phase 的 allow_event_overlay_anchor 门控 (出征过渡回归)。"""

    @staticmethod
    def _make_recognizer(monkeypatch, hit_names: set[str]) -> CombatRecognizer:
        """构造识别器: 截图返回黑帧, find_any 按模板名分派。"""
        from autowsgr.combat import recognizer as rec

        def fake_find_any(screen, templates, confidence):
            for t in templates:
                if t.name in hit_names:
                    return t
            return None

        monkeypatch.setattr(rec.ImageChecker, 'find_any', staticmethod(fake_find_any))
        ctx = MagicMock()
        ctx.stop_event.is_set.return_value = False
        device = MagicMock()
        device.screenshot.return_value = _SCREEN
        ctx.ctrl = device
        return CombatRecognizer(ctx)

    def test_start_fight_stage_ignores_overlay_button(self, monkeypatch):
        """出征过渡 (allow=False): 弹层按钮命中也不判 EVENT_MAP_PAGE → 超时。

        实机事故: 引擎启动瞬间 (画面=弹层过渡帧) 按钮锚点命中 → 210ms
        误判回港 success(节点数=0), 游戏实际已出征。修复后该帧不被接受。
        """
        from autowsgr.ui.event.event_page import _get_fight_button_templates

        rec = self._make_recognizer(
            monkeypatch, _template_names(_get_fight_button_templates())
        )
        # 只跑一轮匹配后即超时: 首次 time.time()=0 (建 deadline), 之后 100s
        state = {'ticked': False}

        def fake_time():
            if not state['ticked']:
                state['ticked'] = True
                return 0.0
            return 100.0

        from autowsgr.combat import recognizer as rec_mod

        monkeypatch.setattr(rec_mod.time, 'time', fake_time)
        with pytest.raises(CombatRecognitionTimeoutError):
            rec.wait_for_phase(
                [CombatPhase.EVENT_MAP_PAGE],
                allow_event_overlay_anchor=False,
            )

    def test_later_stage_accepts_overlay_button(self, monkeypatch):
        """战斗后段 (allow=True 默认): 弹层按钮命中 → 判 EVENT_MAP_PAGE。

        D 点回港场景: 弹层出现即判定战斗结束。
        """
        from autowsgr.ui.event.event_page import _get_fight_button_templates

        rec = self._make_recognizer(
            monkeypatch, _template_names(_get_fight_button_templates())
        )
        result = rec.wait_for_phase(
            [CombatPhase.EVENT_MAP_PAGE],
            allow_event_overlay_anchor=True,
        )
        assert result is CombatPhase.EVENT_MAP_PAGE

    def test_start_fight_stage_still_matches_title(self, monkeypatch):
        """出征过渡 (allow=False): 标题锚点仍然有效 (干净活动页不受影响)。"""
        from autowsgr.ui.event.event_page import _get_event_title_templates

        rec = self._make_recognizer(
            monkeypatch, _template_names(_get_event_title_templates())
        )
        result = rec.wait_for_phase(
            [CombatPhase.EVENT_MAP_PAGE],
            allow_event_overlay_anchor=False,
        )
        assert result is CombatPhase.EVENT_MAP_PAGE
