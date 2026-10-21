"""测试准备页弹药量 (弹药是否为空) 单点检测。

输入约定: **RGB** 数组 (与 screenshot() 协议一致)。

历史 (供维护参考):
- 2026-10-08: 旧实现误按 BGR 反转通道, scrcpy 截图上永远匹配失败 →
  全部判"状态未知" → 旧兜底直接补给 (弹药 40% 也误补)。
- 2026-10-10: 整行占比算法对检测段边界敏感 (低弹药时唯一填充格 =
  最左红色警告格, 贴近旧段左缘被切在框外) → 误判全空 → 误补给。
  现为**单点检测** (第 1 格中心 3x3, 差分校准坐标), 从机制上消除
  边界敏感问题。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autowsgr.context import GameContext
from autowsgr.emulator import AndroidController
from autowsgr.infra.config import UserConfig
from autowsgr.ui.battle.ammo import detect_ammo_empty
from autowsgr.ui.battle.base import Panel
from autowsgr.ui.battle.constants import (
    AMMO_BAR_Y1,
    AMMO_BAR_Y2,
    AMMO_FIRST_CELL_POINTS,
    CLICK_SUPPLY_ALL,
)
from autowsgr.ui.battle.preparation import BattlePreparationPage
from autowsgr.ui.battle.supply import SUPPLY_MODE_AUTO, SUPPLY_MODE_SCRIPT


# 屏幕基准尺寸 (与截图一致, 缩放无关因为坐标相对)。
_W, _H = 960, 540

# 样本颜色均为 **RGB** (与 screenshot() 协议一致)。
# 填充子弹实测见过黄/蓝/绿/红(警告)四种色相, 判定是色相无关的 (饱和度策略)。
yellow_rgb = (234, 173, 31)
gray_rgb = (139, 140, 140)
blue_rgb = (14, 140, 229)  # 部分舰船的填充子弹色
green_rgb = (64, 161, 109)  # 部分填充见过的绿色子弹
red_rgb = (230, 60, 60)  # 低弹药警告色 (最左格)


def _make_screen() -> np.ndarray:
    """全深色背景 (模拟无船时的空槽面板色, 亮度 < 90)。"""
    screen = np.zeros((_H, _W, 3), dtype=np.uint8)
    screen[:, :] = (20, 30, 40)
    return screen


def _paint_point(screen: np.ndarray, slot: int, rgb: tuple[int, int, int]) -> None:
    """在 slot 的检测点 (第 1 格中心) 涂 3x3 邻域色。"""
    rx, ry = AMMO_FIRST_CELL_POINTS[slot]
    cx, cy = int(rx * _W), int(ry * _H)
    screen[cy - 1 : cy + 2, cx - 1 : cx + 2] = rgb


def _paint_row_gray_around_point(screen: np.ndarray, slot: int) -> None:
    """检测点两侧涂灰 (模拟该船仅剩第 1 格有弹的行环境, 增强真实感)。"""
    rx, _ = AMMO_FIRST_CELL_POINTS[slot]
    cx = int(rx * _W)
    y1, y2 = int(AMMO_BAR_Y1 * _H), int(AMMO_BAR_Y2 * _H)
    screen[y1:y2, max(0, cx - 30) : cx - 2] = gray_rgb
    screen[y1:y2, cx + 2 : cx + 30] = gray_rgb


def test_full_ammo_not_empty():
    screen = _make_screen()
    for slot in range(6):
        _paint_point(screen, slot, yellow_rgb)
    res = detect_ammo_empty(screen)
    assert res == [False] * 6


def test_blue_and_green_filled_cells_not_empty():
    """填充子弹色相随 UI 状态变化 (蓝/绿实测见过), 均应判有弹药。"""
    screen = _make_screen()
    _paint_point(screen, 0, blue_rgb)
    _paint_point(screen, 1, green_rgb)
    res = detect_ammo_empty(screen)
    assert res[0] is False
    assert res[1] is False


def test_empty_ammo_detected():
    screen = _make_screen()
    _paint_point(screen, 1, gray_rgb)
    res = detect_ammo_empty(screen)
    assert res[1] is True  # 第 1 格灰 = 弹药全空
    assert res[0] is None  # 无舰船 (深色背景)


def test_low_ammo_red_cell_regression():
    """实机回归 (2026-10-10 误补给): 弹药仅剩最左 1 格红色警告格。

    旧整行占比算法对检测段边界敏感 (红格贴近段左缘), 曾误判全空 →
    误触发补给。单点检测 (第 1 格中心 3x3) 必须判 False (有弹)。
    """
    screen = _make_screen()
    for slot in range(6):
        _paint_point(screen, slot, red_rgb)  # 红色警告格
        _paint_row_gray_around_point(screen, slot)  # 两侧灰格环境
    res = detect_ammo_empty(screen)
    assert res == [False] * 6


def test_scrcpy_40_percent_regression():
    """实机回归 (2026-10-08 误补场景): 全队 40% 弹药 → 未全空。

    弹药从右往左消耗, 40% 时第 1 格 (最左) 仍为填充色 → False。
    """
    screen = _make_screen()
    for slot in range(6):
        _paint_point(screen, slot, yellow_rgb)
        _paint_row_gray_around_point(screen, slot)
    res = detect_ammo_empty(screen)
    assert res == [False] * 6


def test_no_ship_returns_none():
    screen = _make_screen()  # 全部深色背景, 无舰船
    res = detect_ammo_empty(screen)
    assert res == [None, None, None, None, None, None]


def test_occupied_override_marks_unoccupied_as_none():
    screen = _make_screen()
    _paint_point(screen, 0, yellow_rgb)
    res = detect_ammo_empty(screen, occupied=[False] * 6)
    assert res == [None] * 6


def test_occupied_override_reports_for_occupied_only():
    screen = _make_screen()
    _paint_point(screen, 0, gray_rgb)  # 空弹药
    _paint_point(screen, 1, yellow_rgb)  # 有弹药
    res = detect_ammo_empty(screen, occupied=[True, True, False, False, False, False])
    assert res[0] is True
    assert res[1] is False
    assert res[2] is None


def _make_page(screen: np.ndarray) -> BattlePreparationPage:
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    return BattlePreparationPage(GameContext(ctrl=ctrl, config=MagicMock(), ocr=None))


def test_detect_fleet_snapshot_includes_ammo_empty():
    screen = _make_screen()
    _paint_point(screen, 0, yellow_rgb)  # 有弹药
    _paint_point(screen, 1, gray_rgb)  # 空弹药
    page = _make_page(screen)
    with patch.object(
        page,
        'detect_fleet',
        return_value=['岛风', '雪风', None, None, None, None],
    ):
        snapshot = page.detect_fleet_snapshot()
    assert snapshot.ammo_empty is not None
    assert snapshot.ammo_empty[0] is False
    assert snapshot.ammo_empty[1] is True
    assert snapshot.ammo_empty[2] is None


def test_public_detect_ammo_empty_method():
    screen = _make_screen()
    _paint_point(screen, 2, gray_rgb)  # 空弹药
    page = _make_page(screen)
    res = page.detect_ammo_empty()
    assert res[2] is True
    assert res[0] is None


# ─────────────────────────────────────────────
# 补给方式开关 (auto / script)
# ─────────────────────────────────────────────


def _make_page_cfg(ctrl: AndroidController, cfg: UserConfig) -> BattlePreparationPage:
    return BattlePreparationPage(GameContext(ctrl=ctrl, config=cfg, ocr=None, ship_ocr=None))


def test_supply_all_clicks_quick_supply_then_all_button():
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig())
    with patch.object(page, 'select_panel') as select_panel:
        page.supply_all()
        select_panel.assert_called_once_with(Panel.QUICK_SUPPLY)
        ctrl.click.assert_called_once_with(*CLICK_SUPPLY_ALL)


def test_ensure_auto_supply_enables_when_off():
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig())
    with (
        patch.object(page, 'is_auto_supply_enabled', return_value=False),
        patch.object(page, 'toggle_auto_supply') as toggle,
    ):
        page.ensure_auto_supply()
        toggle.assert_called_once()


def test_ensure_auto_supply_skips_when_on():
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig())
    with (
        patch.object(page, 'is_auto_supply_enabled', return_value=True),
        patch.object(page, 'toggle_auto_supply') as toggle,
    ):
        page.ensure_auto_supply()
        toggle.assert_not_called()


def test_apply_supply_auto_mode_uses_checkbox():
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=True),
        patch.object(page, 'ensure_auto_supply') as ensure,
        patch.object(page, 'supply_all') as supply_all,
    ):
        page.apply_supply()
        ensure.assert_called_once()
        supply_all.assert_not_called()


def test_apply_supply_script_mode_clicks_all():
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=True),
        patch.object(page, 'ensure_auto_supply') as ensure,
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'supply_all') as supply_all,
    ):
        page.apply_supply()
        supply_all.assert_called_once()
        ensure.assert_not_called()
        # 脚本补给必须先取消游戏内自动补给（2026-10-12 修复）
        disable.assert_called_once()


def test_apply_supply_explicit_mode_override():
    ctrl = MagicMock(spec=AndroidController)
    # 配置为 auto, 但调用时显式指定 script
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=True),
        patch.object(page, 'ensure_auto_supply') as ensure,
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'supply_all') as supply_all,
    ):
        page.apply_supply(mode=SUPPLY_MODE_SCRIPT)
        supply_all.assert_called_once()
        ensure.assert_not_called()
        disable.assert_called_once()


# ─────────────────────────────────────────────
# 自动补给复选框校正 (2026-10-12 用户指出的漏洞)
# ─────────────────────────────────────────────
#
# 复选框是**游戏内的持久状态**, 不随页面切换重置。修复前 script 模式从不碰它,
# 上一轮 auto 模式留下的勾选态会持续生效 → 脚本补完弹药, 出击时游戏再自动
# 补一次, 「按需补给」语义彻底失效。


def test_disable_auto_supply_toggles_when_on():
    """复选框已勾选 → 必须点击取消。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig())
    with (
        patch.object(page, 'is_auto_supply_enabled', return_value=True),
        patch.object(page, 'toggle_auto_supply') as toggle,
    ):
        page.disable_auto_supply()
        toggle.assert_called_once()


