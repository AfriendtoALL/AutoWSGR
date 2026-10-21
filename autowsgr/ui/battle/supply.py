"""出征准备 — 补给与战役支援。

提供补给 / 支援状态查询与切换操作。

补给触发条件 (:meth:`SupplyMixin.apply_supply`): **任一舰船弹药全空**
(该船 5 格子弹全灰) 即补给 (基于 :func:`autowsgr.ui.battle.ammo.detect_ammo_empty`
弹药子弹检测)；所有舰船尚有弹药时跳过；**检测失败/状态未知时同样跳过**
(用户约定 2026-10-08: 宁可漏补不可误补 —— 旧兜底"未知即补给"曾把 40%
弹药的舰队误补。2026-10-10 起触发阈值从"全舰队全空"放宽为"任一舰船
全空", 用户指令)。

补给方式由配置 :attr:`~autowsgr.infra.config.UserConfig.supply_mode` 控制
(:meth:`SupplyMixin.apply_supply` 读取):

- ``'auto'``   : 勾选游戏内「自动补给」复选框，由游戏在出击时自动补给。
- ``'script'`` : **先取消**游戏内「自动补给」勾选，再打开「快速补给」面板
  点击「全部补给」按钮 (即 ``CLICK_SUPPLY_ALL``)。原逐舰船点击的补给规则已
  废弃，统一改为点击右侧「全部补给」。

⚠️ **脚本模式每轮校正复选框状态** (2026-10-12 修复)：复选框是游戏内的
持久状态，不随页面切换重置。此前 ``'script'`` 模式从不碰它，导致上一轮
``'auto'`` 模式留下的勾选态持续生效 —— 脚本补完弹药后，出击时游戏再自动补
一次，既白耗燃料，又让「按需补给」的语义彻底失效。详见
:meth:`SupplyMixin.disable_auto_supply`。

``'auto'`` 模式则在首次校正成功后把结果缓存到
:attr:`~autowsgr.context.game_context.GameContext.supply_state`，同一会话
后续战斗直接跳过识别 (2026-10-21 优化)：复选框不会自行改变，游戏会在出击
时自动补给，脚本无需每场重复截图确认。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from autowsgr.context.supply import SupplyState
from autowsgr.infra.logger import get_logger
from autowsgr.ui.battle.base import BaseBattlePreparation, Panel
from autowsgr.ui.battle.constants import (
    CLICK_SUPPLY_ALL,
    CLICK_SUPPORT,
    SUPPORT_DISABLE,
    SUPPORT_ENABLE,
    SUPPORT_EXHAUSTED,
    SUPPORT_PROBE,
)
from autowsgr.vision import PixelChecker


if TYPE_CHECKING:
    import numpy as np


_log = get_logger('ui.preparation')

# 补给方式常量
SUPPLY_MODE_AUTO = 'auto'
"""勾选游戏内自动补给复选框。"""
SUPPLY_MODE_SCRIPT = 'script'
"""脚本手动点击快速补给 → 全部补给。"""


class SupplyMixin(BaseBattlePreparation):
    """补给与战役支援 Mixin。

    依赖 :class:`~autowsgr.ui.battle.base.BaseBattlePreparation` 提供的
    ``_ctrl``, ``select_panel``, ``is_auto_supply_enabled``。
    """

    # ── 状态查询 — 战役支援 ──────────────────────────────────────────────

    @staticmethod
    def is_support_enabled(screen: np.ndarray) -> bool:
        """检测战役支援是否启用。灰色 (次数用尽) 也视为已启用。"""
        x, y = SUPPORT_PROBE
        pixel = PixelChecker.get_pixel(screen, x, y)
        d_enable = pixel.distance(SUPPORT_ENABLE)
        d_disable = pixel.distance(SUPPORT_DISABLE)
        d_exhausted = pixel.distance(SUPPORT_EXHAUSTED)
        if d_enable > d_exhausted and d_disable > d_exhausted:
            return True
        return d_enable < d_disable

    # ── 动作 — 开关 ──────────────────────────────────────────────────────

    def toggle_battle_support(self) -> None:
        """切换战役支援开关。"""
        _log.debug('[UI] 出征准备 → 切换战役支援')
        self._ctrl.click(*CLICK_SUPPORT)

    # ── 动作 — 补给 ──────────────────────────────────────────────────────

    def ensure_auto_supply(self) -> None:
        """确保游戏内「自动补给」复选框已勾选 (开启)。

        若当前未开启则点击复选框开启；已开启则跳过，避免重复操作。
        """
        screen = self._ctrl.screenshot()
        if self.is_auto_supply_enabled(screen):
            _log.debug('[UI] 出征准备 → 自动补给已开启, 跳过')
            return
        _log.debug('[UI] 出征准备 → 勾选自动补给')
        self.toggle_auto_supply()

    def disable_auto_supply(self) -> None:
        """确保游戏内「自动补给」复选框处于**未勾选**状态。

        与 :meth:`ensure_auto_supply` 对称，是脚本补给模式的**前置条件**。

        为什么必须显式取消 (2026-10-12 用户指出)
        ----------------------------------------
        复选框是**游戏内的持久状态**，不随页面切换重置。若上一轮用了
        ``'auto'`` 模式、或用户在游戏里手动勾选过，之后切到 ``'script'``
        模式时复选框仍是勾选态 —— 此时脚本补完弹药，出击时游戏会
        **再自动补给一次**，白白多耗燃料，且「按需补给」的语义彻底失效。

        与 :meth:`ensure_auto_supply` 一样是幂等的：已是目标状态则不点击。
        """
        screen = self._ctrl.screenshot()
        if not self.is_auto_supply_enabled(screen):
            _log.debug('[UI] 出征准备 → 自动补给已关闭, 跳过')
            return
        _log.info('[UI] 出征准备 → 取消自动补给勾选 (脚本补给模式下必须关闭)')
        self.toggle_auto_supply()

    def supply_all(self) -> None:
        """脚本手动补给: 打开「快速补给」面板并点击「全部补给」。

        .. note::
            原逐舰船点击的补给规则已废弃，统一改为点击面板右侧的
            「全部补给」按钮 (``CLICK_SUPPLY_ALL``)，对全舰队一次性补给。
        """
        self.select_panel(Panel.QUICK_SUPPLY)
        time.sleep(0.5)
        self._ctrl.click(*CLICK_SUPPLY_ALL)
        time.sleep(0.5)
        _log.debug('[UI] 出征准备 → 全部补给 (脚本补给)')

    def supply(self, ship_ids: list[int] | None = None) -> None:
        """脚本手动补给 (全部)。

        .. deprecated::
            原逐舰船点击逻辑已废弃；现在直接调用 :meth:`supply_all`
            (打开快速补给面板 → 点击「全部补给」)。``ship_ids`` 参数保留以兼容
            调用方签名，但「全部补给」会一次性补给全舰队，不再按槽位逐个处理。
        """
        if ship_ids is not None:
            _log.debug('[UI] 出征准备 → 脚本补给忽略 ship_ids, 统一全部补给')
        self.supply_all()

    def has_empty_ammo_ship(self) -> bool | None:
        """检测准备页是否存在弹药已耗尽 (5 格子弹全灰) 的舰船。

        对准备页当前截图做弹药子弹颜色检测 (见
        :func:`autowsgr.ui.battle.ammo.detect_ammo_empty`)，忽略无舰船的空槽位。

        检测失败 (未识别到任何弹药子弹) 等 0.6s 重试, 最多 3 次: 页面动画/
        弹层未完全展示时首帧常截不到子弹行, 单次失败即判定"状态未知"会造成
        误跳过 (漏补); 重试仍失败才视为未知。

        Returns
        -------
        bool | None
            ``True``  = 至少一艘在队舰船弹药全空 (满足补给条件);
            ``False`` = 所有在队舰船尚有弹药 (不满足);
            ``None``  = 三次检测均未识别到弹药子弹或检测过程异常，状态未知。
        """
        from autowsgr.ui.battle.ammo import detect_ammo_empty

        for attempt in (1, 2, 3):
            try:
                screen = self._ctrl.screenshot()
                states = detect_ammo_empty(screen)
            except Exception as exc:
                _log.warning('[UI] 弹药检测异常 (第 {} 次) ({}), 视为状态未知', attempt, exc)
                states = []
            ships = [s for s in states if s is not None]
            if ships:
                return any(ships)
            if attempt < 3:
                _log.debug('[UI] 弹药检测未见子弹行 (第 {} 次), 0.6s 后重试', attempt)
                time.sleep(0.6)
        return None

    def _supply_state(self) -> SupplyState:
        """获取补给会话缓存；兼容未挂载该字段的测试替身。"""
        state = getattr(self._ctx, 'supply_state', None)
        if not isinstance(state, SupplyState):
            state = SupplyState()
            try:
                self._ctx.supply_state = state
            except AttributeError:
                pass
        return state

    def apply_supply(self, *, mode: str | None = None, force: bool = False) -> None:
        """按需补给——**任一舰船弹药全空**即补给 (2026-10-10 用户指令)。

        - ``'auto'``   : 勾选游戏内自动补给复选框 (见 :meth:`ensure_auto_supply`),
                        由游戏在出击时自动补给。首次校正成功后在本会话内缓存,
                        后续战斗不再重复识别。
        - ``'script'`` : **先取消**自动补给勾选 (见 :meth:`disable_auto_supply`),
                        再按需点击「全部补给」。每场都会重新校正复选框, 防止
                        上一模式或用户手动操作留下的勾选态。

        为什么脚本模式必须在**判断是否需要补给之前**校正复选框
        ------------------------------------------------------
        复选框是**游戏内的持久状态**, 不随页面切换重置。若不在每轮显式校正,
        上一轮 ``'auto'`` 模式留下的勾选态会持续生效 —— 此时脚本补完弹药,
        出击时游戏再自动补一次, 「按需补给」的语义彻底失效 (2026-10-12 用户指出)。

        ``'auto'`` 模式下弹药检测没有意义: 游戏会在出击时自动补给, 脚本无须
        关心当前弹药状态。因此本方法在确保复选框后立即返回, 不再调用
        :meth:`has_empty_ammo_ship` (2026-10-21 优化)。

        脚本模式的弹药检测 (:meth:`has_empty_ammo_ship`) 三种结果:

        - 存在全空船 → 执行补给 (全部补给会一并补满其他船);
        - 所有船尚有弹药 → 跳过补给并记 INFO 日志;
        - 检测失败 (状态未知) → **同样跳过补给** 并记 WARNING —— 用户约定
          (2026-10-08): 宁可漏补不可误补 (旧"未知即补给"兜底曾在 40% 弹药
          时误补)。连续未知请检查页面状态或截图日志。

        Parameters
        ----------
        mode:
            显式指定补给方式，可选 ``'auto'`` / ``'script'``。缺省时读取配置
            :attr:`~autowsgr.infra.config.UserConfig.supply_mode`
            (默认 ``'script'``)。
        force:
            忽略会话缓存，强制重新识别并校正复选框。仅影响 ``'auto'`` 模式;
            ``'script'`` 模式本来就每场校正。
        """
        cfg = getattr(self._ctx, 'config', None)
        resolved = mode or getattr(cfg, 'supply_mode', None) or SUPPLY_MODE_SCRIPT
        if resolved not in (SUPPLY_MODE_AUTO, SUPPLY_MODE_SCRIPT):
            _log.warning('[UI] 未知补给方式 {!r}, 回退为脚本补给', resolved)
            resolved = SUPPLY_MODE_SCRIPT

        state = self._supply_state()

        # ── auto: 游戏在出击时自动补给, 脚本只需保证复选框一次 ──
        if resolved == SUPPLY_MODE_AUTO:
            if force or not state.checkbox_correct_for(resolved):
                state.invalidate()  # 校正失败时下次仍会重试
                self.ensure_auto_supply()
                state.mark_checkbox_correct(resolved)
            else:
                _log.debug('[UI] 自动补给复选框已确认, 跳过识别')
            return

        # ── script: 保持每场校正, 防止上一模式/手动操作留下勾选态 ──
        state.invalidate()
        self.disable_auto_supply()
        state.mark_checkbox_correct(resolved)

        ammo_state = self.has_empty_ammo_ship()
        if ammo_state is False:
            _log.info('[UI] 无空弹药船, 跳过补给 (任一舰船弹药全空才补给)')
            return
        if ammo_state is None:
            _log.warning('[UI] 弹药状态未知 (未见子弹行), 跳过补给')
            return
        self.supply_all()
