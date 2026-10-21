"""pytest 全局配置与公共 fixtures。

本文件同时承担一项**防回归**职责: 阻止 ``testing/ops/`` 下的实机验证脚本
被 pytest 收集。

背景
----
``testing/ops/`` 里的 ``build.py`` / ``campaign.py`` / ``event_fight.py``
等文件是**需要真实模拟器的交互式脚本**, 不是单元测试 —— 它们会连接设备、
在真实游戏里点击并阻塞直到操作完成。若被pytest 收集, 跑全量测试时会尝试
连接不存在的设备, 产生大量无意义失败。

历史上曾出现反向误判: 因这些文件不带 ``test_`` 前缀, 被当成「测试文件
命名不规范导致 pytest 未收集」,进而计划重命名 —— 那会把上述问题**引入**。

因此这里显式声明这些文件不可被收集。若将来有人把 ``build.py`` 改成
``test_build.py``,本检查会立即失败并说明原因, 而不是等到跑全量时才发现
一堆连接设备的错误。

真正为某个 ops 补单元测试时, 应新建 ``test_<功能>_unit.py`` 并用 mock
替代真机依赖, 而不是把这些脚本改名。参见 ``testing/ops/README.md``。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 防回归: 实机脚本不得被 pytest 收集
# ═══════════════════════════════════════════════════════════════════════════════

#: 这些文件在 testing/ops/ 下需真机运行, 不应被 pytest 收集。
#: 改名会让全量测试尝试连接设备而失败 —— 见本文件与 testing/ops/README.md 的说明。
MANUAL_E2E_SCRIPT_NAMES = frozenset({
    'build',
    'campaign',
    'cook',
    'decisive_battle',
    'destroy',
    'event_fight',
    'exercise',
    'expedition',
    'normal_fight',
    'repair',
    'reward',
    'scheduler',
    'startup',
})


def looks_like_misrenamed_manual_script(file_name: str) -> str | None:
    """若 ``file_name`` 是被误改名的实机脚本, 返回原名; 否则 ``None``。

    抽成独立函数而非只写在钩子里, 是为了让测试能**直接验证同一份判定逻辑**
    而不必真的造文件跑子进程 —— 之前那个版本在项目目录里``shutil.copy``
    出 ``test_build.py``, 进程被中断时 ``finally`` 来不及清理就会残留,
    导致下一次全量跑直接收集失败 (2026-10-12 踩过)。
    """
    stem = Path(file_name).stem
    for prefix in ('test_',):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    else:
        return None
    return stem if stem in MANUAL_E2E_SCRIPT_NAMES else None


def pytest_collect_file(file_path: Path, parent):
    """拒绝收集被误改名的实机验证脚本。

    pytest 只会对 ``test_*.py`` / ``*_test.py`` 调用本钩子, 故正常情况下
    这些手动脚本不会走到这里。**若有人把某个脚本改名为 ``test_*.py``**,
    钩子会命中并立即报错, 而不是等到跑全量时才发现一堆连接设备的失败。
    """
    del parent  # 钩子签名要求, 本实现不需要
    if file_path.parent.name != 'ops':
        return None

    stem = looks_like_misrenamed_manual_script(file_path.name)
    if stem is None:
        return None

    raise pytest.UsageError(
        f'testing/ops/{file_path.name} 疑似被误改名的实机验证脚本'
        f'(原名 {stem}.py), 不能作为 pytest 测试收集。\n'
        f'pytest 收集它会尝试连接真实模拟器并失败。\n'
        f'正确做法: 改回 {stem}.py; 若要补单元测试, 请新建 '
        f'test_{stem}_unit.py 并用 mock 替代真机依赖。\n'
        f'详见 testing/ops/README.md。'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 公共 fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fixtures_dir() -> Path:
    """测试数据目录。"""
    return Path(__file__).parent / 'fixtures'


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Callable[[str, str], Path]:
    """创建临时 YAML 文件的工厂 fixture。"""

    def _factory(name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding='utf-8')
        return p

    return _factory