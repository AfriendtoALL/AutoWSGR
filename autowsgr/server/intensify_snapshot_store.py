"""Short-lived authoritative storage for immutable intensify inventory snapshots."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol


if TYPE_CHECKING:
    from autowsgr.ui.material_inventory_scanner import MaterialInventorySnapshot
    from autowsgr.ui.target_inventory_scanner import TargetInventorySnapshot


class IntensifySnapshotStoreError(LookupError):
    """Raised when a snapshot session is unavailable or expired."""


class SnapshotClock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class IntensifySnapshotSession:
    session_id: str
    target_snapshot: TargetInventorySnapshot
    material_snapshot: MaterialInventorySnapshot
    created_at: datetime
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IntensifySnapshotStore:
    """Thread-safe in-memory owner of short-lived immutable snapshot pairs."""

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(minutes=10),
        clock: SnapshotClock = _utc_now,
    ) -> None:
        if ttl <= timedelta():
            raise ValueError('强化快照 TTL 必须大于零')
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('强化快照时钟必须返回带时区时间')
        self._ttl = ttl
        self._clock = clock
        self._sessions: dict[str, IntensifySnapshotSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        target_snapshot: TargetInventorySnapshot,
        material_snapshot: MaterialInventorySnapshot,
    ) -> IntensifySnapshotSession:
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            session_id = self._new_session_id_locked()
            session = IntensifySnapshotSession(
                session_id=session_id,
                target_snapshot=target_snapshot,
                material_snapshot=material_snapshot,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> IntensifySnapshotSession:
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                raise IntensifySnapshotStoreError('强化快照会话不存在或已过期')
            return session

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def prune_expired(self) -> int:
        now = self._now()
        with self._lock:
            return self._prune_locked(now)

    def __len__(self) -> int:
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            return len(self._sessions)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('强化快照时钟必须返回带时区时间')
        return now

    def _new_session_id_locked(self) -> str:
        while True:
            session_id = secrets.token_urlsafe(32)
            if session_id not in self._sessions:
                return session_id

    def _prune_locked(self, now: datetime) -> int:
        expired = tuple(
            session_id
            for session_id, session in self._sessions.items()
            if now >= session.expires_at
        )
        for session_id in expired:
            del self._sessions[session_id]
        return len(expired)
