from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from autowsgr.server.intensify_preview_service import (
    IntensifyPreviewCommand,
    IntensifyPreviewDataError,
    IntensifyPreviewSelectionError,
    IntensifyPreviewService,
    IntensifyPreviewSessionUnavailableError,
)
from autowsgr.server.intensify_snapshot_store import IntensifySnapshotStore
from autowsgr.types import ShipType
from autowsgr.ui.intensify_workflow import IntensifyPolicy, SelectionRef, ShipStats
from autowsgr.ui.material_inventory_scanner import MaterialInventorySnapshot
from autowsgr.ui.target_inventory_scanner import (
    CardRect,
    TargetInventorySnapshot,
    TargetShipSnapshot,
)


if TYPE_CHECKING:
    from pathlib import Path


def _target_snapshot(*, revision: str = 'target-rev') -> TargetInventorySnapshot:
    return TargetInventorySnapshot(
        (
            TargetShipSnapshot(
                ref=SelectionRef(f'target:{revision}:0:0:0:0.1000:0.2000'),
                ship_id=7,
                name='目标舰',
                ship_type=ShipType.DD,
                levels=ShipStats(armor=4),
                card=CardRect(10, 10, 110, 210),
                visual_hash=1,
                identity_confidence=0.9,
                identity_match_key='gallery/7.png',
                global_index=0,
                occurrence=0,
            ),
            TargetShipSnapshot(
                ref=SelectionRef(f'target:{revision}:0:0:1:0.2000:0.2000'),
                ship_id=8,
                name='第二目标舰',
                ship_type=ShipType.CL,
                levels=ShipStats(armor=5),
                card=CardRect(120, 10, 220, 210),
                visual_hash=2,
                identity_confidence=0.9,
                identity_match_key='gallery/8.png',
                global_index=1,
                occurrence=0,
            ),
        ),
        2,
        True,
        revision,
    )


def _material_snapshot() -> MaterialInventorySnapshot:
    return MaterialInventorySnapshot(
        names=('素材舰',),
        ship_ids=(11,),
        total=1,
        viewport_count=1,
        refs=('material:material-rev:0:0:0:0.1000:0.2000',),
    )


def _write_sources(tmp_path: Path) -> tuple[Path, Path]:
    strengthen = tmp_path / 'strengthen.json'
    strengthen.write_text(
        json.dumps(
            [
                {
                    'id': 10000712,
                    'title': '目标舰',
                    'strengthenLevelUpExp': 20,
                    'strengthenSupply': {'atk': 1, 'torpedo': 1, 'def': 1, 'airDef': 1},
                    'strengthenMax': {'atk': 0, 'torpedo': 0, 'def': 100, 'airDef': 0},
                },
                {
                    'id': 10000812,
                    'title': '第二目标舰',
                    'strengthenLevelUpExp': 20,
                    'strengthenSupply': {'atk': 1, 'torpedo': 1, 'def': 1, 'airDef': 1},
                    'strengthenMax': {'atk': 0, 'torpedo': 0, 'def': 100, 'airDef': 0},
                },
                {
                    'id': 10001112,
                    'title': '素材舰',
                    'strengthenLevelUpExp': 20,
                    'strengthenSupply': {'atk': 0, 'torpedo': 0, 'def': 40, 'airDef': 0},
                    'strengthenMax': {'atk': 0, 'torpedo': 0, 'def': 20, 'airDef': 0},
                },
            ]
        ),
        encoding='utf-8',
    )
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(
        json.dumps({'ships': [{'id': 11, 'rarity': 3}]}),
        encoding='utf-8',
    )
    return strengthen, manifest


