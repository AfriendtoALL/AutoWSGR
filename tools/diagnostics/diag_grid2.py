"""渲染 ADB 真帧的粗色块网格，识别当前页面。

用法:
    python tools/diagnostics/diag_grid2.py
前置: logs/diagnostics/adb_frame.png 已存在 (由 adb exec-out screencap 落地)。
"""
import numpy as np
from PIL import Image

from _paths import ADB_FRAME_PNG

a = np.array(Image.open(ADB_FRAME_PNG).convert('RGB'))
h, w = a.shape[:2]
print(f'frame {w}x{h}')

cols, rows = 48, 27

def classify(rgb):
    r, g, b = rgb
    if r > 230 and g > 230 and b > 230:
        return 'W'
    if r < 60 and g < 60 and b < 60:
        return '#'
    if r > 180 and g < 120 and b < 120:
        return 'R'
    if g > 180 and r < 120 and b < 120:
        return 'G'
    if b > 180 and r < 120 and g < 120:
        return 'B'
    if r > 180 and g > 180 and b < 140:
        return 'Y'
    if r > 180 and g > 150 and b > 150:
        return '.'
    if abs(r - g) < 25 and abs(g - b) < 25 and r < 120:
        return ':'
    if abs(r - g) < 25 and abs(g - b) < 25:
        return '-'
    return '?'

for ry in range(rows):
    y0 = ry * h // rows
    y1 = (ry + 1) * h // rows
    line = []
    for rx in range(cols):
        x0 = rx * w // cols
        x1 = (rx + 1) * w // cols
        avg = a[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0).astype(int)
        line.append(classify(avg.tolist()))
    print(''.join(line))

step = 32
quant = (a // step * step).reshape(-1, 3)
uniq, counts = np.unique(quant, axis=0, return_counts=True)
order = np.argsort(-counts)[:12]
print('\nTop-12 量化颜色 (RGB, 占比%):')
for idx in order:
    c = uniq[idx].tolist()
    pct = 100.0 * counts[idx] / len(quant)
    print(f'  {c}  {pct:.1f}%')
