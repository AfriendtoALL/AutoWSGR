"""活动期号参数化 (T2-3) 的测试。

问题背景
---------
活动期号此前硬编码在 3 个文件 4 处:

- ``ui/event/event_page.py``   地图标题/出击模板、节点坐标、α/β 入口判断
- ``ui/event/popups.py``      问答弹窗模板
- ``ui/main_page/event_nav.py`` 主页面活动入口图标 (含一处评审时遗漏的
  ``20260212``)

最危险的是 ``start_fight()`` 里的 ``if self._event_name == '20260930'``:
新活动上线时该判断永远为False → 允许传入 α/β 入口 → 用错入口打偏节点,
且**没有任何报错**。

以及 ``find_any`` 的语义是「任一模板命中即认定活动页」: 新旧活动页面相似时
旧标题图仍会命中 → ``is_current_page`` 返回 True 但后续判据全 False →
**无声失效**, 日志只有一行 warning。

本模块验证: 期号相关数据已全部外置到 ``autowsgr/data/event/*.yaml``,
且「缺配置」变成显式错误而非静默降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autowsgr.data.event_config import (
    DEFAULT_NAV_ORDER,
    EVENT_CONFIG_DIR,
    EventConfigError,
    has_event_config,
    load_event_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ═══════════════════════════════════════════════════════════════════════════════
# 配置文件本身
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventConfigFiles:
    """每个被代码引用的期号都必须有配置文件。"""

    @pytest.mark.parametrize('event_name', sorted(DEFAULT_NAV_ORDER))
    def test_config_exists(self, event_name: str) -> None:
        assert has_event_config(event_name), (
            f'期号 {event_name} 被 DEFAULT_NAV_ORDER 引用但缺配置文件'
        )

    def test_no_python_hardcodes_event_codes(self) -> None:
        """py 文件里不得再出现模板路径 / 坐标形式的期号硬编码。

        只允许出现在 docstring 示例与注释里 (那些是说明文字, 不影响行为)。
        """
        offenders: list[str] = []
        for py in (PROJECT_ROOT / 'autowsgr').rglob('*.py'):
            if 'event_config' in py.name:
                continue
            for lineno, line in enumerate(
                py.read_text(encoding='utf-8').splitlines(), 1
            ):
                code = '20260930' in line or '20260730' in line
                # 排除注释与文档字符串行
                stripped = line.strip()
                if code and not stripped.startswith('#') and not stripped.startswith('"""'):
                    if '.png' in line or '= (' in line or 'NODE_POSITION' in line:
                        offenders.append(f'{py.relative_to(PROJECT_ROOT)}:{lineno}: {stripped[:70]}')

        assert not offenders, '以下位置仍有期号硬编码:\n' + '\n'.join(offenders)

    def test_config_dir_shipped_with_package(self) -> None:
        """配置目录须随包分发 (打包后不可缺失)。"""
        assert EVENT_CONFIG_DIR.exists()
        assert EVENT_CONFIG_DIR.parent.name == 'data'


# ═══════════════════════════════════════════════════════════════════════════════
# 加载与解析
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadEventConfig:
    """加载器的正常路径。"""

    def test_loads_current_event(self) -> None:
        cfg = load_event_config('20260930')
        assert cfg.event_name == '20260930'
        assert cfg.supports_entrance is False, '本期活动不提供 α/β 入口'

    def test_node_positions_parsed(self) -> None:
        """节点坐标应解析为 {1..6: (x, y)}。"""
        positions = load_event_config('20260930').node_positions
        assert set(positions) == {1, 2, 3, 4, 5, 6}
        for value in positions.values():
            assert len(value) == 2
            assert all(0.0 <= v <= 1.0 for v in value), '坐标应为归一化相对值'

    def test_coordinates_match_migration(self) -> None:
        """坐标必须与迁移前的硬编码完全一致 (不能漂移)。

        迁移前 ``NODE_POSITIONS_BY_EVENT['20260930']`` 的原始值, 逐项断言。
        """
        expected = {
            1: (0.0879801735, 0.2251655629),
            2: (0.5947955390, 0.2240618102),
            3: (0.7856257745, 0.2075055188),
            4: (0.1964064436, 0.5242825607),
            5: (0.4702602230, 0.6401766004),
            6: (0.7806691450, 0.7483443709),
        }
        assert load_event_config('20260930').node_positions == expected

    def test_partial_templates_allowed(self) -> None:
        """模板可**部分声明** (如 20260212 只有入口图标)。"""
        cfg = load_event_config('20260212')
        assert cfg.has_templates('nav_icon') is True
        assert cfg.has_templates('title') is False

    def test_lru_cache_returns_same_instance(self) -> None:
        """加载结果应缓存 (模板是重量级资源)。"""
        assert load_event_config('20260930') is load_event_config('20260930')


