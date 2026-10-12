"""BuildPage「卸下装备」复选框的状态收敛测试（2026-10-12 修复）。

背景
----
解装页的「卸下装备」是**游戏内的持久状态**复选框，不随页面切换重置。
修复前的写法是::

    if remove_equipment:
        page.destroy_toggle_remove_equip()# 裸 click，不读状态

导致两条失效路径：

1. **连续两轮解装**（长挂机最常见的 DOCK_FULL 场景）：每轮都点一下 →
   第 1 轮勾选（装备被卸下），第 2 轮**取消勾选** → **装备随舰船不可逆销毁**。
2. **配置改为 False 但上一轮是 True**：残留的勾选态生效，用户明确配置的
   「不卸装备」被静默忽略。

修复方式与 :meth:`SupplyMixin.apply_supply` 对称：改为**无条件收敛到目标态**
（``ensure_remove_equip`` / ``disable_remove_equip``），且检测放在任何提前
return 之前。

本文件用**实机截图**（用户提供，960x540）做检测函数的回归，并锁定业务调用点
必须走 ensure/disable 而非盲切换。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from autowsgr.ui.build_page import (
    CLICK_DESTROY_REMOVE_EQUIP,
    DESTROY_REMOVE_EQUIP_BOX,
    DESTROY_REMOVE_EQUIP_DARK_LUM,
    DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS,
    BuildPage,
)


# fixture 统一放 testing/fixtures/, 跨测试目录共享 (combat / ui / ops 都会用到)
FIXTURE_DIR = Path(__file__).resolve().parents[2] / 'fixtures' / 'screenshots' / 'destroy'


def load(name: str) -> np.ndarray:
    """读取实机截图为 RGB 数组（screenshot() 协议是 RGB）。"""
    path = FIXTURE_DIR / name
    assert path.exists(), f'缺少 fixture: {path}'
    import cv2

    with path.open('rb') as fh:
        buf = np.frombuffer(fh.read(), np.uint8)
    bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    assert bgr is not None, f'无法解码: {path}'
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


# ══════════════════════════════════════════════
# 检测函数：实机正/负样本
# ══════════════════════════════════════════════


class TestRemoveEquipDetection:
    def test_detects_unchecked_state(self) -> None:
        """未勾选截图 → False。"""
        assert BuildPage.is_destroy_remove_equip_enabled(
            load('destroy_remove_equip_off.png')
        ) is False

    def test_detects_checked_state(self) -> None:
        """勾选截图 → True。"""
        assert BuildPage.is_destroy_remove_equip_enabled(
            load('destroy_remove_equip_on.png')
        ) is True

    def test_dark_pixel_margin_is_large(self) -> None:
        """两种状态的暗像素数必须有充足余量，否则阈值是脆的。

        实测：勾选态 62 个 vs 未勾选态 0 个，阈值 12。
        若哪天余量被压缩到个位数，说明抗锯齿/压缩噪声变强，阈值需要重标。
        """
        counts = {}
        for name in ('destroy_remove_equip_off.png', 'destroy_remove_equip_on.png'):
            img = load(name)
            x0, y0, x1, y1 = DESTROY_REMOVE_EQUIP_BOX
            crop = img[y0:y1, x0:x1]
            counts[name] = int((crop.sum(axis=2) < DESTROY_REMOVE_EQUIP_DARK_LUM).sum())

        off = counts['destroy_remove_equip_off.png']
        on = counts['destroy_remove_equip_on.png']
        assert off < DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS, f'未勾选态暗像素过多: {off}'
        assert on >= DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS * 2, (
            f'勾选态余量不足 2 倍: {on} vs 阈值 {DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS}'
        )

    def test_threshold_distinguishes_both_directions(self) -> None:
        """阈值必须**双向**可分 —— 这是变异测试补的缺口。

        原先只验证了「阈值升高不会误判」，但阈值升到700 时两种截图的
        检测结果**恰好都不变**（都是「已勾选」，因为未勾选底色 708~718
        仍高于 700），单看结果发现不了。

        实测亮度分布很关键：
        - 未勾选：708~718，**极窄**（纯白底+ JPEG噪声）
        - 勾选：111~753，**很宽**（对勾线深灰 + 部分背景）

        因此失效边界是未勾选底色的下界(708)。这里直接断言阈值落在
        「对勾线亮度」与「未勾选底色下界」之间 —— 两头都有真实余量。
        """
        off = load('destroy_remove_equip_off.png')
        on = load('destroy_remove_equip_on.png')
        x0, y0, x1, y1 = DESTROY_REMOVE_EQUIP_BOX

        def dark_count(img: np.ndarray, lum: int) -> int:
            return int((img[y0:y1, x0:x1].sum(axis=2) < lum).sum())

        off_lum = off[y0:y1, x0:x1].sum(axis=2)
        on_lum = on[y0:y1, x0:x1].sum(axis=2)
        off_bg_min = int(off_lum.min())      # 未勾选底色下界, 实测 708
        on_ink_min = int(on_lum.min())        # 对勾线最深处, 实测 111

        # 阈值必须夹在两者之间, 且两头各留足余量(避免贴着边界)
        assert on_ink_min < DESTROY_REMOVE_EQUIP_DARK_LUM < off_bg_min, (
            f'阈值 {DESTROY_REMOVE_EQUIP_DARK_LUM} 未落在实测区间 '
            f'({on_ink_min}, {off_bg_min}) 内 —— 改它会直接误判'
        )
        # 真实失效边界: 阈值越过未勾选底色下界(708)后, 未勾选态开始出现暗像素,
        # 再升高就会越过 MIN_DARK_PIXELS 而被误判为「已勾选」→ ensure 跳过点击。
        # 因此断言「阈值抬到失效点时确实会误判」, 证明这条断言有意义。
        broken_threshold = off_bg_min + 1
        assert dark_count(off, broken_threshold) >= DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS, (
            '失效边界假设有误: 阈值越过底色下界后未勾选态竟仍未误判'
        )
        # 贴近对勾线时, 勾选态仍应检出足够暗像素 (下界方向的余量)
        assert dark_count(on, on_ink_min + 60) >= DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS

    def test_blank_screen_reports_unchecked(self) -> None:
        """空白/异常画面 → False（保守：视为未勾选，由调用方补一次点击）。

        比返回 True 安全 —— 误判为「已勾选」会让 ensure_remove_equip 跳过点击,
        而实际状态未勾选, 于是带装备解装。
        """
        blank = np.full((540, 960, 3), 255, dtype=np.uint8)
        assert BuildPage.is_destroy_remove_equip_enabled(blank) is False

    def test_wrong_resolution_returns_unchecked(self) -> None:
        """非 960x540 画面不得取错像素，保守返回 False。"""
        small = np.zeros((270, 480, 3), dtype=np.uint8)
        assert BuildPage.is_destroy_remove_equip_enabled(small) is False


# ══════════════════════════════════════════════
# ensure / disable 的幂等性
# ══════════════════════════════════════════════


@pytest.fixture
def page() -> BuildPage:
    ctrl = MagicMock()
    ctrl.screenshot.return_value = np.zeros((540, 960, 3), dtype=np.uint8)
    return BuildPage(ctrl)


class TestEnsureDisableIdempotency:
    def test_enable_toggles_when_unchecked(self, page: BuildPage) -> None:
        with (
            patch.object(page, 'is_destroy_remove_equip_enabled', return_value=False),
            patch.object(page, 'destroy_toggle_remove_equip') as toggle,
        ):
            page.ensure_remove_equip()
            toggle.assert_called_once()

    def test_enable_skips_when_already_checked(self, page: BuildPage) -> None:
        with (
            patch.object(page, 'is_destroy_remove_equip_enabled', return_value=True),
            patch.object(page, 'destroy_toggle_remove_equip') as toggle,
        ):
            page.ensure_remove_equip()
            toggle.assert_not_called()

    def test_disable_toggles_when_checked(self, page: BuildPage) -> None:
        with (
            patch.object(page, 'is_destroy_remove_equip_enabled', return_value=True),
            patch.object(page, 'destroy_toggle_remove_equip') as toggle,
        ):
            page.disable_remove_equip()
            toggle.assert_called_once()

    def test_disable_skips_when_already_unchecked(self, page: BuildPage) -> None:
        with (
            patch.object(page, 'is_destroy_remove_equip_enabled', return_value=False),
            patch.object(page, 'destroy_toggle_remove_equip') as toggle,
        ):
            page.disable_remove_equip()
            toggle.assert_not_called()


# ══════════════════════════════════════════════
# 业务调用点：必须无条件收敛（核心回归）
# ══════════════════════════════════════════════


class TestDestroyShipsConvergence:
    """锁定 ``destroy_ships`` 走 ensure/disable 而非盲切换。

    这组测试守护的正是「装备被销毁」那条失效路径。
    """

    def _run(self, page: BuildPage, *, remove_equipment: bool, calls: list[str]) -> None:
        with (
            patch.object(page, 'ensure_remove_equip', side_effect=lambda: calls.append('ensure')),
            patch.object(page, 'disable_remove_equip', side_effect=lambda: calls.append('disable')),
            patch.object(page, 'destroy_toggle_remove_equip',
                         side_effect=lambda: calls.append('BLIND_TOGGLE')),
            patch.object(page, 'destroy_click_add'),
            patch.object(page, 'destroy_quick_select'),
            patch.object(page, 'destroy_confirm_select'),
            patch.object(page, 'destroy_confirm'),
            patch.object(page, 'destroy_four_star_confirm'),
            patch('autowsgr.ui.build_page.time.sleep'),
        ):
            page.destroy_ships(None, remove_equipment=remove_equipment)

    def test_true_uses_ensure_not_blind_toggle(self, page: BuildPage) -> None:
        calls: list[str] = []
        self._run(page, remove_equipment=True, calls=calls)
        assert 'ensure' in calls, f'应调用 ensure_remove_equip, 实际: {calls}'
        assert 'BLIND_TOGGLE' not in calls, '不得盲切换（连续两轮会把勾选取消掉）'

    def test_false_uses_disable_not_skip(self, page: BuildPage) -> None:
        """配置 False 时必须显式**取消**，而不是什么都不做。

        这正是失效路径 2：残留的勾选态会让「不卸装备」的配置形同虚设。
        """
        calls: list[str] = []
        self._run(page, remove_equipment=False, calls=calls)
        assert 'disable' in calls, f'应调用 disable_remove_equip 清理残留勾选, 实际: {calls}'
        assert 'BLIND_TOGGLE' not in calls


# ══════════════════════════════════════════════
# 坐标常量契约
# ══════════════════════════════════════════════


class TestConstants:
    def test_probe_box_inside_frame(self) -> None:
        x0, y0, x1, y1 = DESTROY_REMOVE_EQUIP_BOX
        assert 0 <= x0 < x1 <= 960, f'包围盒 x 越界: {(x0, x1)}'
        assert 0 <= y0 < y1 <= 540, f'包围盒 y 越界: {(y0, y1)}'

    def test_click_coord_falls_inside_probe_box(self) -> None:
        """点击坐标必须落在检测框内，否则「检测的是 A，点的是 B」。"""
        x0, y0, x1, y1 = DESTROY_REMOVE_EQUIP_BOX
        px, py = CLICK_DESTROY_REMOVE_EQUIP
        cx, cy = int(px * 960), int(py * 540)
        assert x0 <= cx <= x1, f'点击 x={cx} 不在检测框 x范围 {(x0, x1)} 内'
        assert y0 <= cy <= y1, f'点击 y={cy} 不在检测框 y范围 {(y0, y1)} 内'

    def test_threshold_below_checked_and_above_unchecked(self) -> None:
        """亮度阈值必须落在「未勾选的框内底色」与「勾选的对勾线」之间。

        实测 (RGB 之和)：未勾选框内 ≈ 237*3=711（纯白）;
        勾选的对勾线 ≈ 56*3=168（深灰）。阈值 450 居中且余量充足。
        """
        unchecked_lum = 237 * 3   # 未勾选: 框内纯白
        checked_lum = 56 * 3      # 勾选: 对勾线深灰
        assert checked_lum < DESTROY_REMOVE_EQUIP_DARK_LUM < unchecked_lum, (
            f'亮度阈值 {DESTROY_REMOVE_EQUIP_DARK_LUM} 未落在实测区间 '
            f'({checked_lum}, {unchecked_lum}) 内'
        )


def test_fixtures_resolution() -> None:
    """fixture 必须是 960x540，否则检测框取错位置。"""
    for name in ('destroy_remove_equip_off.png', 'destroy_remove_equip_on.png'):
        assert load(name).shape == (540, 960, 3), f'{name} 分辨率异常'
