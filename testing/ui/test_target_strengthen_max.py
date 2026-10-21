from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from autowsgr.ui.intensify_workflow import ShipStats
from autowsgr.ui.target_strengthen_max import (
    ShipStrengthenDataResolver,
    TargetStrengthenMaxResolver,
    source_experience_per_level,
    source_to_canonical_ship_id,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_source_id_mapping_and_refit_experience() -> None:
    assert source_to_canonical_ship_id(10057012) == 570
    assert source_to_canonical_ship_id(11008411) == 1084
    assert source_experience_per_level(10057012) == 20
    assert source_experience_per_level(11008411) == 12


def test_resolver_uses_ceiling_division(tmp_path: Path) -> None:
    source = [
        {
            'id': 10000113,
            'title': '舰',
            'strengthenLevelUpExp': 25,
            'strengthenSupply': {'atk': 0, 'torpedo': 0, 'def': 0, 'airDef': 0},
            'strengthenMax': {'atk': 625, 'torpedo': 0, 'def': 375, 'airDef': 750},
        }
    ]
    path = tmp_path / 'strengthen.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    resolver = TargetStrengthenMaxResolver.from_source(path)
    assert resolver(1) == ShipStats(25, 0, 15, 30)


def test_combined_resolver_preserves_supply_and_converts_maximum(tmp_path: Path) -> None:
    source = [
        {
            'id': 10000113,
            'title': '舰',
            'strengthenLevelUpExp': 25,
            'strengthenSupply': {'atk': 2, 'torpedo': 3, 'def': 4, 'airDef': 5},
            'strengthenMax': {'atk': 625, 'torpedo': 0, 'def': 375, 'airDef': 750},
        }
    ]
    path = tmp_path / 'strengthen.json'
    path.write_text(json.dumps(source), encoding='utf-8')

    resolver = ShipStrengthenDataResolver.from_source(path)

    assert resolver.supply(1) == ShipStats(2, 3, 4, 5)
    assert resolver.maximum(1) == ShipStats(25, 0, 15, 30)
    assert resolver.experience_per_level(1) == 25
    assert resolver.supply(999) is None
    assert resolver.experience_per_level(999) is None


@pytest.mark.parametrize('value', [None, 0, -1, True, 1.5, '25'])
def test_resolver_rejects_missing_or_invalid_explicit_level_experience(
    tmp_path: Path, value: object
) -> None:
    record = {
        'id': 10000113,
        'title': '舰',
        'strengthenSupply': {'atk': 2, 'torpedo': 3, 'def': 4, 'airDef': 5},
        'strengthenMax': {'atk': 625, 'torpedo': 0, 'def': 375, 'airDef': 750},
    }
    if value is not None:
        record['strengthenLevelUpExp'] = value
    path = tmp_path / 'strengthen.json'
    path.write_text(json.dumps([record]), encoding='utf-8')

    with pytest.raises(ValueError, match='每级强化经验'):
        ShipStrengthenDataResolver.from_source(path)
