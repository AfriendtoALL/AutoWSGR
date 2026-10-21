"""diag_ammo.py — 诊断: 进入 H5 出征准备页, 截图保存 + 弹药检测逐槽采样。

只进准备页, 不点击出征; 采样后立即退出 (游戏停在准备页无妨)。

用法:
    python tools/diagnostics/diag_ammo.py
"""
import sys
import time

import cv2
import numpy as np

from _paths import DIAG_DIR, default_config, ensure_diag_dir, ensure_project_on_path


ensure_project_on_path()

from autowsgr.scheduler import launch
from autowsgr.ops.navigate import goto_page
from autowsgr.types import PageName
from autowsgr.ui.event.event_page import BaseEventPage
from autowsgr.ui.battle.preparation import BattlePreparationPage
from autowsgr.ui.battle.ammo import detect_ammo_empty
from autowsgr.ui.battle.constants import (
    AMMO_BAR_X_RANGES,
    AMMO_BAR_Y1,
    AMMO_BAR_Y2,
    AMMO_TOLERANCE,
)

OUT = str(DIAG_DIR / 'ammo_prep_now.png')


def ratio_profile(screen: np.ndarray) -> None:
    """逐槽输出黄/灰/蓝/绿四色像素占比。"""
    h, w = screen.shape[:2]
    rgb = screen[..., ::-1].astype(np.int32)

    def ratio(color, tol=AMMO_TOLERANCE):
        diff = rgb - np.array(color, dtype=np.int32)
        dist = np.sqrt(np.einsum("ijk,ijk->ij", diff, diff))
        return dist <= tol

    yellow = ratio((234, 173, 31))
    gray = ratio((139, 140, 140))
    blue = ratio((14, 140, 229))
    green = ratio((64, 161, 109))
    y1, y2 = int(AMMO_BAR_Y1 * h), int(AMMO_BAR_Y2 * h)
    print(f"rows y={y1}..{y2}")
    for slot, (x0, x1) in enumerate(AMMO_BAR_X_RANGES):
        sl = np.s_[y1:y2, int(x0 * w) : int(x1 * w)]
        print(
            f"slot{slot}: yellow={yellow[sl].mean():.3f} gray={gray[sl].mean():.3f} "
            f"blue={blue[sl].mean():.3f} green={green[sl].mean():.3f}"
        )
    # 整行 (y 346-364) 六槽宽段的蓝/灰占比 (修正后的候选坐标)
    for slot, (xa, xb) in enumerate(
        [(0.1083, 0.151), (0.225, 0.2677), (0.3417, 0.3844), (0.4583, 0.5), (0.574, 0.6167), (0.7, 0.7333)]
    ):
        sl = np.s_[346:364, int(xa * w) : int(xb * w)]
        print(
            f"wide slot{slot}: gray={gray[sl].mean():.3f} blue={blue[sl].mean():.3f} "
            f"green={green[sl].mean():.3f} yellow={yellow[sl].mean():.3f}"
        )


def main() -> None:
    ensure_diag_dir()
    ctx = launch(default_config())
    goto_page(ctx, PageName.EVENT_MAP)
    time.sleep(0.25)
    page = BaseEventPage(ctx, event_name="20260930")
    page.start_fight("H5", None, False)
    time.sleep(1.5)

    screen = ctx.ctrl.screenshot()
    arr = np.asarray(screen)
    print("screen:", arr.shape)
    cv2.imwrite(OUT, arr[..., ::-1] if arr.ndim == 3 else arr)
    print("saved:", OUT)

    print("== as-is (assume BGR input) ==")
    ratio_profile(arr)
    print("== flipped (if input was RGB) ==")
    ratio_profile(arr[..., ::-1])


if __name__ == "__main__":
    sys.exit(main())
