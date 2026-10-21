"""弹药弹药量检测 (单点式)。

提供准备页六个舰船槽位弹药是否为空的像素检测函数。

**输入约定**: **RGB** 数组，与
:meth:`autowsgr.emulator.controller.protocol.AndroidController.screenshot`
返回值一致。

**单点检测 (2026-10-10, 用户方案)**: 弹药从右往左消耗 —— 第 1 颗子弹
(最左格) **最后熄灭**, 且低弹药警告时该格变红。因此**每船只需检测
第 1 格中心一个点**即可代表整船弹药状态:

- 采样点 **灰色** (低饱和、中亮度) → 该船弹药全空 (5 格全灰);
- 采样点 **彩色** (黄/蓝/红警告/绿, 任意高饱和) → 该船尚有弹药;
- 采样点 **非灰非彩** (空槽的深蓝面板 / 白色「+」) → 无舰船。

采样用 3×3 小邻域均值 (抗压缩噪声, 仍是"第 1 格中间一个点"的语义)。
检测点坐标由满弹图 vs 全灰图逐像素差分校准 (见
:data:`autowsgr.ui.battle.constants.AMMO_FIRST_CELL_POINTS`)。

历史教训 (供后续维护): 整行占比算法 + 检测段边界误差, 曾把低弹药时
唯一填充格 (行最左的红警告格) 切在框外导致误判全空 → 误补给
(2026-10-10); 更早的 BGR 通道反转 bug 导致 scrcpy 截图永远匹配失败
(2026-10-08)。单点方案从机制上消除边界敏感问题。
"""

from __future__ import annotations

import numpy as np

from autowsgr.infra.logger import get_logger
from autowsgr.ui.battle.constants import AMMO_FIRST_CELL_POINTS

_log = get_logger('ammo')

# 灰色 (空子弹) 判定: 低饱和 + 中亮度。
_GRAY_SATURATION_MAX: float = 40.0
_GRAY_VALUE_MIN: float = 90.0
_GRAY_VALUE_MAX: float = 210.0
# 彩色 (已填充子弹) 判定: 高饱和。
_FILL_SATURATION_MIN: float = 60.0
# 深色背景 (无船/空槽面板) 判定: 低亮度。
# 空槽面板实测 (34,81,123) val≈79; 正常弹药格 (灰 139 / 蓝 128 / 红 ~117
# / 黄 ~146) 亮度均 >= 117, 分界清晰。
_BACKGROUND_VALUE_MAX: float = 90.0


def _sample_mean(screen: np.ndarray, cx: int, cy: int) -> np.ndarray | None:
    """取 (cx, cy) 处 3×3 邻域的均值色; 出界返回 None。"""
    h, w = screen.shape[:2]
    if not (1 <= cx < w - 1 and 1 <= cy < h - 1):
        return None
    patch = screen[cy - 1:cy + 2, cx - 1:cx + 2]
    return patch.reshape(-1, 3).mean(axis=0)


def detect_ammo_empty(
    screen: np.ndarray,
    *,
    occupied: list[bool] | None = None,
) -> list[bool | None]:
    """检测准备页六个槽位弹药是否为空 (第 1 格单点判定)。

    Parameters
    ----------
    screen:
        准备页截图 (**RGB** 数组, 与 ``screenshot()`` 协议一致)。
    occupied:
        可选的长度为 6 的占用标志。提供时仅对占用槽位给出弹药状态，未占用槽位
        直接返回 ``None``；不提供时由采样点颜色自动推断 (深色背景 = 无船)。

    Returns
    -------
    list[bool | None]
        长度为 6，元素: ``True``=弹药为空 (第 1 格灰), ``False``=弹药非空
        (第 1 格彩色), ``None``=无舰船/不适用。
    """
    h, w = screen.shape[:2]
    result: list[bool | None] = [None] * 6
    states: list[str] = []
    for slot, (rx, ry) in enumerate(AMMO_FIRST_CELL_POINTS):
        if occupied is not None and not occupied[slot]:
            result[slot] = None
            continue
        mean = _sample_mean(screen, int(rx * w), int(ry * h))
        if mean is None:
            result[slot] = None
            states.append('出界')
            continue
        sat = float(mean.max() - mean.min())
        val = float(mean.mean())
        if val < _BACKGROUND_VALUE_MAX:
            # 深色背景优先: 空槽「添加舰船」面板 (深蓝, 如 (34,81,123)),
            # 无论饱和度一律判无船 (正常弹药格亮度 >= 117)
            result[slot] = None
            states.append(f'无船(深背景,val={val:.0f})')
        elif sat >= _FILL_SATURATION_MIN:
            result[slot] = False
            states.append(f'有弹(sat={sat:.0f})')
        elif (
            sat <= _GRAY_SATURATION_MAX
            and _GRAY_VALUE_MIN <= val <= _GRAY_VALUE_MAX
        ):
            result[slot] = True
            states.append('空')
        else:
            # 深色面板 (val<90, 空槽背景) / 白色「+」与缓冲带 → 无船或不适用
            # (缓冲带饱和度 40~60 归"有弹": 宁可漏补不可误补)
            result[slot] = None
            states.append(f'无船/不适用(sat={sat:.0f},val={val:.0f})')
    _log.debug('[Ammo] 单点判定: {}', states)
    return result
