"""诊断：用框架控制器抓取当前帧，检查 MAIN 像素签名，并落盘供分析。

用法 (在项目任意位置):
    python tools/diagnostics/diag_capture.py
产物:
    logs/diagnostics/diag_frame.npy / .png  (供 diag_grid.py 使用)
"""
import numpy as np
from PIL import Image

from _paths import FRAME_NPY, FRAME_PNG, default_config, ensure_diag_dir, ensure_project_on_path


ensure_project_on_path()

from autowsgr.infra import ConfigManager
from autowsgr.emulator import ScrcpyController

cfg = ConfigManager.load(default_config())
print('serial =', cfg.emulator.serial)
ctrl = ScrcpyController(serial=cfg.emulator.serial, config=cfg.emulator)
info = ctrl.connect()
print('connect info res =', info.resolution)

frame = ctrl.screenshot()
print('FRAME shape =', frame.shape, 'dtype =', frame.dtype)
ensure_diag_dir()
np.save(FRAME_NPY, frame)
Image.fromarray(frame).save(FRAME_PNG)
print('saved', FRAME_NPY, '/', FRAME_PNG)

# MAIN 像素签名规则 (autowsgr/ui/main_page/constants.py)
# PixelRule.of(0.6453, 0.9375, (52,115,168), tol=30)
# PixelRule.of(0.8126, 0.8681, (213,206,180), tol=30)
# PixelRule.of(0.9696, 0.8903, (121,130,135), tol=30)
# PixelRule.of(0.0570, 0.8847, (251,252,255), tol=30)
rules = [
    (0.6453, 0.9375, (52, 115, 168), 30.0),
    (0.8126, 0.8681, (213, 206, 180), 30.0),
    (0.9696, 0.8903, (121, 130, 135), 30.0),
    (0.0570, 0.8847, (251, 252, 255), 30.0),
]
h, w = frame.shape[:2]
print(f'frame {w}x{h}')
for i, (xf, yf, color, tol) in enumerate(rules, 1):
    px = int(round(xf * (w - 1)))
    py = int(round(yf * (h - 1)))
    got = frame[py, px].tolist()
    dist = np.sqrt(sum((a - b) ** 2 for a, b in zip(got, color)))
    ok = dist <= tol
    print(f'  rule{i}: px=({px},{py}) expect={color} got={got} dist={dist:.1f} tol={tol} -> {"OK" if ok else "FAIL"}')

# 粗略判断当前页面：打印四角与中心区块平均色
def block_avg(y0, y1, x0, x1):
    return np.mean(frame[y0:y1, x0:x1], axis=(0, 1)).astype(int).tolist()

print('corners/top/bottom avg color:')
print('  top    ', block_avg(0, 20, 0, w))
print('  bottom ', block_avg(h - 20, h, 0, w))
print('  left   ', block_avg(0, h, 0, 20))
print('  right  ', block_avg(0, h, w - 20, w))
print('  center ', block_avg(h // 2 - 20, h // 2 + 20, w // 2 - 20, w // 2 + 20))
ctrl.disconnect()
