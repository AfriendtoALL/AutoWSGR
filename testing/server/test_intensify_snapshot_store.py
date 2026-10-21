from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest

from autowsgr.server.intensify_snapshot_store import (
    IntensifySnapshotSession,
    IntensifySnapshotStore,
    IntensifySnapshotStoreError,
)
from autowsgr.types import ShipType
from autowsgr.ui.intensify_workflow import SelectionRef, ShipStats
from autowsgr.ui.material_inventory_scanner import MaterialInventorySnapshot
from autowsgr.ui.target_inventory_scanner import (
    CardRect,
    TargetInventorySnapshot,
    TargetShipSnapshot,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _target_snapshot(revision: str = 'target-rev') -> TargetInventorySnapshot:
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
        ),
        1,
        True,
        revision,
    )


def _material_snapshot(revision: str = 'material-rev') -> MaterialInventorySnapshot:
    return MaterialInventorySnapshot(
        names=('素材舰',),
        ship_ids=(11,),
        total=1,
        viewport_count=1,
        refs=(f'material:{revision}:0:0:0:0.1000:0.2000',),
    )


def test_snapshot_store_creates_unpredictable_immutable_session() -> None:
    clock = _Clock()
    store = IntensifySnapshotStore(ttl=timedelta(minutes=10), clock=clock)
    targets = _target_snapshot()
    materials = _material_snapshot()

    first = store.create(targets, materials)
    second = store.create(targets, materials)

    assert isinstance(first, IntensifySnapshotSession)
    assert first.session_id != second.session_id
    assert len(first.session_id) >= 32
    assert first.target_snapshot is targets
    assert first.material_snapshot is materials
    assert first.created_at == clock.now
    assert first.expires_at == clock.now + timedelta(minutes=10)
    assert store.get(first.session_id) is first
    with pytest.raises(FrozenInstanceError):
        first.session_id = 'forged'  # type: ignore[misc]


def test_snapshot_store_fails_closed_for_unknown_or_expired_session() -> None:
    clock = _Clock()
    store = IntensifySnapshotStore(ttl=timedelta(seconds=30), clock=clock)
    session = store.create(_target_snapshot(), _material_snapshot())

    with pytest.raises(IntensifySnapshotStoreError, match='不存在或已过期'):
        store.get('unknown')

    clock.advance(30)
    with pytest.raises(IntensifySnapshotStoreError, match='不存在或已过期'):
        store.get(session.session_id)
    assert len(store) == 0


def test_snapshot_store_prunes_lazily_on_create_and_explicit_request() -> None:
    clock = _Clock()
    store = IntensifySnapshotStore(ttl=timedelta(seconds=10), clock=clock)
    expired = store.create(_target_snapshot('target-old'), _material_snapshot('material-old'))
    clock.advance(11)

    current = store.create(_target_snapshot('target-new'), _material_snapshot('material-new'))

    assert len(store) == 1
    assert store.get(current.session_id) is current
    assert store.prune_expired() == 0
    with pytest.raises(IntensifySnapshotStoreError):
        store.get(expired.session_id)


def test_snapshot_store_delete_is_scoped_and_idempotent() -> None:
    store = IntensifySnapshotStore()
    first = store.create(_target_snapshot(), _material_snapshot())
    second = store.create(_target_snapshot(), _material_snapshot())

    assert store.delete(first.session_id) is True
    assert store.delete(first.session_id) is False
    assert store.get(second.session_id) is second


def test_snapshot_store_rejects_invalid_ttl_or_naive_clock() -> None:
    with pytest.raises(ValueError, match='TTL'):
        IntensifySnapshotStore(ttl=timedelta())
    with pytest.raises(ValueError, match='时区'):
        IntensifySnapshotStore(clock=lambda: datetime(2026, 8, 21, tzinfo=None))  # noqa: DTZ001


def test_snapshot_store_serializes_concurrent_creation() -> None:
    store = IntensifySnapshotStore()
    session_ids: list[str] = []

    threads = [
        Thread(
            target=lambda: session_ids.append(
                store.create(_target_snapshot(), _material_snapshot()).session_id
            )
        )
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(session_ids) == 20
    assert len(set(session_ids)) == 20
    assert len(store) == 20
