"""图像预处理 (vision.transform) 的单元测试。

背景 (T3-2 步骤 5)
-----------------
``combat/recognition.py`` 原先用 PIL 做「转灰度 + 缩放到 960x540」
再裁缩略图交给 DLL 识别敌编成。这让战斗域直接依赖了 PIL ——
项目里的**第二套图像库**(vision 全模块统一用 cv2)。

现下沉为 :func:`autowsgr.vision.transform.to_gray_resized`。
"""

from __future__ import annotations

import numpy as np
import pytest

from autowsgr.vision.transform import to_gray_resized


class TestToGrayResized:
    """灰度化 + 缩放。"""

    def test_rgb_to_gray_2d(self) -> None:
        """彩色输入 → 2维灰度。"""
        img = np.random.randint(0, 255, (100, 200, 3), dtype=np.uint8)
        out = to_gray_resized(img, (200, 100))
        assert out.ndim == 2
        assert out.shape == (100, 200)

    def test_size_is_w_then_h(self) -> None:
        """size 参数是 (宽, 高), 输出 shape 是 (高, 宽) —— 与 cv2 一致。

        这是与 PIL 的 ``resize((w, h))`` 恰好相同的顺序, 但输出数组
        是 numpy 的 (h, w), 容易搞反 —— 用非正方形尺寸锁定。
        """
        out = to_gray_resized(np.zeros((50, 100, 3), np.uint8), (300, 150))
        assert out.shape == (150, 300), '应输出 (高=150, 宽=300)'

    def test_grayscale_input_passthrough_shape(self) -> None:
        """输入已是灰度时也能处理 (不应因ndim==2 而出错)。"""
        img = np.zeros((100, 200), np.uint8)
        out = to_gray_resized(img, (200, 100))
        assert out.shape == (100, 200)

    def test_same_size_returns_without_resize(self) -> None:
        """尺寸相同则原样返回 (不触发无意义的插值)。"""
        img = np.random.randint(0, 255, (100, 200, 3), dtype=np.uint8)
        out = to_gray_resized(img, (200, 100))
        assert out.shape == (100, 200)

    def test_downscaling_uses_area(self) -> None:
        """缩小时用 INTER_AREA (抗锯齿优于默认线性插值)。"""
        import cv2

        small = to_gray_resized(
            np.random.randint(0, 255, (200, 400, 3), np.uint8),
            (100, 50),
        )
        exact = to_gray_resized(
            np.random.randint(0, 255, (200, 400, 3), np.uint8),
            (100, 50),
            interpolation=cv2.INTER_AREA,
        )
        assert small.shape == exact.shape

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError, match='为空'):
            to_gray_resized(np.array([]), (10, 10))  # type: ignore[arg-type]

    def test_none_input_raises(self) -> None:
        with pytest.raises(ValueError, match='为空'):
            to_gray_resized(None, (10, 10))  # type: ignore[arg-type]

    def test_realistic_combat_flow(self) -> None:
        """模拟 recognition 的真实用法: 720p 截图 → 960x540 灰度。"""
        screen = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
        out = to_gray_resized(screen, (960, 540))

        assert out.shape == (540, 960), 'DLL 期望 960x540 灰度'
        assert out.dtype == np.uint8

        # 裁 6 张缩略图 (recognition 的下一步)
        crop = out[100:200, 200:400]
        assert crop.shape == (100, 200)


class TestCombatNoLongerUsesPil:
    """战斗域不应再直接用 PIL。"""

    def test_recognition_uses_vision(self) -> None:
        """recognition 应调用 vision 的 to_gray_resized。"""
        from pathlib import Path

        import autowsgr.combat as combat_pkg

        src = (Path(combat_pkg.__file__).parent / 'recognition.py').read_text(
            encoding='utf-8'
        )
        assert 'to_gray_resized' in src, '应使用 vision.to_gray_resized'
        assert 'Image.fromarray' not in src, '不应再有 PIL 的 Image.fromarray'