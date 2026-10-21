"""对当前真帧做 OCR，识别当前页面文字。

用法:
    python tools/diagnostics/diag_ocr.py
前置: logs/diagnostics/adb_frame.png 已存在。
"""
import numpy as np
from PIL import Image
import easyocr

from _paths import ADB_FRAME_PNG

img = Image.open(ADB_FRAME_PNG).convert('RGB')
arr = np.array(img)
print('OCR on frame', arr.shape)
reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)
res = reader.readtext(arr, detail=1, paragraph=False)
print(f'--- {len(res)} 个文本块 ---')
for (bbox, text, conf) in res:
    if conf > 0.3:
        print(f'[{conf:.2f}] {text}')
