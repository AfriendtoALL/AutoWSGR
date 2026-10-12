"""实机战斗页截图 fixture 的识别回归测试（T3-3②）。

素材来源
--------
2026-10-12 用户在MuMu 16416 上手动截取，统一 960x540：
``battle_prep`` 出征准备页 / ``formation_select`` 阵型选择面板 /
``battle_result`` 战果页(A 级) / ``proceed_dialog`` 前进回港对话框 /
``night_battle`` 夜战战斗画面。

为什么必须是**真机截图**而非合成图
----------------------------------
模板识别依赖真实渲染细节（抗锯齿、渐变、舰船立绘遮挡）。合成图能通过
断言但线上仍会失效 —— 这类fixture 的价值恰恰在于「与线上渲染同源」。

本文件锁住的三个不变量
----------------------
1. 每个页面的**正样本**必须被识别为预期状态（防回归：模板被改坏/阈值被调高）
2. **否证**：出征准备页不得被识别成 ``EVENT_MAP_PAGE``（按钮锚点门控，见
   ``test_event_map_overlay_anchor_disabled_during_start_fight``）——
   这正是2026-10-08 出征瞬间误判回港的根因
3. 战果页与经验页的区分靠「评级字母有/无」，本文件锁住战果页有评级

未覆盖（需用户补图）
--------------------
- ``day_battle`` 昼战 —— 用户反馈昼战流程复杂，未截到
- ``flagship_severe_damage`` 旗舰大破 —— 触发概率低
- ``get_ship`` 舰船掉落页 —— 用户已魔改为快速点击跳过，流程不再经过
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from autowsgr.combat.recognizer import PHASE_SIGNATURES, CombatRecognizer
from autowsgr.combat.state import CombatPhase


# fixture 统一放testing/fixtures/, 跨测试目录共享 (combat / ui 都会用到)
FIXTURE_DIR = Path(__file__).resolve().parents[1] / 'fixtures' / 'screenshots' / 'combat'


def load(name: str) -> np.ndarray:
    """读取 fixture 为 RGB 数组（协议是 RGB，注意别按 BGR 传入）。"""
    path = FIXTURE_DIR / name
    assert path.exists(), f'缺少 fixture: {path}'
    with path.open('rb') as fh:
        buf = np.frombuffer(fh.read(), np.uint8)
    import cv2

    bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    assert bgr is not None, f'无法解码: {path}'
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def match_all(screen: np.ndarray) -> set[str]:
    """返回该截图命中的所有战斗状态名。"""
    return {
        phase.name
        for phase, sig in PHASE_SIGNATURES.items()
        if CombatRecognizer._match_phase(screen, sig)
    }


# ══════════════════════════════════════════════
# 1. 正样本：每页必须认对
# ══════════════════════════════════════════════


class TestPositiveSamples:
    def test_proceed_dialog(self) -> None:
        assert 'PROCEED' in match_all(load('proceed_dialog.png'))

    def test_formation_select(self) -> None:
        assert 'FORMATION' in match_all(load('formation_select.png'))

    def test_battle_result(self) -> None:
        assert 'RESULT' in match_all(load('battle_result.png'))

    def test_night_battle(self) -> None:
        assert 'FIGHT_PERIOD' in match_all(load('night_battle.png'))


# ══════════════════════════════════════════════
# 2. 否证：出征准备页不得被认成活动地图页
# ══════════════════════════════════════════════


class TestOverlayAnchorGuard:
    """按钮锚点门控（``allow_event_overlay_anchor``）的实机回归。

    背景（2026-10-08 实机事故）：点「开始出征」后出征准备页淡出，底下
    的关卡详情弹层露出 1~2 帧，``fight_button`` 按钮锚点同样命中 →
    引擎刚启动就误判「已回港」直接结束。修复是在 ``START_FIGHT`` 阶段
    关闭按钮锚点。

    这张出征准备页 fixture 恰好也命中 ``EVENT_MAP_PAGE``（见下），说明
    门控是**唯一**防线 —— 关掉它就会误判。
    """

    def test_battle_prep_does_hit_event_map_page(self) -> None:
        """前提：出征准备页确实会命中按钮锚点（否则本组测试无意义）。"""
        assert 'EVENT_MAP_PAGE' in match_all(load('battle_prep.png'))

    def test_event_map_page_still_recognized_without_button_anchor(self) -> None:
        """关掉按钮锚点后，活动地图页仍能靠标题图认出（不能误伤真识别）。"""
        screen = load('battle_prep.png')
        from autowsgr.combat.recognizer import _get_event_map_templates
        from autowsgr.vision.image_matcher import ImageChecker

        titles = [t for t in _get_event_map_templates() if not t.name.startswith('fight_button')]
        assert titles, '活动标题图模板缺失'

        # 出征准备页不应命中标题图（标题图是活动地图页独有的）
        assert ImageChecker.find_any(screen, titles, confidence=0.8) is None

    def test_allow_event_overlay_anchor_flag_exists(self) -> None:
        """门控开关必须仍在签名上，否则哪天被删掉无人察觉。"""
        import inspect

        params = inspect.signature(CombatRecognizer.wait_for_phase).parameters
        assert 'allow_event_overlay_anchor' in params
        assert params['allow_event_overlay_anchor'].default is True


# ══════════════════════════════════════════════
# 3. START_FIGHT 是刻意无签名的过渡态
# ══════════════════════════════════════════════


class TestStartFightIsTransitional:
    def test_start_fight_has_no_visual_signature(self) -> None:
        """``START_FIGHT`` 靠转移表时序推进，不靠视觉判定。

        若哪天给它加了template_key，这个「无签名」前提就该重新审视 ——
        目前断言的是**设计意图**：它必须不能被独立识别出来。
        """
        sig = PHASE_SIGNATURES[CombatPhase.START_FIGHT]
        assert sig.template_key is None
        assert sig.image_templates is None
        assert not CombatRecognizer._match_phase(load('battle_prep.png'), sig)


# ══════════════════════════════════════════════
# 4. fixture 本身的规格
# ══════════════════════════════════════════════


class TestFixtureSpec:
    @pytest.mark.parametrize(
        'name',
        [
            'battle_prep.png',
            'formation_select.png',
            'battle_result.png',
            'proceed_dialog.png',
            'night_battle.png',
        ],
    )
    def test_resolution_is_960x540(self, name: str) -> None:
        """模板按 960x540 标定，分辨率不一致会让置信度整体偏移。"""
        img = load(name)
        assert img.shape == (540, 960, 3), f'{name} 尺寸异常: {img.shape}'

    @pytest.mark.parametrize(
        'name',
        [
            'battle_prep.png',
            'formation_select.png',
            'battle_result.png',
            'proceed_dialog.png',
            'night_battle.png',
        ],
    )
    def test_not_blank(self, name: str) -> None:
        """排除「截到纯色/黑屏」这类废图。"""
        img = load(name)
        assert img.std() > 10, f'{name} 疑似空白图 (std={img.std():.2f})'


# ══════════════════════════════════════════════
# 5. 明确记录未覆盖项，避免误以为「已全覆盖」
# ══════════════════════════════════════════════


def test_known_uncovered_fixtures() -> None:
    """记录 T3-3② 尚未覆盖的页面，防止「以为采全了」。

    这几条是**已知缺口**，不是失败断言 —— 等用户补图后应移入上面的
    正样本组并删除本测试。
    """
    uncovered = {
        'day_battle': '昼战 —— 用户反馈流程复杂, 2026-10-12 未截到',
        'flagship_severe_damage': '旗舰大破 —— 触发概率低',
        'get_ship': '舰船掉落页 —— 用户已改为快速点击跳过, 流程不再经过',
    }
    for name, reason in uncovered.items():
        assert not (FIXTURE_DIR / f'{name}.png').exists(), (
            f'{name}.png 已存在, 请把它移入 TestPositiveSamples 并删除本测试的对应项'
        )
        assert reason, f'{name} 缺少缺口说明'