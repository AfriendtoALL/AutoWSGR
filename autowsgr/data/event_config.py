"""活动期号配置加载 (2026-10-11 T2-3)。

解决的问题
----------
活动相关的**期号硬编码**此前散落在 3 个文件 4 处:

- ``ui/event/event_page.py``   地图标题/出击模板、节点坐标、α/β 入口判断
- ``ui/event/popups.py``      问答弹窗模板
- ``ui/main_page/event_nav.py`` 主页面活动入口图标 (含一处评审时遗漏的
  ``20260212``)

新活动上线必须改Python 代码, 且「模板找不到时静默用旧模板凑合」会导致
**无声失效** —— 新旧活动页面相似时旧标题图仍命中, ``is_current_page`` 返回
True 但后续判据全False, 日志只有一行warning。

本模块把期号相关数据外置到 ``autowsgr/data/event/<期号>.yaml``, 并让
「找不到配置」变成**显式报错**。

.. code-block:: python

    from autowsgr.data.event_config import load_event_config

    cfg = load_event_config('20260930')
    cfg.templates('fight_button')   # → [(ImageTemplate, ...)]
    cfg.node_positions              # → {1: (x, y), ...}
    cfg.supports_entrance           # → False
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autowsgr.infra.file_utils import load_yaml
from autowsgr.infra.logger import get_logger


if TYPE_CHECKING:
    from autowsgr.image_resources import ImageTemplate


_log = get_logger('data.event')

EVENT_CONFIG_DIR = Path(__file__).resolve().parent / 'event'
"""活动期号配置目录 (``autowsgr/data/event/``)。"""

#: 没有专属配置时的默认入口图标搜索顺序 (主页面活动入口按期号挨个试匹配)。
#: 20260212 仅提供入口图标, 未录入地图模板, 故置于最前 —— 主页面只需认图标。
DEFAULT_NAV_ORDER = ('20260930', '20260212', '20260730')


class EventConfigError(Exception):
    """活动配置缺失或格式错误。

    与 :class:`~autowsgr.infra.exceptions.ActionFailedError` 区分: 前者是
    **部署问题**(缺文件/写错了), 后者是游戏内操作失败。
    """


@dataclass(frozen=True)
class TemplateSpec:
    """一个模板的声明 (路径 + 素材原始分辨率)。"""

    path: str
    source_resolution: tuple[int, int] = (960, 540)

    def load(self, name: str) -> ImageTemplate:
        """按声明加载模板。"""
        from autowsgr.image_resources._lazy import load_template

        return load_template(
            self.path,
            name=name,
            source_resolution=self.source_resolution,
        )


@dataclass(frozen=True)
class EventConfig:
    """单个活动期号的配置 (只读)。"""

    event_name: str
    title: str
    supports_entrance: bool
    entrance_hint: str
    _templates: dict[str, tuple[TemplateSpec, ...]]
    _node_positions: dict[int, tuple[float, float]]

    def has_templates(self, kind: str) -> bool:
        """是否声明了某类模板。"""
        return bool(self._templates.get(kind))

    def templates(self, kind: str) -> tuple[ImageTemplate, ...]:
        """加载某类模板。

        模板 ``name`` 采用 ``<kind>_<期号>[_<序号>]`` 格式 —— **``kind`` 前缀
        必须在最前**: :func:`autowsgr.combat.recognizer._is_event_overlay_button`
        等判定逻辑按 ``name.startswith('fight_button')`` 区分模板用途, 换
        顺序会破坏它们的语义。

        Raises
        ------
        EventConfigError
            该期号未声明此类模板 —— **显式报错而非静默回退**, 避免
            「新活动页面相似 → 旧模板仍命中 → 识别无声失效」。
        """
        specs = self._templates.get(kind)
        if not specs:
            raise EventConfigError(
                f'活动 {self.event_name} 未声明 {kind!r} 模板。'
                f'请在 {EVENT_CONFIG_DIR / (self.event_name + ".yaml")} '
                f"的 templates.{kind} 下补充。"
            )

        if len(specs) == 1:
            names = (f'{kind}_{self.event_name}',)
        else:
            names = tuple(f'{kind}_{self.event_name}_{i}' for i in range(len(specs)))

        return tuple(
            spec.load(name) for spec, name in zip(specs, names, strict=True)
        )

    @property
    def node_positions(self) -> dict[int, tuple[float, float]]:
        """地图入口坐标; 未声明时返回空字典 (调用方回退默认网格)。"""
        return dict(self._node_positions)


def _parse_templates(raw: Any, event_name: str) -> dict[str, tuple[TemplateSpec, ...]]:
    """解析 templates 段。"""
    out: dict[str, tuple[TemplateSpec, ...]] = {}
    if not raw:
        return out
    if not isinstance(raw, dict):
        raise EventConfigError(f'活动 {event_name} 的 templates 应为映射')

    for kind, items in raw.items():
        specs: list[TemplateSpec] = []
        for item in items or ():
            if not isinstance(item, dict) or 'path' not in item:
                raise EventConfigError(
                    f'活动 {event_name} 的 templates.{kind} 条目缺少 path'
                )
            res = item.get('source_resolution') or (960, 540)
            if not (isinstance(res, (list, tuple)) and len(res) == 2):
                raise EventConfigError(
                    f'活动 {event_name} 的 templates.{kind}.source_resolution '
                    f'应为 [宽, 高]'
                )
            specs.append(TemplateSpec(str(item['path']), (int(res[0]), int(res[1]))))
        out[str(kind)] = tuple(specs)
    return out


def _parse_node_positions(raw: Any, event_name: str) -> dict[int, tuple[float, float]]:
    """解析 node_positions 段 (键为地图编号 1~6)。"""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise EventConfigError(f'活动 {event_name} 的 node_positions 应为映射')

    out: dict[int, tuple[float, float]] = {}
    for key, pos in raw.items():
        if not isinstance(pos, (list, tuple)) or len(pos) != 2:
            raise EventConfigError(
                f'活动 {event_name} 的 node_positions.{key} 应为 [x, y]'
            )
        out[int(key)] = (float(pos[0]), float(pos[1]))
    return out


@functools.lru_cache(maxsize=8)
def load_event_config(event_name: str) -> EventConfig:
    """加载活动期号配置。

    Raises
    ------
    EventConfigError
        配置文件不存在 (新活动上线必须先补数据文件) 或格式错误。
    """
    path = EVENT_CONFIG_DIR / f'{event_name}.yaml'
    if not path.exists():
        known = sorted(p.stem for p in EVENT_CONFIG_DIR.glob('*.yaml'))
        raise EventConfigError(
            f'活动 {event_name} 没有配置文件 ({path.name})。'
            f'新活动上线需先在 {EVENT_CONFIG_DIR} 下新增 YAML; '
            f'当前已配置: {known or "(空)"}'
        )

    data = load_yaml(path) or {}
    if not isinstance(data, dict):
        raise EventConfigError(f'活动配置文件格式错误 (应为映射): {path.name}')

    return EventConfig(
        event_name=str(data.get('event_name', event_name)),
        title=str(data.get('title', event_name)),
        supports_entrance=bool(data.get('supports_entrance', True)),
        entrance_hint=str(data.get('entrance_hint', '活动不支持 α/β 入口')),
        _templates=_parse_templates(data.get('templates'), event_name),
        _node_positions=_parse_node_positions(data.get('node_positions'), event_name),
    )


def has_event_config(event_name: str) -> bool:
    """是否已有该期号的配置文件 (不加缓存, 供探查用)。"""
    return (EVENT_CONFIG_DIR / f'{event_name}.yaml').exists()