def test_disable_auto_supply_skips_when_off():
    """已是未勾选 → 不点击（幂等，避免每轮多余操作）。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig())
    with (
        patch.object(page, 'is_auto_supply_enabled', return_value=False),
        patch.object(page, 'toggle_auto_supply') as toggle,
    ):
        page.disable_auto_supply()
        toggle.assert_not_called()


def test_apply_supply_script_mode_disables_checkbox():
    """核心回归：script 模式必须取消自动补给勾选。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=True),
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'supply_all'),
    ):
        page.apply_supply()
        disable.assert_called_once()


def test_apply_supply_auto_mode_never_disables_checkbox():
    """auto 模式下绝不能取消勾选（否则补给完全失效）。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=True),
        patch.object(page, 'ensure_auto_supply') as ensure,
        patch.object(page, 'disable_auto_supply') as disable,
    ):
        page.apply_supply()
        ensure.assert_called_once()
        disable.assert_not_called()


def test_apply_supply_auto_mode_skips_ammo_detection():
    """auto 模式由游戏在出击时补给，脚本不需要检测弹药状态。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with (
        patch.object(page, 'ensure_auto_supply'),
        patch.object(page, 'has_empty_ammo_ship') as ammo,
    ):
        page.apply_supply()
        ammo.assert_not_called()


