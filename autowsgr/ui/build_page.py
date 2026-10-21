"""建造页面 UI 控制器。

已完成，需测试

覆盖游戏 **建造** 页面及其标签组 (建造/解体/开发/废弃) 的交互。

页面入口:
    主页面 → 侧边栏 → 建造

标签组:
    建造/解体/开发/废弃 四个标签共享相同的顶部导航栏。
    切换标签不会离开此页面组，只是改变内容区域。

坐标体系:
    所有坐标为相对值 (0.0-1.0)。

使用方式::

    from autowsgr.ui.build_page import BuildPage, BuildTab

    page = BuildPage(ctrl)
    page.switch_tab(BuildTab.DEVELOP)
    page.go_back()
"""

from __future__ import annotations

import enum
import time
from typing import TYPE_CHECKING

from autowsgr.image_resources import Templates
from autowsgr.infra.logger import get_logger
from autowsgr.types import PageName, ShipType
from autowsgr.vision import ImageChecker, PageMatch

from .page import click_and_wait_for_page
from .tabbed_page import (
    TabbedPageType,
    check_tabbed_page,
    get_active_tab_index,
    make_tab_checker,
)


if TYPE_CHECKING:
    import numpy as np

    from autowsgr.context import GameContext


_log = get_logger('ui')

# ═══════════════════════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════════════════════


class BuildTab(enum.Enum):
    """建造页面标签组。"""

    BUILD = '建造'
    DESTROY = '解体'
    DEVELOP = '开发'
    DISCARD = '废弃'


# ═══════════════════════════════════════════════════════════════════════════════
# 标签索引映射
# ═══════════════════════════════════════════════════════════════════════════════

_TAB_LIST: list[BuildTab] = list(BuildTab)
"""标签枚举值列表 — 索引与标签栏探测位置一一对应。"""

_TAB_TO_INDEX: dict[BuildTab, int] = {tab: i for i, tab in enumerate(_TAB_LIST)}
"""标签 → 标签索引映射。"""


# ═══════════════════════════════════════════════════════════════════════════════
# 点击坐标
# ═══════════════════════════════════════════════════════════════════════════════

CLICK_BACK: tuple[float, float] = (0.022, 0.058)
"""回退按钮 (◁)。"""

CLICK_TAB: dict[BuildTab, tuple[float, float]] = {
    BuildTab.BUILD: (0.1875, 0.0463),
    BuildTab.DESTROY: (0.3125, 0.0463),
    BuildTab.DEVELOP: (0.4375, 0.0463),
    BuildTab.DISCARD: (0.5625, 0.0463),
}
"""标签切换点击坐标。

.. note::
    坐标为估计值 (TODO: 待实际截图确认)。
"""

# ── 建造槽位 ──

BUILD_SLOT_POSITIONS: list[tuple[float, float]] = [
    (0.823, 0.312),
    (0.823, 0.508),
    (0.823, 0.701),
    (0.823, 0.898),
]
"""4 个建造槽位的中心点 (start/complete/fast 按钮位置)。"""

CLICK_CONFIRM_BUILD: tuple[float, float] = (0.89, 0.89)
"""确认建造按钮。"""

# ── 解体标签操作 ──

CLICK_DESTROY_ADD: tuple[float, float] = (0.0938, 0.3815)
"""解体 — 点击「添加」按钮。旧代码: timer.click(90, 206)"""

CLICK_DESTROY_QUICK_SELECT: tuple[float, float] = (0.91, 0.3)
"""解体 — 快速选择按钮。"""

CLICK_DESTROY_CONFIRM_SELECT: tuple[float, float] = (0.915, 0.906)
"""解体 — 确认选择。"""

CLICK_DESTROY_REMOVE_EQUIP: tuple[float, float] = (0.837, 0.646)
"""解体 — 卸下装备复选框。

坐标标定 (2026-10-12 实机截图差分): 复选框本体位于像素 x798-814 / y342-358,
中心 (806, 351) → 归一化 (0.8396, 0.6505)。此处沿用旧值 (0.837, 0.646),
偏差 3px 仍落在框内, 且点击有容错, 故不改以免影响既有实机行为。
"""

DESTROY_REMOVE_EQUIP_BOX: tuple[int, int, int, int] = (798, 342, 815, 359)
"""解体 — 卸下装备复选框的像素包围盒 (x0, y0, x1, y1), 基于 960x540 实机截图标定。

用于**状态检测**(而非点击): 勾选时框内有深色对勾线, 未勾选时框内是纯白,
因此统计框内暗像素数量即可判定, 比单点取色更抗干扰 (见
:meth:`BuildPage.is_destroy_remove_equip_enabled`)。
"""

