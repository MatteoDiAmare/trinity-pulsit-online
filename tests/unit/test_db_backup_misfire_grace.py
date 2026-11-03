"""The daily DB backup must survive a suspended host: a run missed by hours is executed late (once), not skipped."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)


def _service():
    try:
        from services import db_backup_service as mod
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return mod


def test_backup_job_tolerates_a_host_suspended_for_hours_and_coalesces_missed_runs():
    mod = _service()
    svc = mod.db_backup_service
    with mock.patch.object(mod, "DB_BACKUP_ENABLED", True), mock.patch.object(svc.scheduler, "add_job") as add, mock.patch.object(svc.scheduler, "start"):
        svc.start()
    kw = add.call_args.kwargs
    assert kw["id"] == "db_backup"
    assert kw["misfire_grace_time"] >= 24 * 3600, "a 5 h 38 min late wake-up (seen in production) must still run the backup"
    assert kw["coalesce"] is True, "several missed days must collapse into one run"
    assert kw["replace_existing"] is True


def test_the_run_is_day_keyed_so_running_late_cannot_create_duplicates():
    mod = _service()
    from db import backup_primitives as bp
    assert bp.sqlite_artifact_name("20261103") == bp.sqlite_artifact_name("20261103")
    assert bp.sqlite_artifact_name("20261103") != bp.sqlite_artifact_name("20261104")
