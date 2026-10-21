"""把当前帧降采样成粗色块网格，用文本方式"看"屏幕布局。

用法:
    python tools/diagnostics/diag_capture.py   # 先抓帧
    python tools/diagnostics/diag_grid.py      # 再渲染网格
"""
import numpy as np

from _paths import FRAME_NPY

frame = np.load(FRAME_NPY)
h, w = frame.shape[:2]
print(f'frame {w}x{h}')

# 粗网格 48 列 x 27 行
cols, rows = 48, 27
# 颜色归类
def classify(rgb):
    r, g, b = rgb
    if r > 230 and g > 230 and b > 230:
        return 'W'   # 白
    if r < 60 and g < 60 and b < 60:
        return '#'   # 黑
    if r > 180 and g < 120 and b < 120:
        return 'R'   # 红
    if g > 180 and r < 120 and b < 120:
        return 'G'   # 绿
    if b > 180 and r < 120 and g < 120:
        return 'B'   # 蓝
    if r > 180 and g > 180 and b < 140:
        return 'Y'   # 黄
    if r > 180 and g > 150 and b > 150:
        return '.'   # 浅(米白)
    if abs(r - g) < 25 and abs(g - b) < 25 and r < 120:
        return ':'   # 深灰
    if abs(r - g) < 25 and abs(g - b) < 25:
        return '-'   # 灰
    return '?'       # 其他

grid_chars = []
for ry in range(rows):
    y0 = ry * h // rows
    y1 = (ry + 1) * h // rows
    line = []
    for rx in range(cols):
        x0 = rx * w // cols
        x1 = (rx + 1) * w // cols
        avg = frame[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0).astype(int)
        line.append(classify(avg.tolist()))
    grid_chars.append(''.join(line))

print('\n'.join(grid_chars))

# 另外：统计整体色调分布
step = 32
quant = (frame // step * step).reshape(-1, 3)
uniq, counts = np.unique(quant, axis=0, return_counts=True)
order = np.argsort(-counts)[:12]
print('\nTop-12 量化颜色 (RGB, 占比%):')
for idx in order:
    c = uniq[idx].tolist()
    pct = 100.0 * counts[idx] / len(quant)
    print(f'  {c}  {pct:.1f}%')