def test_apply_supply_auto_mode_caches_checkbox_check():
    """复选框是持久状态：同一会话只需在首场确认一次。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with patch.object(page, 'ensure_auto_supply') as ensure:
        page.apply_supply()
        page.apply_supply()
        ensure.assert_called_once()


def test_apply_supply_auto_mode_force_rechecks():
    """force=True 必须绕过缓存重新确认复选框。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with patch.object(page, 'ensure_auto_supply') as ensure:
        page.apply_supply()
        page.apply_supply(force=True)
        assert ensure.call_count == 2


def test_apply_supply_auto_after_script_rechecks_checkbox():
    """模式切换会改变复选框状态，切回 auto 时必须重新确认。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with (
        patch.object(page, 'ensure_auto_supply') as ensure,
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'has_empty_ammo_ship', return_value=False),
    ):
        page.apply_supply()  # auto → 首次确认
        page.apply_supply(mode=SUPPLY_MODE_SCRIPT)  # script → 取消勾选
        page.apply_supply(mode=SUPPLY_MODE_AUTO)  # 缓存失效 → 重新确认
        assert ensure.call_count == 2
        disable.assert_called_once()


def test_apply_supply_script_disables_checkbox_even_when_no_supply_needed():
    """**顺序关键**：即使弹药充足不补给，也必须校正复选框。

    这正是漏洞的隐蔽之处 —— 旧逻辑在「无需补给」时提前 return,
    残留的勾选态就会在出击时触发游戏自动补给。
    """
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=False),
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'supply_all') as supply_all,
    ):
        page.apply_supply()
        disable.assert_called_once()
        supply_all.assert_not_called()


def test_apply_supply_script_disables_checkbox_when_ammo_unknown():
    """弹药状态未知时同样必须校正复选框（跳过补给 ≠ 跳过校正）。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=None),
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'supply_all') as supply_all,
    ):
        page.apply_supply()
        disable.assert_called_once()
        supply_all.assert_not_called()


