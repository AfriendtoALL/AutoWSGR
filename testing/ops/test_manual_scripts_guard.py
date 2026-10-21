"""ops 实机脚本不被 pytest 收集的防回归测试。

背景 (2026-10-11)
-----------------
上一轮评审曾把 ``testing/ops/`` 下 13 个不带 ``test_` 前缀的文件判定为
「pytest 未收集 = 实际零覆盖」, 并计划重命名。**该结论是错的**, 实施本项
时才发现:

- 这些文件里 ``test_`` 定义数为**0**, 全部带 ``if __name__ == '__main__'``
- 它们是需要**真实模拟器**的交互式验证脚本 (连接设备 → 真实点击 → 阻塞)
- 重命名会让 pytest 收集它们 → 跑全量时尝试连接不存在的设备 → 一片失败

正确处置不是改名, 而是**明确标注性质 + 加防误改保护**。本测试锁住该保护,
避免后人「好心」重命名后把问题引入。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from testing.conftest import (
    MANUAL_E2E_SCRIPT_NAMES,
    looks_like_misrenamed_manual_script,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = PROJECT_ROOT / 'testing' / 'ops'


# ═══════════════════════════════════════════════════════════════════════════════
# 这些脚本确实不该被收集
# ═══════════════════════════════════════════════════════════════════════════════


class TestManualScriptsAreNotPytestTests:
    """确认「未被收集」是设计意图, 而非命名疏忽。"""

    @pytest.mark.parametrize('name', sorted(MANUAL_E2E_SCRIPT_NAMES))
    def test_script_exists(self, name: str) -> None:
        assert (OPS_DIR / f'{name}.py').exists(), f'缺少实机脚本 {name}.py'

    @pytest.mark.parametrize('name', sorted(MANUAL_E2E_SCRIPT_NAMES))
    def test_script_has_no_test_functions(self, name: str) -> None:
        """实机脚本内不得含 ``def test_`` —— 它们不是 pytest 测试。"""
        src = (OPS_DIR / f'{name}.py').read_text(encoding='utf-8')
        assert '\ndef test_' not in src, (
            f'{name}.py 含 test_ 函数, 应拆分为独立的 test_*_unit.py'
        )

    @pytest.mark.parametrize('name', sorted(MANUAL_E2E_SCRIPT_NAMES))
    def test_script_is_runnable_standalone(self, name: str) -> None:
        """必须是可直接 ``python xxx.py`` 运行的脚本。"""
        src = (OPS_DIR / f'{name}.py').read_text(encoding='utf-8')
        assert "__main__" in src, f'{name}.py 缺少 __main__ 守卫'

    def test_real_unit_tests_use_test_prefix(self) -> None:
        """真正的单元测试都在 ``test_*.py`` 里, 且确实被收集。"""
        unit_tests = sorted(p.name for p in OPS_DIR.glob('test_*.py'))
        assert len(unit_tests) >= 10, (
            f'testing/ops/ 的单元测试偏少: {unit_tests}'
        )

    def test_framework_is_private(self) -> None:
        """``_framework.py`` 是共享工具, 不应作为测试收集。"""
        assert (OPS_DIR / '_framework.py').exists()
        assert not (OPS_DIR / 'test__framework.py').exists()


# ═══════════════════════════════════════════════════════════════════════════════
# 防误改保护 (conftest.py 的 pytest_collect_file)
# ═══════════════════════════════════════════════════════════════════════════════


class TestMisrenameGuard:
    """若有人把实机脚本改名为 ``test_*.py``, 必须立即报错而非静默收集。"""

    def test_guard_rejects_renamed_script(self, tmp_path: Path) -> None:
        """核心行为: 伪装成 test_*.py 后必须被判为误改名。

        刻意**不造真实文件** —— 之前版本在项目目录里 ``shutil.copy`` 出
        ``test_build.py``, 进程被中断 (超时/强杀) 时 ``finally`` 来不及清理,
        残留文件会让**下一次全量跑直接收集失败** (2026-10-12 踩过, 手动
        清掉才发现是这里留下的)。现在直接调``conftest`` 里的同一份判定函数,
        测的是真代码路径且零副作用。
        """
        assert 'build' in MANUAL_E2E_SCRIPT_NAMES, (
            f'实机脚本名单应含 build, 实际: {sorted(MANUAL_E2E_SCRIPT_NAMES)}'
        )

        # 正例: 名单里的脚本加 test_ 前缀必须被识别为误改名
        assert looks_like_misrenamed_manual_script('test_build.py') == 'build'
        for name in sorted(MANUAL_E2E_SCRIPT_NAMES):
            assert looks_like_misrenamed_manual_script(f'test_{name}.py') == name

        # 反例: 真正的单元测试 (带 _unit 后缀 / 不在名单里) 不能被误伤
        for name in (
            'test_build_unit.py',
            'test_scheduler_unit.py',
            'test_normal_fight_unit.py',
            'test_intensify.py',
            'test_manual_scripts_guard.py',
        ):
            assert looks_like_misrenamed_manual_script(name) is None, (
                f'{name} 不该被判为误改名'
            )

        # 原名 (无 test_ 前缀) 也不该被判
        for name in sorted(MANUAL_E2E_SCRIPT_NAMES):
            assert looks_like_misrenamed_manual_script(f'{name}.py') is None

    def test_guard_hook_still_raises_on_real_path(self, tmp_path: Path) -> None:
        """钩子在**真实路径**上仍必须抛 UsageError（不依赖列表判断）。"""
        import pytest
        from testing.conftest import pytest_collect_file

        # 造一个 testing/ops 下的路径（只验证判定，不落盘）
        fake = OPS_DIR / 'test_build.py'
        with pytest.raises(pytest.UsageError, match='build'):
            pytest_collect_file(fake, parent=None)

        # 非 ops 目录不触发
        assert pytest_collect_file(PROJECT_ROOT / 'testing' / 'ops_x' / 'test_build.py', None) is None

    def test_normal_collection_unaffected(self) -> None:
        """正常收集必须畅通无阻 (防护不能误伤)。"""
        import subprocess
        import sys

        proc = subprocess.run(  # noqa: S603
            [sys.executable, '-m', 'pytest', 'testing/ops/', '--collect-only', '-q'],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=120,
            check=False,
        )
        output = (proc.stdout + proc.stderr).decode('utf-8', errors='replace')
        assert proc.returncode == 0, f'正常收集不应报错:\n{output[-800:]}'
        assert 'tests collected' in output


# ═══════════════════════════════════════════════════════════════════════════════
# 文档标注
# ═══════════════════════════════════════════════════════════════════════════════


class TestScriptsAreDocumented:
    """每个脚本都应在 docstring 顶部声明自己不是 pytest 测试。"""

    @pytest.mark.parametrize('name', sorted(MANUAL_E2E_SCRIPT_NAMES))
    def test_docstring_declares_nature(self, name: str) -> None:
        src = (OPS_DIR / f'{name}.py').read_text(encoding='utf-8')
        assert src.startswith('"""实机验证脚本'), (
            f'{name}.py 的 docstring 未在首行声明「实机验证脚本 — 非 pytest 测试」'
        )

    def test_readme_exists(self) -> None:
        """README 说明为何这些脚本不该被收集。"""
        readme = OPS_DIR / 'README.md'
        assert readme.exists(), '缺少 testing/ops/README.md'
        content = readme.read_text(encoding='utf-8')
        assert '不能被 pytest 收集' in content or '不是 pytest 测试' in content
        assert '重命名' in content, 'README 应说明不要重命名的原因'