DESTROY_REMOVE_EQUIP_DARK_LUM: int = 450
"""卸下装备复选框内判定「有勾」的亮度阈值 (R+G+B 之和)。

实测: 未勾选时框内均值 RGB≈(237,237,237) → 和≈711, 暗像素 0 个;
勾选时对勾线 RGB≈(56,56,56) → 和≈168。450 处于两者之间, 余量充足。
"""

DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS: int = 12
"""判定「已勾选」所需的最小暗像素数。

实测勾选态 17x17 框内有 62 个暗像素、未勾选态 0 个。取 12 作为阈值:
既能容忍抗锯齿/JPEG压缩导致的少量像素漂移, 又远低于勾选态的真实值。
"""

CLICK_DESTROY_CONFIRM: tuple[float, float] = (0.9, 0.9)
"""解体 — 解装确认按钮。"""

CLICK_DESTROY_FOUR_STAR_CONFIRM: tuple[float, float] = (0.38, 0.567)
"""解体 — 四星确认弹窗。"""

CLICK_DESTROY_TYPE_FILTER: tuple[float, float] = (0.912, 0.681)
"""解体 — 打开舰船类型过滤器。"""

CLICK_DESTROY_CONFIRM_FILTER: tuple[float, float] = (0.9, 0.85)
"""解体 — 确认舰船类型过滤。"""

# ── 建造获取动画 ──

CLICK_DISMISS_ANIMATION: tuple[float, float] = (0.9531, 0.9537)
"""点击跳过建造获取动画。旧代码: timer.click(915, 515)"""


# ═══════════════════════════════════════════════════════════════════════════════
# 页面控制器
# ═══════════════════════════════════════════════════════════════════════════════