def test_apply_supply_unknown_mode_falls_back_to_script():
    """未知模式回退为 script —— 且回退后仍要取消勾选。"""
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig())
    with (
        patch.object(page, 'has_empty_ammo_ship', return_value=True),
        patch.object(page, 'disable_auto_supply') as disable,
        patch.object(page, 'supply_all') as supply_all,
    ):
        page.apply_supply(mode='garbage')
        disable.assert_called_once()
        supply_all.assert_called_once()


def test_apply_supply_checkbox_corrected_before_ammo_check():
    """校正必须发生在弹药检测**之前**（顺序不变量）。

    用调用序列表记录，断言 disable 先于 has_empty_ammo_ship 被调用 ——
    这样「无需补给」分支才不会绕过校正。
    """
    ctrl = MagicMock(spec=AndroidController)
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    order: list[str] = []

    with (
        patch.object(page, 'disable_auto_supply', side_effect=lambda: order.append('disable')),
        patch.object(
            page, 'has_empty_ammo_ship', side_effect=lambda: order.append('check') or False
        ),
        patch.object(page, 'supply_all'),
    ):
        page.apply_supply()

    assert order == ['disable', 'check'], f'校正应先于弹药检测, 实际: {order}'


# ─────────────────────────────────────────────
# 任一舰船弹药全空即补给 (2026-10-10 用户指令)
# ─────────────────────────────────────────────


def test_apply_supply_skips_when_ammo_not_empty():
    screen = _make_screen()
    _paint_point(screen, 0, yellow_rgb)  # 所有船尚有弹药
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'supply_all') as supply_all,
        patch.object(page, 'ensure_auto_supply') as ensure,
    ):
        page.apply_supply()
        supply_all.assert_not_called()
        ensure.assert_not_called()
    assert ctrl.click.call_count == 0  # 不应产生任何点击


