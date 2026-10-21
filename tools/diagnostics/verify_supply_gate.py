"""verify_supply_gate.py — 实机验证: 40% 弹药时 apply_supply 必须跳过。

游戏当前应停在出征准备页 (diag_ammo.py 留下, 弹药 2 黄 3 灰 = 40%)。
预期日志: 「弹药未全空, 跳过补给 (仅全空才补给)」, 且零点击、页面不变。

用法:
    python tools/diagnostics/verify_supply_gate.py
"""
import time

import cv2
import numpy as np

from _paths import DIAG_DIR, default_config, ensure_diag_dir, ensure_project_on_path


ensure_project_on_path()

from autowsgr.scheduler import launch
from autowsgr.ui.battle.preparation import BattlePreparationPage

OUT = str(DIAG_DIR / 'supply_gate_after.png')


def main() -> None:
    ensure_diag_dir()
    ctx = launch(
        default_config(),
        ensure_game=False,
    )
    page = BattlePreparationPage(ctx)

    state = page.is_fleet_ammo_empty()
    print(f"\n>>> is_fleet_ammo_empty() = {state}  (期望 False = 未全空)")

    # 「零点击」验证: 包装 click 计数, 直接数 apply_supply 期间的实际点击数。
    # 原实现用 getattr(ctx.ctrl, "_click_count", None) 取一个全仓不存在的属性,
    # 永远拿到 None, 等于没验证。
    clicks: list[tuple[float, float]] = []
    original_click = ctx.ctrl.click

    def _counting_click(x: float, y: float, **kwargs):
        clicks.append((x, y))
        return original_click(x, y, **kwargs)

    ctx.ctrl.click = _counting_click
    page.apply_supply()
    ctx.ctrl.click = original_click
    print(f">>> apply_supply() 期间点击数 = {len(clicks)}  (期望 0)")
    print(">>> apply_supply() 执行完毕 (期望日志: 跳过补给)")

    time.sleep(1.0)
    screen = np.asarray(ctx.ctrl.screenshot())
    cv2.imwrite(OUT, screen[..., ::-1])
    print(f">>> 已保存验证后截图: {OUT}")


if __name__ == "__main__":
    main()
