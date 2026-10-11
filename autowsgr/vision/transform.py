"""通用图像预处理 (2026-10-11 T3-2 步骤 5)。

下沉自 ``combat/recognition.py`` —— 那里用 PIL 做「转灰度 + 缩放」,
让战斗域直接依赖了 PIL (项目里的第二套图像库; vision 全模块统一用 cv2)。

这类组合是纯粹的图像预处理, 归 vision 层。连通域分析见
:mod:`autowsgr.vision.blob`。
"""

from __future__ import annotations

import cv2
import numpy as np


def to_gray_resized(
    image: np.ndarray,
    size: tuple[int, int],
    *,
    interpolation: int | None = None,
) -> np.ndarray:
    """转灰度并缩放到指定尺寸。

    替代原先的 ``PIL.Image.fromarray(screen).convert('L').resize(size)``。

    Parameters
    ----------
    image:
        彩色 (HWC, RGB) 或已是灰度 (HW) 的图。
    size:
        目标 ``(宽, 高)`` —— 注意顺序与 cv2 一致 (与 PIL 的 resize 相反)。
    interpolation:
        cv2 插值方式; ``None`` 时按缩放方向自动选 (缩小用 ``INTER_AREA``
        抗锯齿更好, 放大用 ``INTER_LINEAR``)。

    Returns
    -------
    np.ndarray
        ``uint8`` 灰度图, 形状为 ``(size[1], size[0])``。
    """
    if image is None or getattr(image, 'size', 0) == 0:
        raise ValueError('输入图像为空')

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    src_h, src_w = gray.shape[:2]
    if (src_w, src_h) == tuple(size):
        return gray

    if interpolation is None:
        interpolation = cv2.INTER_AREA if size[0] < src_w else cv2.INTER_LINEAR

    return cv2.resize(gray, tuple(size), interpolation=interpolation)