from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from autowsgr.server.intensify_snapshot_scan_service import (
    IntensifySnapshotScanError,
    IntensifySnapshotScanService,
)


def test_scan_service_stores_only_after_both_snapshots_exist() -> None:
    store = MagicMock()
    targets = MagicMock()
    materials = MagicMock()
    expected = MagicMock()
    store.create.return_value = expected
    service = IntensifySnapshotScanService(store, MagicMock(return_value=(targets, materials)))

    assert service.create_session() is expected
    store.create.assert_called_once_with(targets, materials)


def test_scan_service_does_not_create_partial_session_after_scan_failure() -> None:
    store = MagicMock()
    service = IntensifySnapshotScanService(
        store,
        MagicMock(side_effect=RuntimeError('material scan failed')),
    )

    with pytest.raises(IntensifySnapshotScanError, match='未创建'):
        service.create_session()

    store.create.assert_not_called()