def test_preview_service_uses_authoritative_session_and_returns_stable_json(
    tmp_path: Path,
) -> None:
    store = IntensifySnapshotStore()
    session = store.create(_target_snapshot(), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    service = IntensifyPreviewService(store, strengthen, manifest)
    command = IntensifyPreviewCommand(
        session_id=session.session_id,
        selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
        policy=IntensifyPolicy(frozenset({'素材舰'})),
        selected_material_refs=(SelectionRef('material:material-rev:0:0:0:0.1000:0.2000'),),
    )

    payload = service.preview(command)

    assert payload['targetRevision'] == 'target-rev'
    assert payload['materialRevision'] == 'material-rev'
    assert payload['executionPath'] == 'direct'
    assert payload['executable'] is False
    assert payload['targets'][0]['projectedGains']['armor'] == 2
    assert len(payload['targets']) == 1
    assert payload['targets'][0]['ref'] == 'target:target-rev:0:0:0:0.1000:0.2000'
    assert payload['materials'][0]['rarity'] == 3
    assert store.get(session.session_id) is session


def test_preview_service_ignores_missing_data_for_unselected_target(tmp_path: Path) -> None:
    store = IntensifySnapshotStore()
    session = store.create(_target_snapshot(), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    records = json.loads(strengthen.read_text(encoding='utf-8'))
    strengthen.write_text(
        json.dumps([record for record in records if record['id'] != 10000812]),
        encoding='utf-8',
    )
    service = IntensifyPreviewService(store, strengthen, manifest)

    payload = service.preview(
        IntensifyPreviewCommand(
            session_id=session.session_id,
            selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
            policy=IntensifyPolicy(frozenset({'素材舰'})),
            selected_material_refs=(),
        )
    )

    assert [target['shipId'] for target in payload['targets']] == [7]


def test_preview_service_hides_unknown_and_expired_session_distinction(tmp_path: Path) -> None:
    strengthen, manifest = _write_sources(tmp_path)
    service = IntensifyPreviewService(IntensifySnapshotStore(), strengthen, manifest)
    command = IntensifyPreviewCommand(
        session_id='unknown',
        selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
        policy=IntensifyPolicy(frozenset({'素材舰'})),
        selected_material_refs=(),
    )

    with pytest.raises(IntensifyPreviewSessionUnavailableError, match='不可用'):
        service.preview(command)


def test_preview_service_rejects_forged_material_ref_as_selection_error(tmp_path: Path) -> None:
    store = IntensifySnapshotStore()
    session = store.create(_target_snapshot(), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    service = IntensifyPreviewService(store, strengthen, manifest)

    with pytest.raises(IntensifyPreviewSelectionError, match='素材选择'):
        service.preview(
            IntensifyPreviewCommand(
                session_id=session.session_id,
                selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
                policy=IntensifyPolicy(frozenset({'素材舰'})),
                selected_material_refs=(SelectionRef('material:forged-rev:0:0:0:0.1000:0.2000'),),
            )
        )


def test_preview_service_rejects_forged_target_ref_as_selection_error(tmp_path: Path) -> None:
    store = IntensifySnapshotStore()
    session = store.create(_target_snapshot(), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    service = IntensifyPreviewService(store, strengthen, manifest)

    with pytest.raises(IntensifyPreviewSelectionError, match='目标选择'):
        service.preview(
            IntensifyPreviewCommand(
                session_id=session.session_id,
                selected_target_ref=SelectionRef('target:forged-rev:0:0:0:0.1000:0.2000'),
                policy=IntensifyPolicy(frozenset({'素材舰'})),
                selected_material_refs=(),
            )
        )


def test_preview_service_rejects_target_ref_from_another_session(tmp_path: Path) -> None:
    store = IntensifySnapshotStore()
    first = store.create(_target_snapshot(revision='target-first'), _material_snapshot())
    second = store.create(_target_snapshot(revision='target-second'), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    service = IntensifyPreviewService(store, strengthen, manifest)

    with pytest.raises(IntensifyPreviewSelectionError, match='目标选择'):
        service.preview(
            IntensifyPreviewCommand(
                session_id=second.session_id,
                selected_target_ref=first.target_snapshot.targets[0].ref,
                policy=IntensifyPolicy(frozenset({'素材舰'})),
                selected_material_refs=(),
            )
        )


def test_preview_service_sanitizes_authoritative_data_failure(tmp_path: Path) -> None:
    store = IntensifySnapshotStore()
    session = store.create(_target_snapshot(), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    strengthen.write_text('{broken', encoding='utf-8')
    service = IntensifyPreviewService(store, strengthen, manifest)

    with pytest.raises(IntensifyPreviewDataError, match='权威强化数据不可用') as exc_info:
        service.preview(
            IntensifyPreviewCommand(
                session_id=session.session_id,
                selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
                policy=IntensifyPolicy(frozenset({'素材舰'})),
                selected_material_refs=(),
            )
        )

    assert str(strengthen) not in str(exc_info.value)


def test_preview_service_classifies_missing_target_data_as_data_error(tmp_path: Path) -> None:
    store = IntensifySnapshotStore()
    session = store.create(_target_snapshot(), _material_snapshot())
    strengthen, manifest = _write_sources(tmp_path)
    records = json.loads(strengthen.read_text(encoding='utf-8'))
    strengthen.write_text(json.dumps(records[1:]), encoding='utf-8')
    service = IntensifyPreviewService(store, strengthen, manifest)

    with pytest.raises(IntensifyPreviewDataError, match='权威强化数据不可用'):
        service.preview(
            IntensifyPreviewCommand(
                session_id=session.session_id,
                selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
                policy=IntensifyPolicy(frozenset({'素材舰'})),
                selected_material_refs=(),
            )
        )


def test_preview_command_rejects_empty_session_id() -> None:
    with pytest.raises(ValueError, match='session_id'):
        IntensifyPreviewCommand(
            session_id=' ',
            selected_target_ref=SelectionRef('target:target-rev:0:0:0:0.1000:0.2000'),
            policy=IntensifyPolicy(frozenset({'素材舰'})),
            selected_material_refs=(),
        )
