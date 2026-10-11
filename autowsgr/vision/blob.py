"""连通域分析 — cv2 的高层封装 (2026-10-11 T3-2)。

背景
----
``combat/node_tracker.py`` 直接 ``import cv2`` 调用
``connectedComponentsWithStats``, 让战斗域绕过 :mod:`autowsgr.vision`
抽象层用第三方库 —— 与项目里"识别一律走 vision"的分层约定不符。

不过度封装: 业务真正需要的是「二值掩码里最大的连通块在哪、有多大」,
而不是完整的连通域 API。故这里只暴露这一个语义, 保留 ``cv2`` 作为
实现细节。

.. code-block:: python

    from autowsgr.vision import largest_blob

    blob = largest_blob(mask, min_area=200)
    if blob is None:
        return None
    cx, cy = blob.centroid
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class Blob:
    """一个连通块的描述。"""

    label: int
    """连通块编号 (0 是背景, 不使用)。"""

    area: int
    """像素面积。"""

    centroid: tuple[float, float]
    """重心 ``(x, y)``, 浮点像素坐标。"""

    bounding_box: tuple[int, int, int, int]
    """外接矩形 ``(left, top, width, height)``。"""


def largest_blob(
    mask: np.ndarray,
    *,
    min_area: int = 1,
    connectivity: int = 8,
) -> Blob | None:
    """取二值掩码中面积最大的连通块。

    Parameters
    ----------
    mask:
        二值掩码 (非 0 即为前景)。不需是 ``uint8``, 内部会转换。
    min_area:
        最小有效面积, 低于此值视为噪声返回 ``None``。
    connectivity:
        连通性 (4 或 8), 默认 8。

    Returns
    -------
    Blob | None
        最大连通块; 掩码为空或全被 ``min_area`` 过滤时返回 ``None``。
    """
    if mask is None or mask.size == 0:
        return None

    binary = (np.asarray(mask) > 0).astype(np.uint8)
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary,
        connectivity=connectivity,
    )

    # label 0 是背景, 从 1 开始
    best_label = -1
    best_area = 0
    for label_id in range(1, num_labels):
        area = int(stats[label_id, cv2.CC_STAT_AREA])
        if area >= min_area and area > best_area:
            best_area = area
            best_label = label_id

    if best_label < 0:
        return None

    left, top, width, height = (int(v) for v in stats[best_label, :4])
    cx, cy = (float(v) for v in centroids[best_label])

    return Blob(
        label=best_label,
        area=best_area,
        centroid=(cx, cy),
        bounding_box=(left, top, width, height),
    )