class BuildPage:
    """建造页面控制器 (含 解体/开发/废弃 标签组)。

    Parameters
    ----------
    ctrl:
        Android 设备控制器实例。
    """

    def __init__(self, ctx: GameContext) -> None:
        self._ctx = ctx
        self._ctrl = ctx.ctrl

    # ── 页面识别 ──────────────────────────────────────────────────────────

    @staticmethod
    def is_current_page(screen: np.ndarray) -> PageMatch:
        """判断截图是否为建造页面组 (含全部 4 个标签)。

        通过统一标签页检测层识别,返回带覆盖度分数的 PageMatch。

        Parameters
        ----------
        screen:
            截图 (HxWx3, RGB)。
        """
        return check_tabbed_page(screen, TabbedPageType.BUILD)

    @staticmethod
    def get_active_tab(screen: np.ndarray) -> BuildTab | None:
        """获取当前激活的标签。

        Parameters
        ----------
        screen:
            截图 (HxWx3, RGB)。

        Returns
        -------
        BuildTab | None
            当前标签，索引越界或无法确定时返回 ``None``。
        """
        idx = get_active_tab_index(screen)
        if idx is None or idx >= len(_TAB_LIST):
            return None
        return _TAB_LIST[idx]

    # ── 标签切换 ──────────────────────────────────────────────────────────

    def switch_tab(self, tab: BuildTab) -> None:
        """切换到指定标签并验证到达。

        会先截图判断当前标签状态并记录日志，然后点击目标标签，
        最后验证目标标签签名匹配。

        Parameters
        ----------
        tab:
            目标标签。

        Raises
        ------
        NavigationError
            超时未到达目标标签。
        """
        current = self.get_active_tab(self._ctrl.screenshot())
        _log.info(
            '[UI] 建造页面: {} → {}',
            current.value if current else '未知',
            tab.value,
        )
        target_idx = _TAB_TO_INDEX[tab]
        click_and_wait_for_page(
            self._ctrl,
            click_coord=CLICK_TAB[tab],
            checker=make_tab_checker(TabbedPageType.BUILD, target_idx),
            source=f'建造-{current.value if current else "?"}',
            target=f'建造-{tab.value}',
        )

    # ── 回退 ──────────────────────────────────────────────────────────────

    def go_back(self) -> None:
        """点击回退按钮 (◁)，返回侧边栏。

        Raises
        ------
        NavigationError
            超时仍在建造页面。
        """
        from autowsgr.ui.main_page import MainPage
        from autowsgr.ui.sidebar_page import SidebarPage

        _log.info('[UI] 建造页面 → 返回侧边栏')

        def _checker(screen: np.ndarray) -> bool:
            return SidebarPage.is_current_page(screen) or MainPage.is_current_page(screen)

        click_and_wait_for_page(
            self._ctrl,
            click_coord=CLICK_BACK,
            checker=_checker,
            source=PageName.BUILD,
            target=f'{PageName.SIDEBAR}/{PageName.MAIN}',
        )

    # ── 建造操作 ──────────────────────────────────────────────────────────

    def click_slot(self, slot: int) -> None:
        """点击建造槽位 (1-4)。

        Parameters
        ----------
        slot:
            槽位编号 (1-4)。
        """
        if not 1 <= slot <= 4:
            raise ValueError(f'建造槽位必须为 1-4，收到: {slot}')
        _log.info('[UI] 建造页面 → 点击槽位 {}', slot)
        self._ctrl.click(*BUILD_SLOT_POSITIONS[slot - 1])
        time.sleep(0.5)

    def collect_slot(self, slot: int) -> None:
        """收取指定槽位的已完成建造。

        点击槽位后等待弹出获取动画。

        Parameters
        ----------
        slot:
            槽位编号 (1-4)。
        """
        _log.info('[UI] 建造页面 → 收取槽位 {}', slot)
        self.click_slot(slot)
        time.sleep(1.0)

    def fast_build_slot(self, slot: int) -> None:
        """对指定槽位使用快速建造。

        点击快速建造按钮后等待确认弹窗。

        Parameters
        ----------
        slot:
            槽位编号 (1-4)。
        """
        _log.info('[UI] 建造页面 → 快速建造槽位 {}', slot)
        self.click_slot(slot)
        time.sleep(0.5)

    def start_build(self) -> None:
        """在当前标签下启动建造。

        点击一个空闲槽位的「开始建造」按钮后，
        等待资源选择页面，然后点击确认。

        .. note::
            资源配方调整（滑块操作）需要 ops 层配合 OCR 实现。
            此方法仅执行确认点击。
        """
        _log.info('[UI] 建造页面 → 确认建造')
        self._ctrl.click(*CLICK_CONFIRM_BUILD)
        time.sleep(1.0)

    def dismiss_animation(self) -> None:
        """点击跳过建造获取动画。"""
        self._ctrl.click(*CLICK_DISMISS_ANIMATION)

    # ── 解体操作 ──────────────────────────────────────────────────────────

    def destroy_click_add(self) -> None:
        """解体标签 → 点击「添加」按钮。"""
        _log.info('[UI] 建造页面 (解体) → 添加')
        self._ctrl.click(*CLICK_DESTROY_ADD)

    def destroy_quick_select(self) -> None:
        """解体标签 → 点击「快速选择」按钮。"""
        _log.info('[UI] 建造页面 (解体) → 快速选择')
        self._ctrl.click(*CLICK_DESTROY_QUICK_SELECT)

    def destroy_confirm_select(self) -> None:
        """解体标签 → 点击「确认选择」。"""
        _log.info('[UI] 建造页面 (解体) → 确认选择')
        self._ctrl.click(*CLICK_DESTROY_CONFIRM_SELECT)

    def destroy_toggle_remove_equip(self) -> None:
        """解体标签 → 点击「卸下装备」复选框。**无条件切换**。

        .. warning::
            这是**盲切换** (不读状态), 仅供调试与 :meth:`ensure_remove_equip`
            / :meth:`disable_remove_equip` 内部使用。业务流程请用后两者 ——
            见 :meth:`is_destroy_remove_equip_enabled` 的说明。
        """
        _log.info('[UI] 建造页面 (解体) → 卸下装备 (切换)')
        self._ctrl.click(*CLICK_DESTROY_REMOVE_EQUIP)

    @staticmethod
    def is_destroy_remove_equip_enabled(screen: np.ndarray) -> bool:
        """检测解体页「卸下装备」复选框是否已勾选。

        实现方式: 截取复选框包围盒 (:data:`DESTROY_REMOVE_EQUIP_BOX`),
        统计其中亮度低于 :data:`DESTROY_REMOVE_EQUIP_DARK_LUM` 的像素数,
        达到 :data:`DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS` 即视为已勾选。

        为什么统计暗像素数量而非取单点颜色
        ----------------------------------
        对勾是抗锯齿的斜线, 单点取色落在勾上/勾外结果会跳变;
        而「框内有没有深色笔画」这个整体特征稳定得多
        (实测勾选态 62 个暗像素 vs 未勾选态 0 个)。

        坐标基于 960x540 实机截图标定; 其他分辨率下会退化, 返回 ``False``
        (保守: 视为未勾选, 由调用方补一次点击)。
        """
        h, w = screen.shape[:2]
        x0, y0, x1, y1 = DESTROY_REMOVE_EQUIP_BOX
        # 分辨率不符时框会取错位置, 直接返回未勾选(保守), 不做无意义的像素统计
        if x1 > w or y1 > h:
            return False
        crop = screen[y0:y1, x0:x1]
        dark_count = int((crop.sum(axis=2) < DESTROY_REMOVE_EQUIP_DARK_LUM).sum())
        return dark_count >= DESTROY_REMOVE_EQUIP_MIN_DARK_PIXELS

    def ensure_remove_equip(self) -> None:
        """确保「卸下装备」复选框处于**勾选**状态 (幂等)。"""
        screen = self._ctrl.screenshot()
        if self.is_destroy_remove_equip_enabled(screen):
            _log.debug('[UI] 建造页面 (解体) → 卸下装备已勾选, 跳过')
            return
        self.destroy_toggle_remove_equip()

    def disable_remove_equip(self) -> None:
        """确保「卸下装备」复选框处于**未勾选**状态 (幂等)。

        与 :meth:`ensure_remove_equip` 对称。业务逻辑必须用这两个而非
        :meth:`destroy_toggle_remove_equip`, 否则残留的勾选态会生效 ——
        见 :meth:`destroy_ships` 处的说明。
        """
        screen = self._ctrl.screenshot()
        if not self.is_destroy_remove_equip_enabled(screen):
            _log.debug('[UI] 建造页面 (解体) → 卸下装备未勾选, 跳过')
            return
        self.destroy_toggle_remove_equip()

    def destroy_confirm(self) -> None:
        """解体标签 → 点击「解装确认」。"""
        _log.info('[UI] 建造页面 (解体) → 解装确认')
        self._ctrl.click(*CLICK_DESTROY_CONFIRM)

    def destroy_four_star_confirm(self) -> None:
        """解体标签 → 四星确认弹窗点击确认。"""
        _log.info('[UI] 建造页面 (解体) → 四星确认')
        self._ctrl.click(*CLICK_DESTROY_FOUR_STAR_CONFIRM)

    def destroy_open_type_filter(self) -> None:
        """解体标签 → 打开舰船类型过滤器。"""
        _log.info('[UI] 建造页面 (解体) → 打开类型过滤')
        self._ctrl.click(*CLICK_DESTROY_TYPE_FILTER)

    def destroy_confirm_filter(self) -> None:
        """解体标签 → 确认舰船类型过滤。"""
        _log.info('[UI] 建造页面 (解体) → 确认过滤')
        self._ctrl.click(*CLICK_DESTROY_CONFIRM_FILTER)

    # ── 组合动作 — 建造收取 ──

    def dismiss_build_result(self) -> None:
        """处理建造完成后获取舰船的动画/弹窗。"""
        get_ship_templates = [Templates.Symbol.GET_SHIP, Templates.Symbol.GET_ITEM]
        for _ in range(10):
            screen = self._ctrl.screenshot()
            if not ImageChecker.template_exists(screen, get_ship_templates):
                break
            self.dismiss_animation()
            time.sleep(0.5)
            screen = self._ctrl.screenshot()
            detail = ImageChecker.find_any(screen, Templates.Confirm.all())
            if detail is not None:
                self._ctrl.click(*detail.center)
                time.sleep(0.5)

    def collect_all(
        self,
        build_type: str = 'ship',
        *,
        allow_fast_build: bool = False,
    ) -> int:
        """收取已建造完成的舰船或装备。

        必须已在建造页面对应标签上。

        Returns
        -------
        int
            收取数量。

        Raises
        ------
        RuntimeError
            仓库已满。
        """
        collected = 0

        if allow_fast_build:
            fast_tmpl = (
                Templates.Build.SHIP_FAST if build_type == 'ship' else Templates.Build.EQUIP_FAST
            )
            for _ in range(4):
                screen = self._ctrl.screenshot()
                detail = ImageChecker.find_template(screen, fast_tmpl)
                if detail is None:
                    break
                self._ctrl.click(*detail.center)
                time.sleep(0.3)
                screen = self._ctrl.screenshot()
                confirm = ImageChecker.find_any(screen, Templates.Confirm.all())
                if confirm is not None:
                    self._ctrl.click(*confirm.center)
                    time.sleep(1.0)

        complete_tmpl = (
            Templates.Build.SHIP_COMPLETE
            if build_type == 'ship'
            else Templates.Build.EQUIP_COMPLETE
        )
        full_depot_tmpl = (
            Templates.Build.SHIP_FULL_DEPOT
            if build_type == 'ship'
            else Templates.Build.EQUIP_FULL_DEPOT
        )

        for _ in range(4):
            screen = self._ctrl.screenshot()
            detail = ImageChecker.find_template(screen, complete_tmpl)
            if detail is None:
                break
            if ImageChecker.template_exists(screen, full_depot_tmpl):
                raise RuntimeError(f'{build_type} 仓库已满')
            self._ctrl.click(*detail.center)
            time.sleep(1.0)
            self.dismiss_build_result()
            collected += 1

        _log.info('[UI] 建造收取: {} 艘 ({})', collected, build_type)
        return collected

    def start_new_build(self, build_type: str = 'ship') -> None:
        """在当前标签启动一次新建造。

        Raises
        ------
        RuntimeError
            队列已满或资源选择页面未出现。
        """
        start_tmpl = (
            Templates.Build.SHIP_START if build_type == 'ship' else Templates.Build.EQUIP_START
        )
        screen = self._ctrl.screenshot()
        detail = ImageChecker.find_template(screen, start_tmpl)
        if detail is None:
            raise RuntimeError(f'{build_type} 建造队列已满')

        self._ctrl.click(*detail.center)
        time.sleep(1.0)

        resource_tmpl = Templates.Build.RESOURCE
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            screen = self._ctrl.screenshot()
            if ImageChecker.template_exists(screen, resource_tmpl):
                break
            time.sleep(0.3)
        else:
            raise RuntimeError('资源选择页面未出现')

        self._ctrl.click(*CLICK_CONFIRM_BUILD)
        time.sleep(1.0)
        _log.info('[UI] 建造已启动 ({})', build_type)

    def destroy_ships(
        self,
        ship_types: list[ShipType] | None = None,
        *,
        remove_equipment: bool = True,
    ) -> None:
        """在解体标签上执行完整解装流程。

        Parameters
        ----------
        ship_types:
            要解体的舰种列表。
            ``None`` (默认) 表示不过滤舰种，直接快速选择全部。
            传入非空列表时，先打开舰种过滤器按舰种筛选，再执行后续操作。
        remove_equipment:
            是否在解装前卸下装备。默认 ``True``。
        """
        # 在任何 UI 操作前验证坐标，避免新增 OCR 舰种导致筛选中途失败。
        type_positions = [t.relative_position_in_destroy for t in ship_types or []]
        _step_delay = 1.5

        self.destroy_click_add()
        time.sleep(_step_delay)

        if ship_types:
            # 按舰种过滤：打开过滤器 → 勾选各舰种 → 确认
            self.destroy_open_type_filter()
            time.sleep(_step_delay)
            for ship_type, position in zip(ship_types, type_positions, strict=True):
                _log.debug('[UI] 解体 → 点击舰种: {}', ship_type.value)
                self._ctrl.click(*position)
                time.sleep(0.8)
            self.destroy_confirm_filter()
            time.sleep(_step_delay)

        self.destroy_quick_select()
        time.sleep(_step_delay)
        self.destroy_confirm_select()
        time.sleep(_step_delay)
        # ── 无条件收敛「卸下装备」复选框 ──
        # 该复选框是**游戏内的持久状态**, 不随页面切换重置。此前写成
        # `if remove_equipment: toggle()` 有两个失效路径(2026-10-12 修复):
        #   1. 连续两轮解装 → 第二轮把勾选**取消**掉 → 装备随舰船不可逆销毁;
        #   2. 配置改为 False 但上一轮是 True → 残留勾选态生效, 配置被静默忽略。
        # 必须无条件收敛到目标态, 且不能放在任何提前 return 之后。
        if remove_equipment:
            self.ensure_remove_equip()
        else:
            self.disable_remove_equip()
        time.sleep(_step_delay)
        self.destroy_confirm()
        time.sleep(_step_delay)
        self.destroy_four_star_confirm()
        time.sleep(_step_delay)

        _log.info(
            '[UI] 解装完成 (舰种={}, 卸下装备={})',
            [t.value for t in ship_types] if ship_types else '全部',
            remove_equipment,
        )
