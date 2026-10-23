"""M10: separate coda_reconcile entrypoint + pipeline import-graph isolation."""

import os
import subprocess
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def test_pipeline_import_does_not_pull_coda_reconcile():
    """Importing the pipeline package graph must NOT import coda_reconcile.

    Runs in a subprocess so module state is fresh.
    """
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import gce_ingestion.adapters.kimi, gce_ingestion.realms.handles, "
        "gce_ingestion.store.migrations, gce_ingestion.control.run, "
        "gce_ingestion.stages.acquire; "
        "assert 'gce_ingestion.coda_reconcile' not in sys.modules, "
        "'pipeline graph imported coda_reconcile'"
    )
    env = dict(os.environ, PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, "-c", code, SRC],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


def test_coda_reconcile_can_write_canon(store_root):
    from gce_ingestion import coda_reconcile
    conn = coda_reconcile.open_canon_write(store_root)
    try:
        rec_id = coda_reconcile.write_canon_record(conn, "conv1", "CANDIDATE",
                                                   "content")
        row = conn.execute(
            "SELECT * FROM canon_record WHERE record_id=?", (rec_id,)).fetchone()
        assert row and row["authority_state"] == "CANDIDATE"
    finally:
        conn.close()


def test_coda_reconcile_rejects_verified(store_root):
    from gce_ingestion import coda_reconcile
    conn = coda_reconcile.open_canon_write(store_root)
    try:
        with pytest.raises(ValueError):
            coda_reconcile.write_canon_record(conn, "conv1", "VERIFIED", "c")
    finally:
        conn.close()