def test_apply_supply_supplies_when_ammo_all_empty():
    screen = _make_screen()
    for slot in range(6):
        _paint_point(screen, slot, gray_rgb)  # 全舰队弹药全空
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with patch.object(page, 'supply_all') as supply_all:
        page.apply_supply()
        supply_all.assert_called_once()


def test_apply_supply_supplies_when_any_ship_empty():
    """任一舰船弹药全空 (其余有弹) → 补给 (2026-10-10 用户指令)。

    旧逻辑要求全舰队全空才补; 新逻辑放宽为任一船第 1 格全灰即补。
    """
    screen = _make_screen()
    _paint_point(screen, 0, yellow_rgb)  # 1 号位尚有弹药
    _paint_point(screen, 1, gray_rgb)  # 2 号位弹药全空
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with patch.object(page, 'supply_all') as supply_all:
        page.apply_supply()
        supply_all.assert_called_once()


def test_apply_supply_auto_mode_supplies_when_all_empty():
    screen = _make_screen()
    for slot in range(6):
        _paint_point(screen, slot, gray_rgb)
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_AUTO))
    with patch.object(page, 'ensure_auto_supply') as ensure:
        page.apply_supply()
        ensure.assert_called_once()


def test_apply_supply_skips_when_ammo_undetectable():
    """状态未知 (采样点非灰非彩) → 跳过补给, 绝不盲补 (用户约定 2026-10-08)。"""
    screen = _make_screen()  # 深色背景 → 全部 None (未知)
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'supply_all') as supply_all,
        patch.object(page, 'ensure_auto_supply') as ensure,
    ):
        page.apply_supply()
        supply_all.assert_not_called()
        ensure.assert_not_called()
    assert ctrl.click.call_count == 0  # 未知状态零点击


# ─────────────────────────────────────────────
# 检测失败重试 (采样点非灰非彩时 0.6s 重试, 最多 3 次)
# ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """重试间隔 0.6s 在测试中不实际等待。"""
    monkeypatch.setattr('autowsgr.ui.battle.supply.time.sleep', lambda _s: None)


def test_ammo_detect_retries_then_succeeds():
    """前两帧未见子弹行 (动画未结束), 第 3 次识别成功。"""
    blank = _make_screen()
    retry = _make_screen()
    for slot in range(6):
        _paint_point(retry, slot, gray_rgb)  # 全空
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.side_effect = [blank, blank, retry]
    page = _make_page_cfg(ctrl, UserConfig())

    assert page.has_empty_ammo_ship() is True
    assert ctrl.screenshot.call_count == 3


def test_ammo_detect_retry_exhausted_returns_none():
    """三次均未见子弹行 → 状态未知 (None), 仅截三次图。"""
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = _make_screen()
    page = _make_page_cfg(ctrl, UserConfig())

    assert page.has_empty_ammo_ship() is None
    assert ctrl.screenshot.call_count == 3


def test_ammo_detect_no_retry_when_first_frame_hits():
    """首帧即识别到弹药格 → 不重试。"""
    screen = _make_screen()
    _paint_point(screen, 0, yellow_rgb)
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.return_value = screen
    page = _make_page_cfg(ctrl, UserConfig())

    assert page.has_empty_ammo_ship() is False
    assert ctrl.screenshot.call_count == 1


def test_apply_supply_not_fooled_by_transient_blank_frame():
    """端到端回归: 首帧空白, 重试后识别到"有弹" → 不补给。"""
    blank = _make_screen()
    partial = _make_screen()
    _paint_point(partial, 0, yellow_rgb)  # 尚有弹药
    ctrl = MagicMock(spec=AndroidController)
    ctrl.screenshot.side_effect = [blank, partial]
    page = _make_page_cfg(ctrl, UserConfig(supply_mode=SUPPLY_MODE_SCRIPT))
    with (
        patch.object(page, 'supply_all') as supply_all,
        patch.object(page, 'ensure_auto_supply') as ensure,
    ):
        page.apply_supply()
        supply_all.assert_not_called()
        ensure.assert_not_called()
