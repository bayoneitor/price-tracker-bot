"""The database has to be copied by something other than an operator remembering.

It lives as one file on one named volume, and the documented backup was a command
to run by hand — so in practice nothing took one. A bad migration, a corrupted
page or a delete nobody meant took everything with it.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from price_tracker.core.backup import snapshot_path, take_snapshot

if TYPE_CHECKING:
    from pathlib import Path


def _database(path: Path, rows: int = 3) -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO products(name) VALUES (?)", [(f"Widget {i}",) for i in range(rows)]
        )
    return path


def test_a_snapshot_is_a_readable_copy_of_the_data(tmp_path: Path) -> None:
    source = _database(tmp_path / "pricetracker.db")

    written = take_snapshot(str(source), stamp="20260907T120000Z")

    assert written is not None
    with sqlite3.connect(written) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3


def test_it_lands_beside_the_database_under_a_dated_name(tmp_path: Path) -> None:
    source = _database(tmp_path / "pricetracker.db")

    written = take_snapshot(str(source), stamp="20260907T120000Z")

    assert written == snapshot_path(str(source), stamp="20260907T120000Z")
    assert written.parent == tmp_path / "backups"
    assert written.name == "pricetracker-20260907T120000Z.db"


def test_the_live_database_is_opened_read_only(tmp_path: Path) -> None:
    """A snapshot must never be the thing that writes to the database."""
    source = _database(tmp_path / "pricetracker.db")
    before = source.read_bytes()

    take_snapshot(str(source), stamp="20260907T120000Z")

    assert source.read_bytes() == before


def test_a_missing_database_is_not_an_error(tmp_path: Path) -> None:
    """First run, before anything has been tracked."""
    assert take_snapshot(str(tmp_path / "absent.db"), stamp="20260907T120000Z") is None


def test_old_snapshots_are_pruned_newest_kept(tmp_path: Path) -> None:
    source = _database(tmp_path / "pricetracker.db")

    for day in range(1, 11):
        take_snapshot(str(source), stamp=f"202609{day:02d}T120000Z", keep=3)

    kept = sorted(p.name for p in (tmp_path / "backups").glob("*.db"))
    assert kept == [
        "pricetracker-20260908T120000Z.db",
        "pricetracker-20260909T120000Z.db",
        "pricetracker-20260910T120000Z.db",
    ]


def test_another_database_in_the_same_folder_is_left_alone(tmp_path: Path) -> None:
    """Pruning matches on the database's own stem, not on every .db it finds."""
    source = _database(tmp_path / "pricetracker.db")
    take_snapshot(str(source), stamp="20260901T120000Z", keep=1)
    stranger = tmp_path / "backups" / "somethingelse-20260101T000000Z.db"
    stranger.write_bytes(b"not ours")

    take_snapshot(str(source), stamp="20260902T120000Z", keep=1)

    assert stranger.exists()


@pytest.mark.asyncio
async def test_the_job_reports_a_failure_without_stopping_the_bot(tmp_path: Path) -> None:
    """Losing a backup is bad; stopping price checks over a full disk is worse."""
    from unittest.mock import MagicMock

    from price_tracker.main import backup_job

    context = MagicMock()
    context.bot_data = {"config": MagicMock(database_path=str(tmp_path / "nope" / "x.db"))}

    await backup_job(context)  # must not raise
