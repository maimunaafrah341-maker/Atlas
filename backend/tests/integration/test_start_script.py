"""scripts/start.sh ordering: migrations first, seed only after they succeed, fail fast.

The real commands are replaced by stubs on PATH that record their calls.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import REPO_DIR

SH = shutil.which("sh")
START = REPO_DIR / "scripts" / "start.sh"

pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")


def run_start(tmp_path: Path, *, migrate_exit: int = 0, seed_exit: int = 0, seed_on: bool) -> tuple[int, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    stubs = {"alembic": migrate_exit, "python": seed_exit, "uvicorn": 0}
    for name, code in stubs.items():
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\nexit {code}\n', encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_LOG": str(log),
        "ATLAS_BACKEND_DIR": str(tmp_path),
        "SEED_CATALOG_ON_START": "1" if seed_on else "0",
    }
    result = subprocess.run([str(SH), str(START)], env=env, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result.returncode, [
        call.split()[0] + (" " + call.split()[1] if len(call.split()) > 1 else "") for call in calls
    ]


def test_migration_failure_stops_startup_before_seed_and_server(tmp_path: Path) -> None:
    code, calls = run_start(tmp_path, migrate_exit=1, seed_on=True)
    assert code != 0
    assert calls == ["alembic upgrade"]


def test_seed_runs_after_migrations_then_server_starts(tmp_path: Path) -> None:
    code, calls = run_start(tmp_path, seed_on=True)
    assert code == 0
    assert calls == ["alembic upgrade", "python -m", "uvicorn app.main:app_factory"]


def test_seed_failure_stops_startup(tmp_path: Path) -> None:
    code, calls = run_start(tmp_path, seed_exit=1, seed_on=True)
    assert code != 0
    assert calls == ["alembic upgrade", "python -m"]


def test_seed_skipped_unless_enabled(tmp_path: Path) -> None:
    code, calls = run_start(tmp_path, seed_on=False)
    assert code == 0
    assert calls == ["alembic upgrade", "uvicorn app.main:app_factory"]
