from __future__ import annotations

import json
from typing import Literal

import pytest

from autowsgr.server.serializers import (
    serialize_intensify_candidate_preview,
    serialize_intensify_material_inventory,
    serialize_intensify_target_inventory,
)
from autowsgr.types import ShipType
from autowsgr.ui.intensify_inventory_semantics import (
    IntensifyCandidatePreview,
    MaterialCandidatePreview,
    TargetCandidatePreview,
)
from autowsgr.ui.intensify_workflow import SelectionRef, ShipStats
from autowsgr.ui.material_inventory_scanner import MaterialInventorySnapshot
from autowsgr.ui.target_inventory_scanner import (
    CardRect,
    TargetInventorySnapshot,
    TargetShipSnapshot,
)


def _preview(
    *, execution_path: Literal['direct', 'confirmation_required'] | None
) -> IntensifyCandidatePreview:
    return IntensifyCandidatePreview(
        targets=(
            TargetCandidatePreview(
                ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
                ship_id=7,
                identity='目标舰',
                occurrence=0,
                current=ShipStats(armor=4),
                maximum=ShipStats(armor=5),
                deficit=ShipStats(armor=1),
                projected_gains=ShipStats(armor=2),
                projected=ShipStats(armor=6),
                needs_intensify=True,
            ),
        ),
        materials=(
            MaterialCandidatePreview(
                ref=SelectionRef('material:material-rev:0:0:0:0.1000:0.2000'),
                identity='素材舰',
                index=0,
                contribution=ShipStats(armor=25, anti_air=5),
                rarity=4,
                requires_confirmation=True,
                eligible=True,
                reason='allowlisted_nonzero_contribution',
            ),
        ),
        target_revision='target-rev',
        material_revision='material-rev',
        execution_path=execution_path,
    )


def test_intensify_candidate_preview_has_stable_json_contract() -> None:
    preview = _preview(execution_path='confirmation_required')

    payload = serialize_intensify_candidate_preview(preview)

    assert payload == {
        'targetRevision': 'target-rev',
        'materialRevision': 'material-rev',
        'executionPath': 'confirmation_required',
        'executable': False,
        'targets': [
            {
                'ref': 'target:target-rev:0:0:0:0.1000:0.2000',
                'shipId': 7,
                'identity': '目标舰',
                'occurrence': 0,
                'current': {'firepower': 0, 'torpedo': 0, 'armor': 4, 'antiAir': 0},
                'maximum': {'firepower': 0, 'torpedo': 0, 'armor': 5, 'antiAir': 0},
                'deficit': {'firepower': 0, 'torpedo': 0, 'armor': 1, 'antiAir': 0},
                'projectedGains': {
                    'firepower': 0,
                    'torpedo': 0,
                    'armor': 2,
                    'antiAir': 0,
                },
                'projected': {'firepower': 0, 'torpedo': 0, 'armor': 6, 'antiAir': 0},
                'needsIntensify': True,
            }
        ],
        'materials': [
            {
                'ref': 'material:material-rev:0:0:0:0.1000:0.2000',
                'identity': '素材舰',
                'index': 0,
                'contribution': {
                    'firepower': 0,
                    'torpedo': 0,
                    'armor': 25,
                    'antiAir': 5,
                },
                'rarity': 4,
                'requiresConfirmation': True,
                'eligible': True,
                'reason': 'allowlisted_nonzero_contribution',
            }
        ],
    }
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
    assert preview.targets[0].ref.value.startswith('target:')


def test_intensify_candidate_preview_preserves_unknown_execution_path_as_null() -> None:
    payload = serialize_intensify_candidate_preview(_preview(execution_path=None))

    assert payload['executionPath'] is None
    assert payload['executable'] is False
    assert 'validationProof' not in json.dumps(payload)


def test_intensify_session_inventory_serializers_expose_only_selection_contract() -> None:
    targets = TargetInventorySnapshot(
        targets=(
            TargetShipSnapshot(
                ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
                ship_id=7,
                name='目标舰',
                ship_type=ShipType.DD,
                levels=ShipStats(firepower=1, torpedo=2, armor=3, anti_air=4),
                card=CardRect(10, 20, 30, 40),
                visual_hash=123,
                identity_confidence=0.99,
                identity_match_key='private/gallery/7.png',
                global_index=0,
                occurrence=0,
            ),
        ),
        total=1,
        complete=True,
        revision='target-rev',
    )
    materials = MaterialInventorySnapshot(
        names=('素材舰',),
        ship_ids=(11,),
        total=1,
        viewport_count=1,
        refs=('material:material-rev:0:0:0:0.1000:0.2000',),
    )

    payload = {
        'targets': serialize_intensify_target_inventory(targets),
        'materials': serialize_intensify_material_inventory(materials),
    }

    assert payload == {
        'targets': [
            {
                'ref': 'target:target-rev:0:0:0:0.1000:0.2000',
                'shipId': 7,
                'identity': '目标舰',
                'occurrence': 0,
                'current': {'firepower': 1, 'torpedo': 2, 'armor': 3, 'antiAir': 4},
            },
        ],
        'materials': [
            {
                'ref': 'material:material-rev:0:0:0:0.1000:0.2000',
                'shipId': 11,
                'identity': '素材舰',
                'index': 0,
            },
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ('card', 'visual_hash', 'identity_confidence', 'identity_match_key'):
        assert forbidden not in serialized
    assert 'private/gallery' not in serialized


def test_material_inventory_serializer_rejects_misaligned_snapshot() -> None:
    snapshot = MaterialInventorySnapshot(
        names=('素材舰',),
        ship_ids=(11,),
        total=2,
        viewport_count=1,
        refs=('material:material-rev:0:0:0:0.1000:0.2000',),
    )

    with pytest.raises(ValueError, match='不一致'):
        serialize_intensify_material_inventory(snapshot)
