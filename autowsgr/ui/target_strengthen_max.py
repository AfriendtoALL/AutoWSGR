"""Canonical strengthening data derived from the source data snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from autowsgr.ui.intensify_workflow import ShipStats


if TYPE_CHECKING:
    from pathlib import Path


_BASE_EXPERIENCE = {11: 10, 12: 20, 13: 30}


def source_to_canonical_ship_id(source_id: int) -> int:
    prefix = source_id // 1_000_000
    if prefix not in (10, 11):
        raise ValueError(f'未知强化数据形态前缀: {source_id}')
    return (prefix - 10) * 1000 + (source_id // 100) % 10_000


def source_experience_per_level(source_id: int) -> int:
    try:
        value = _BASE_EXPERIENCE[source_id % 100]
    except KeyError as error:
        raise ValueError(f'未知强化经验类别: {source_id}') from error
    return value * 6 // 5 if source_id // 1_000_000 == 11 else value


@dataclass(frozen=True, slots=True)
class TargetStrengthenMaxResolver:
    levels_by_ship_id: dict[int, ShipStats]

    @classmethod
    def from_source(cls, path: Path) -> TargetStrengthenMaxResolver:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, list):
            raise TypeError('强化数据根节点必须是数组')
        result: dict[int, ShipStats] = {}
        for item in payload:
            expected_fields = {
                'id',
                'title',
                'strengthenLevelUpExp',
                'strengthenSupply',
                'strengthenMax',
            }
            required_without_exp = expected_fields - {'strengthenLevelUpExp'}
            if (
                not isinstance(item, dict)
                or not required_without_exp.issubset(item)
                or not set(item).issubset(expected_fields)
            ):
                raise ValueError('强化数据记录 schema 不匹配')
            source_id = int(item['id'])
            divisor = _explicit_experience_per_level(item, source_id)
            maximum = item['strengthenMax']
            if not isinstance(maximum, dict) or set(maximum) != {
                'atk',
                'torpedo',
                'def',
                'airDef',
            }:
                raise ValueError(f'强化上限 schema 不匹配: {source_id}')
            raw_values = [int(maximum[field]) for field in ('atk', 'torpedo', 'def', 'airDef')]
            if any(value < 0 for value in raw_values):
                raise ValueError(f'强化上限不能为负数: {source_id}')
            displayed = [(value + divisor - 1) // divisor for value in raw_values]

            ship_id = source_to_canonical_ship_id(source_id)
            if ship_id in result:
                raise ValueError(f'规范舰船 ID 重复: {ship_id}')
            result[ship_id] = ShipStats(
                firepower=displayed[0],
                torpedo=displayed[1],
                armor=displayed[2],
                anti_air=displayed[3],
            )
        return cls(result)

    def __call__(self, ship_id: int) -> ShipStats | None:
        return self.levels_by_ship_id.get(ship_id)


@dataclass(frozen=True, slots=True)
class ShipStrengthenDataResolver:
    """Resolve both material contribution and displayed target maxima by ship ID."""

    supply_by_ship_id: dict[int, ShipStats]
    maximum_by_ship_id: dict[int, ShipStats]
    experience_per_level_by_ship_id: dict[int, int]

    @classmethod
    def from_source(cls, path: Path) -> ShipStrengthenDataResolver:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, list):
            raise TypeError('强化数据根节点必须是数组')
        supplies: dict[int, ShipStats] = {}
        maxima: dict[int, ShipStats] = {}
        experience_per_level: dict[int, int] = {}
        for item in payload:
            expected_fields = {
                'id',
                'title',
                'strengthenLevelUpExp',
                'strengthenSupply',
                'strengthenMax',
            }
            required_without_exp = expected_fields - {'strengthenLevelUpExp'}
            if (
                not isinstance(item, dict)
                or not required_without_exp.issubset(item)
                or not set(item).issubset(expected_fields)
            ):
                raise ValueError('强化数据记录 schema 不匹配')
            source_id = int(item['id'])
            ship_id = source_to_canonical_ship_id(source_id)
            if ship_id in supplies:
                raise ValueError(f'规范舰船 ID 重复: {ship_id}')
            supply = _source_stats(item['strengthenSupply'], source_id, '素材贡献')
            raw_maximum = _source_stats(item['strengthenMax'], source_id, '强化上限')
            divisor = _explicit_experience_per_level(item, source_id)
            supplies[ship_id] = supply
            experience_per_level[ship_id] = divisor
            maxima[ship_id] = ShipStats(
                firepower=(raw_maximum.firepower + divisor - 1) // divisor,
                torpedo=(raw_maximum.torpedo + divisor - 1) // divisor,
                armor=(raw_maximum.armor + divisor - 1) // divisor,
                anti_air=(raw_maximum.anti_air + divisor - 1) // divisor,
            )
        return cls(supplies, maxima, experience_per_level)

    def supply(self, ship_id: int) -> ShipStats | None:
        return self.supply_by_ship_id.get(ship_id)

    def maximum(self, ship_id: int) -> ShipStats | None:
        return self.maximum_by_ship_id.get(ship_id)

    def experience_per_level(self, ship_id: int) -> int | None:
        return self.experience_per_level_by_ship_id.get(ship_id)


def _source_stats(value: object, source_id: int, label: str) -> ShipStats:
    if not isinstance(value, dict) or set(value) != {'atk', 'torpedo', 'def', 'airDef'}:
        raise ValueError(f'{label} schema 不匹配: {source_id}')
    raw = [int(value[field]) for field in ('atk', 'torpedo', 'def', 'airDef')]
    if any(item < 0 for item in raw):
        raise ValueError(f'{label}不能为负数: {source_id}')
    return ShipStats(raw[0], raw[1], raw[2], raw[3])


def _explicit_experience_per_level(item: dict[str, object], source_id: int) -> int:
    value = item.get('strengthenLevelUpExp')
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'每级强化经验必须是正整数: {source_id}')
    return value