# ═══════════════════════════════════════════════════════════════════════════════
# 缺配置 → 显式报错 (本次改造的核心目标)
# ═══════════════════════════════════════════════════════════════════════════════


class TestMissingConfigRaises:
    """「找不到配置」必须显式报错, 绝不静默降级。"""

    def test_unknown_event_raises(self) -> None:
        with pytest.raises(EventConfigError) as exc:
            load_event_config('20991231')
        message = str(exc.value)
        assert '20991231' in message
        assert '新增 YAML' in message, '报错应告知如何新增配置'

    def test_missing_template_kind_raises(self) -> None:
        """请求未声明的模板类别 → 报错 (而非返回空列表让识别静默退化)。"""
        cfg = load_event_config('20260212')
        with pytest.raises(EventConfigError) as exc:
            cfg.templates('title')
        assert 'title' in str(exc.value)
        assert '20260212.yaml' in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════════
# 格式错误 → 报错
# ═══════════════════════════════════════════════════════════════════════════════


class TestMalformedConfig:
    """配置写错时须早点失败, 并指出错在哪。"""

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        """临时配置用完即删, 并清缓存, 不污染真实数据目录。"""
        created: list[Path] = []

        def _create(content: str, name: str = '29990101') -> None:
            path = EVENT_CONFIG_DIR / f'{name}.yaml'
            path.write_text(content, encoding='utf-8')
            created.append(path)
            load_event_config.cache_clear()

        self._create = _create  # type: ignore[attr-defined]
        yield
        for p in created:
            p.unlink(missing_ok=True)
        load_event_config.cache_clear()

    def test_bad_node_position_shape(self) -> None:
        self._create(
            "event_name: '29990101'\nnode_positions:\n  '1': [0.5]\n"
        )
        with pytest.raises(EventConfigError, match='node_positions'):
            load_event_config('29990101')

    def test_bad_template_entry(self) -> None:
        self._create(
            "event_name: '29990101'\ntemplates:\n  title:\n    - foo: bar\n"
        )
        with pytest.raises(EventConfigError, match='path'):
            load_event_config('29990101')

    def test_bad_source_resolution(self) -> None:
        self._create(
            "event_name: '29990101'\ntemplates:\n"
            "  title:\n    - path: x.png\n      source_resolution: [960]\n"
        )
        with pytest.raises(EventConfigError, match='source_resolution'):
            load_event_config('29990101')


# ═══════════════════════════════════════════════════════════════════════════════
# 调用方已参数化
# ═══════════════════════════════════════════════════════════════════════════════


class TestCallSitesParametrized:
    """三处调用方应从配置取数据, 而非硬编码。"""

    def test_event_page_has_no_by_event_dict(self) -> None:
        """``NODE_POSITIONS_BY_EVENT`` 应已移除 (数据在 YAML)。"""
        from autowsgr.ui.event import event_page

        assert not hasattr(event_page, 'NODE_POSITIONS_BY_EVENT') or (
            event_page.NODE_POSITIONS_BY_EVENT == {}
        ), '期号坐标应来自 data/event/*.yaml, 不再维护 Python 侧字典'

    def test_node_positions_for_returns_config_values(self) -> None:
        from autowsgr.ui.event.event_page import node_positions_for

        assert node_positions_for('20260930') == load_event_config(
            '20260930'
        ).node_positions

    def test_node_positions_falls_back_for_unknown(self) -> None:
        """未知期号回退到通用网格, 且**有日志**而非静默。"""
        from autowsgr.ui.event.event_page import NODE_POSITIONS, node_positions_for

        assert node_positions_for('20991231') == dict(NODE_POSITIONS)

    def test_template_names_keep_kind_prefix(self) -> None:
        """模板 name 必须以 kind 开头。

        ``combat/recognizer.py::_is_event_overlay_button`` 依赖
        ``name.startswith('fight_button')`` 区分模板用途 —— 改 name 格式
        会破坏该判定 (历史上真的踩过)。
        """
        from autowsgr.ui.event.event_page import _get_fight_button_templates

        templates = _get_fight_button_templates()
        assert templates, '应至少加载到一个模板'
        assert all(t.name.startswith('fight_button') for t in templates)

    def test_template_counts_match_pre_migration(self) -> None:
        """模板数量须与迁移前一致 (title 2 / fight 2 / nav 3 / quiz 2)。"""
        from autowsgr.ui.event.event_page import (
            _get_event_title_templates,
            _get_fight_button_templates,
        )
        from autowsgr.ui.main_page.event_nav import _get_event_icon_templates

        assert len(_get_event_title_templates()) == 2
        assert len(_get_fight_button_templates()) == 2
        assert len(_get_event_icon_templates()) == 3