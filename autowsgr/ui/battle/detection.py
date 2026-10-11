"""出征准备 — 舰船状态检测与舰队信息识别。

.. note::
    已按需求移除以下识别能力:

    - 舰船血量识别 (``detect_ship_damage`` / ``classify_blood``)
    - 舰船等级识别 (``_recognize_fleet_levels``)
    - 舰船舰种识别 (``_recognize_fleet_ship_types``)
    - 舰队信息聚合 (``detect_fleet_info`` / ``FleetInfo``)

    相关能力改由配置或手动处理；仅保留换船用的舰名 OCR (见
    :mod:`autowsgr.ui.battle.fleet_change`)。
"""
