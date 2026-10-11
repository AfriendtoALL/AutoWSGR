"""出征准备页面 UI 控制器。

最终控制器类 :class:`BattlePreparationPage` 通过菱形继承聚合所有 Mixin:

继承结构::

    BaseBattlePreparation
    ├── SupplyMixin           (补给 / 支援)
    └── FleetChangeMixin         (换船主流程编排)
        └── FleetAlignmentMixin  (成员更换与位置调整)
            └── FleetSelectionMixin  (船池选择页面操作)
                └── FleetPlanningMixin  (目标分配与槽位规划)
                    └── FleetRuleMixin  (舰队规则匹配与校验)
                        └── FleetDetectMixin  (准备页舰队 OCR 检测)
            ↓
    BattlePreparationPage     (最终控制器)

.. note::
    已移除出征准备页的血量识别 / 舰船等级识别 / 舰船种类识别 (原
    ``DetectionMixin``) 与自动修理 (原 ``RepairMixin``)。血量统一视作
    正常，修理由用户手动完成。

数据常量见 :mod:`autowsgr.ui.battle.constants`。
"""

from __future__ import annotations

from autowsgr.ui.battle.base import (
    CLICK_PANEL,
    PAGE_SIGNATURE,
    PANEL_PROBE,
    BaseBattlePreparation,
    Panel,
    RepairStrategy,
)
from autowsgr.ui.battle.fleet_change import FleetChangeMixin
from autowsgr.ui.battle.supply import SupplyMixin


__all__ = [
    'CLICK_PANEL',
    'PAGE_SIGNATURE',
    'PANEL_PROBE',
    'BattlePreparationPage',
    'Panel',
    'RepairStrategy',
]


class BattlePreparationPage(
    SupplyMixin,
    FleetChangeMixin,
    BaseBattlePreparation,
):
    """出征准备页面控制器。

    聚合所有 Mixin 能力:

    - **补给/支援** (:class:`SupplyMixin`):
      ``supply``, ``apply_supply``, ``toggle_battle_support``, ``is_support_enabled``
    - **换船** (:class:`FleetChangeMixin`):
      ``change_fleet``
    - **基础** (:class:`BaseBattlePreparation`):
      ``go_back``, ``start_battle``, ``select_fleet``, ``select_panel`` 等

    重构后不再包含检测与自动修理能力。
    """
