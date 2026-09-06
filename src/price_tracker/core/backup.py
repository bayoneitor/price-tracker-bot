"""Scheduled snapshots of the database.

The database is a single file on a single named volume, and the documented way
to copy it was a command an operator runs by hand — so in practice nobody was.
Losing that volume, or a migration going wrong, took everything with it.

A snapshot is taken through SQLite's online backup API rather than by copying the
file: a copy taken while the bot is writing can land mid-transaction, and the WAL
means the file on disk is not the whole story anyway. The API produces a
consistent database without stopping the bot.

Snapshots live beside the database, on the same volume. That covers the failure
this addresses — a bad migration, a corrupted page, a delete nobody meant — and
not the loss of the volume itself; copying them off the host stays the operator's
job, and `docs/operations.md` says so.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "backups"
KEEP_SNAPSHOTS = 7


def snapshot_path(database_path: str, *, stamp: str) -> Path:
    """Where the snapshot taken at `stamp` belongs."""
    database = Path(database_path)
    return database.parent / BACKUP_DIRNAME / f"{database.stem}-{stamp}.db"


def take_snapshot(database_path: str, *, stamp: str, keep: int = KEEP_SNAPSHOTS) -> Path | None:
    """Copy the database to a dated file beside it, and prune old ones.

    Blocking: SQLite's backup API is synchronous, so callers on the event loop
    must hand this to a thread. Returns the file written, or None when the
    database is not there yet — a first run before anything has been tracked.
    """
    source = Path(database_path)
    if not source.exists():
        logger.info("No database at %s yet; nothing to snapshot", source)
        return None

    target = snapshot_path(database_path, stamp=stamp)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Read-only on the source: a snapshot must never be the thing that writes.
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live,
        sqlite3.connect(target) as copy,
    ):
        live.backup(copy)

    _prune(target.parent, stem=source.stem, keep=keep)
    return target


def _prune(directory: Path, *, stem: str, keep: int) -> None:
    """Keep the newest `keep` snapshots and delete the rest.

    Sorted by name, which the timestamp makes chronological — reading the
    filesystem's own times would be at the mercy of whatever last touched them.
    """
    snapshots = sorted(directory.glob(f"{stem}-*.db"))
    for stale in snapshots[:-keep] if keep > 0 else snapshots:
        try:
            stale.unlink()
        except OSError as exc:  # noqa: PERF203 — one bad file must not stop the prune
            logger.warning("Could not remove old snapshot %s: %s", stale, exc)
