"""Read-only application service for authoritative intensify snapshot previews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autowsgr.server.intensify_snapshot_store import (
    IntensifySnapshotStore,
    IntensifySnapshotStoreError,
)
from autowsgr.server.serializers import serialize_intensify_candidate_preview
from autowsgr.ui.intensify_inventory_semantics import (
    ShipLibraryRarityResolver,
    intensify_candidate_preview,
    material_inventory_observation,
)
from autowsgr.ui.material_inventory_scanner import MaterialInventoryScanError
from autowsgr.ui.target_strengthen_max import ShipStrengthenDataResolver


if TYPE_CHECKING:
    from autowsgr.ui.intensify_workflow import IntensifyPolicy, SelectionRef


class IntensifyPreviewError(RuntimeError):
    """Base error for read-only snapshot preview assembly."""


class IntensifyPreviewSessionUnavailableError(IntensifyPreviewError):
    """Raised when the opaque snapshot session cannot be used."""


class IntensifyPreviewSelectionError(IntensifyPreviewError):
    """Raised when selected material references violate snapshot authority."""


class IntensifyPreviewDataError(IntensifyPreviewError):
    """Raised when trusted authoritative source data cannot be loaded."""


@dataclass(frozen=True, slots=True)
class IntensifyPreviewCommand:
    """Caller intent; authoritative paths remain service-owned dependencies."""

    session_id: str
    selected_target_ref: SelectionRef
    policy: IntensifyPolicy
    selected_material_refs: tuple[SelectionRef, ...]

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError('session_id 不能为空')


class IntensifyPreviewService:
    """Build stable JSON previews without touching a device or execution state."""

    def __init__(
        self,
        snapshot_store: IntensifySnapshotStore,
        strengthen_path: str | Path,
        manifest_path: str | Path,
    ) -> None:
        self._snapshot_store = snapshot_store
        self._strengthen_path = Path(strengthen_path)
        self._manifest_path = Path(manifest_path)

    def preview(self, command: IntensifyPreviewCommand) -> dict[str, Any]:
        try:
            session = self._snapshot_store.get(command.session_id)
        except IntensifySnapshotStoreError as error:
            raise IntensifyPreviewSessionUnavailableError('强化快照会话不可用') from error

        selected_targets = tuple(
            target
            for target in session.target_snapshot.targets
            if target.ref == command.selected_target_ref
        )
        if len(selected_targets) != 1:
            raise IntensifyPreviewSelectionError('强化目标选择不可用')
        selected_snapshot = type(session.target_snapshot)(
            targets=selected_targets,
            total=1,
            complete=True,
            revision=session.target_snapshot.revision,
        )

        try:
            strengthen = ShipStrengthenDataResolver.from_source(self._strengthen_path)
            rarities = ShipLibraryRarityResolver.from_manifest(self._manifest_path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise IntensifyPreviewDataError('权威强化数据不可用') from error

        try:
            materials = material_inventory_observation(
                session.material_snapshot,
                strengthen,
                rarities,
            )
            preview = intensify_candidate_preview(
                selected_snapshot,
                materials,
                strengthen,
                command.policy,
            )
        except MaterialInventoryScanError as error:
            raise IntensifyPreviewDataError('权威强化数据不可用') from error

        if command.selected_material_refs:
            try:
                preview = intensify_candidate_preview(
                    selected_snapshot,
                    materials,
                    strengthen,
                    command.policy,
                    projected_material_refs=command.selected_material_refs,
                )
            except MaterialInventoryScanError as error:
                raise IntensifyPreviewSelectionError('强化素材选择不可用') from error

        return serialize_intensify_candidate_preview(preview)
