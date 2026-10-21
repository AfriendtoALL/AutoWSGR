"""诊断脚本的公共路径解析。

这些脚本被从 ``tools/diagnostics/`` 直接运行, 因此不能用相对路径假设目录
结构 (原始版本硬编码 ``C:/Users/13930/Desktop/AutoWSGR``, 换机即废)。

``PROJECT_ROOT`` 由本文件位置向上三层推导 (``tools/diagnostics/_paths.py`` →
``tools/diagnostics`` → ``tools`` → 项目根), 无论从哪个工作目录调用都成立。

帧文件统一放 ``<PROJECT_ROOT>/logs/diagnostics/``, 与 ``logs/`` 下已有的
时间戳目录并存, 不污染用户主目录, 也不依赖系统临时目录 (临时目录会被清理,
导致「昨天抓的帧今天看不了」)。
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""AutoWSGR 项目根目录。"""

DIAG_DIR = PROJECT_ROOT / 'logs' / 'diagnostics'
"""诊断产物目录 (自动创建)。"""

FRAME_NPY = DIAG_DIR / 'diag_frame.npy'
"""diag_capture.py 落盘的原始帧 (供 diag_grid.py 读取)。"""

FRAME_PNG = DIAG_DIR / 'diag_frame.png'
"""diag_capture.py 落盘的帧截图 (人眼查看)。"""

ADB_FRAME_PNG = DIAG_DIR / 'adb_frame.png'
"""外部 adb 截图的落地位置 (供 diag_enter / diag_grid2 / diag_ocr 读取)。"""


def ensure_project_on_path() -> None:
    """把项目根加入 ``sys.path``, 使脚本可 ``import autowsgr``。

    替代原先硬编码 ``sys.path.insert(0, r'C:/Users/13930/Desktop/AutoWSGR')``。
    重复调用安全 (已存在则不重复插入)。
    """
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def ensure_diag_dir() -> Path:
    """确保诊断产物目录存在并返回它。"""
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    return DIAG_DIR


def default_config() -> str:
    """项目根下的 ``usersettings.yaml`` 绝对路径。"""
    return str(PROJECT_ROOT / 'usersettings.yaml')