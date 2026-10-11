"""连通域分析 (vision.blob) 的单元测试。

背景 (T3-2步骤 3)
-----------------
``combat/node_tracker.py`` 原先直接 ``import cv2`` 调用
``connectedComponentsWithStats`` —— 战斗域绕过 :mod:`autowsgr.vision`
抽象层用第三方库, 与「识别一律走 vision」的分层约定不符。

本模块验证 :func:`largest_blob` 的语义, 并锁住「combat 不再直接 import
第三方视觉库」这一约定。
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from autowsgr.vision import Blob, largest_blob


# ═══════════════════════════════════════════════════════════════════════════════
# 基础语义
# ═══════════════════════════════════════════════════════════════════════════════


class TestLargestBlob:
    """最大连通块的选取逻辑。"""

    def test_empty_mask_returns_none(self) -> None:
        assert largest_blob(np.zeros((50, 50), np.uint8)) is None

    def test_single_block(self) -> None:
        mask = np.zeros((50, 50), np.uint8)
        mask[10:20, 10:20] = 1
        blob = largest_blob(mask)
        assert blob is not None
        assert blob.area == 100
        assert blob.bounding_box == (10, 10, 10, 10)

    def test_picks_largest_of_several(self) -> None:
        """面积最大的那个应被选中, 与原cv2 直调实现一致。"""
        mask = np.zeros((100, 100), np.uint8)
        mask[10:20, 10:20] = 1  # 100 px
        mask[50:70, 50:80] = 1  # 600 px
        mask[80:85, 80:85] = 1  # 25 px

        blob = largest_blob(mask, min_area=50)
        assert blob is not None
        assert blob.area == 600
        assert blob.bounding_box == (50, 50, 30, 20)

    def test_centroid(self) -> None:
        """重心应落在块中心 (原实现取 centroids[label], 此处等价)。"""
        mask = np.zeros((100, 100), np.uint8)
        mask[50:70, 50:80] = 1
        blob = largest_blob(mask)
        assert blob is not None
        cx, cy = blob.centroid
        assert cx == pytest.approx(64.5)
        assert cy == pytest.approx(59.5)

    def test_min_area_filters_noise(self) -> None:
        """小于 min_area 的块应被忽略 → 返回 None。"""
        mask = np.zeros((50, 50), np.uint8)
        mask[10:15, 10:15] = 1  # 25 px
        assert largest_blob(mask, min_area=200) is None

    def test_min_area_picks_qualified(self) -> None:
        """小块被过滤后, 合格块仍应被选中。"""
        mask = np.zeros((100, 100), np.uint8)
        mask[10:15, 10:15] = 1  # 25 px
        mask[50:80, 50:80] = 1  # 900 px
        blob = largest_blob(mask, min_area=200)
        assert blob is not None
        assert blob.area == 900

    def test_connectivity_4_vs_8(self) -> None:
        """对角相连的两块: 8 连通视作一块, 4 连通视作两块。"""
        mask = np.zeros((10, 10), np.uint8)
        mask[2, 2] = 1
        mask[3, 3] = 1  # 与上一个对角相邻

        eight = largest_blob(mask, connectivity=8)
        four = largest_blob(mask, connectivity=4)
        assert eight is not None and eight.area == 2
        assert four is not None and four.area == 1

    def test_accepts_bool_mask(self) -> None:
        """布尔掩码也能吃 (cv2 要求 uint8, 内部应转换)。"""
        mask = np.zeros((20, 20), dtype=bool)
        mask[5:15, 5:15] = True
        blob = largest_blob(mask)
        assert blob is not None
        assert blob.area == 100

    def test_accepts_non_binary_values(self) -> None:
        """非 0 即前景 —— cv2 直调时要求 0/1, 这里放宽以免调用方踩坑。"""
        mask = np.full((20, 20), 255, np.uint8)
        mask[0, 0] = 0
        blob = largest_blob(mask)
        assert blob is not None
        assert blob.area == 399

    def test_none_mask_returns_none(self) -> None:
        assert largest_blob(None) is None  # type: ignore[arg-type]

    def test_blob_is_frozen(self) -> None:
        """Blob 应不可变 (纯数据)。"""
        mask = np.zeros((10, 10), np.uint8)
        mask[2:5, 2:5] = 1
        blob = largest_blob(mask)
        assert isinstance(blob, Blob)
        with pytest.raises(Exception):  # noqa: B017, PT011 - frozen dataclass
            blob.area = 999  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════════
# 分层约定: combat 不直接 import 第三方视觉库
# ═══════════════════════════════════════════════════════════════════════════════


class TestCombatDoesNotImportCv2Directly:
    """combat 层不应绕过 vision 直接用 **cv2** (T3-2 步骤 3)。

    .. note::
       只检查 cv2 —— 本步骤的范围是「连通域分析下沉」。
       ``recognition.py`` 仍有 ``from PIL import Image``, 属图像变换类依赖,
       归T3-2 步骤 5 一并处理, 此处不做拦截。
    """

    def test_no_direct_cv2_in_combat(self) -> None:
        import ast

        import autowsgr.combat as combat_pkg

        offenders: list[str] = []
        for py in Path(combat_pkg.__file__).parent.glob('*.py'):
            tree = ast.parse(py.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split('.')[0] == 'cv2':
                            offenders.append(f'{py.name}:{node.lineno}: import {alias.name}')
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or '').split('.')[0] == 'cv2':
                        offenders.append(f'{py.name}:{node.lineno}: from {node.module}')

        assert not offenders, (
            'combat 层不应直接 import cv2 (应走 vision 封装):\n'
            + '\n'.join(offenders)
        )

    def test_node_tracker_uses_vision_blob(self) -> None:
        """node_tracker 应通过 vision.largest_blob 取连通域。"""
        src = Path(
            __import__('autowsgr.combat', fromlist=['node_tracker']).__file__
        ).parent / 'node_tracker.py'
        text = src.read_text(encoding='utf-8')
        assert 'largest_blob' in text, 'node_tracker 应使用 vision.largest_blob'
        assert 'connectedComponentsWithStats' not in text, (
            'node_tracker 不应再直接调用 cv2.connectedComponentsWithStats'
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 行为等价性: 下沉到 vision 后行为不变
# ═══════════════════════════════════════════════════════════════════════════════


def _make_screen(blocks: list[tuple[int, int, int, int]]) -> np.ndarray:
    """构造测试画面: 黑底 +若干黄色矩形块 (RGB)。"""
    screen = np.zeros((540, 960, 3), np.uint8)
    screen[:, :] = (10, 10, 10)
    for y1, y2, x1, x2 in blocks:
        screen[y1:y2, x1:x2] = (240, 210, 90)  # 落在黄阈值内
    return screen


class TestNodeTrackerEquivalence:
    """``_find_yellow_cluster`` 换用 vision 后行为不变。

    这是本次下沉的**核心风险点**: 参数 (min_area=200) 或坐标换算若出错,
    节点追踪会静默失效 —— 表现为「索敌后一律撤退」(实机 2026-10-08
    三连「节点=0 撤退」事故的同类症状), 单测之外极难发现。
    """

    def test_finds_single_yellow_block(self) -> None:
        from autowsgr.combat.node_tracker import NodeTracker

        # 100x200 的块 = 20000 像素, 远超 min_area=200
        screen = _make_screen([(100, 300, 100, 200)])
        result = NodeTracker._find_yellow_cluster(screen)

        assert result is not None
        rel_x, rel_y = result
        # 块中心 = (150, 200) → 相对 (150/960, 200/540)
        assert rel_x == pytest.approx(150 / 960, abs=1e-3)
        assert rel_y == pytest.approx(200 / 540, abs=1e-3)

    def test_picks_larger_of_two(self) -> None:
        """两块时应取更大者 (原实现按面积择优)。"""
        from autowsgr.combat.node_tracker import NodeTracker

        screen = _make_screen([
            (50, 100, 50, 100),    # 50x50= 2500
            (300, 450, 600, 700),  # 150x100 = 15000  ← 应选这个
        ])
        result = NodeTracker._find_yellow_cluster(screen)

        assert result is not None
        rel_x, rel_y = result
        assert rel_x == pytest.approx(650 / 960, abs=1e-3)
        assert rel_y == pytest.approx(375 / 540, abs=1e-3)

    def test_too_small_block_ignored(self) -> None:
        """小于 min_area=200 的块应返回 None (噪声过滤)。"""
        from autowsgr.combat.node_tracker import NodeTracker

        screen = _make_screen([(100, 110, 100, 110)])  # 10x10 = 100 < 200
        assert NodeTracker._find_yellow_cluster(screen) is None

    def test_no_yellow_returns_none(self) -> None:
        from autowsgr.combat.node_tracker import NodeTracker

        assert NodeTracker._find_yellow_cluster(_make_screen([])) is None

    def test_min_area_threshold_is_200(self) -> None:
        """**行为**验证 min_area=200, 而非检查源码字符串。

        前两版都栽在这里: ①断言源码含 ``min_area=200`` 字样 —— 变异后仍全绿;
        ②构造的色块面积实际是 2000 和 180 万 (我把乘法算错了),
        远超阈值 2000, 同样全绿。

        这里用**精确面积**的输入 (全部可手算验证):
        - 面积 1x199 = 199 < 200 → 应被拒
        - 面积 10x20 = 200 = 阈值 → 应被接受 (阈值调到 >200 即失败)
        - 面积 20x50 = 1000 → 应被接受 (阈值调到 >1000 即失败)
        """
        from autowsgr.combat.node_tracker import NodeTracker

        # 1 x 199 = 199 像素
        area_199 = _make_screen([(100, 101, 100, 299)])
        # 10 x 20 = 200 像素 (恰好等于阈值)
        area_200 = _make_screen([(100, 110, 100, 120)])
        # 20 x 50 = 1000 像素
        area_1000 = _make_screen([(100, 120, 100, 150)])

        assert NodeTracker._find_yellow_cluster(area_199) is None, (
            '199 像素应被 min_area=200 过滤掉'
        )
        assert NodeTracker._find_yellow_cluster(area_200) is not None, (
            '200 像素应恰好通过 min_area=200 过滤'
        )
        assert NodeTracker._find_yellow_cluster(area_1000) is not None, (
            '1000 像素应通过 min_area=200 过滤'
        )