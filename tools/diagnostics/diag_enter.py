"""定位『进入游戏』按钮并打印其中心像素坐标 (ADB tap 用)。

用法:
    python tools/diagnostics/diag_enter.py
前置: logs/diagnostics/adb_frame.png 已存在。
"""
import numpy as np
from PIL import Image
import easyocr

from _paths import ADB_FRAME_PNG

img = Image.open(ADB_FRAME_PNG).convert('RGB')
arr = np.array(img)
reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)
res = reader.readtext(arr, detail=1, paragraph=False)
print('--- 文本块 (bbox 左上/右下) ---')
for (bbox, text, conf) in res:
    if conf < 0.3:
        continue
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    cx, cy = int((min(xs) + max(xs)) / 2), int((min(ys) + max(ys)) / 2)
    print(f'[{conf:.2f}] {text!r} center=({cx},{cy}) bbox_x[{int(min(xs))},{int(max(xs))}] bbox_y[{int(min(ys))},{int(max(ys))}]')
