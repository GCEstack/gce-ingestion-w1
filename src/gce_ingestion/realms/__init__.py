"""Realm definitions and store-root layout for gce_ingestion.

Five realms: EVIDENCE / DERIVED / CANON / VIEW / CONTROL. Calibro design C.1-C.4.
"""

from __future__ import annotations

# (realm, relative_dir, filename)
REALM_FILES: dict[str, tuple[str, str]] = {
    "CONTROL": ("CONTROL", "control.sqlite"),
    "EVIDENCE": ("EVIDENCE", "evidence.sqlite"),
    "DERIVED": ("DERIVED", "derived.sqlite"),
    "VIEW": ("VIEW", "view.sqlite"),
    "CANON": ("CANON", "canon.sqlite"),
}

# Blob CAS lives under EVIDENCE/blobs/sha256/<first2>/<hash64>
BLOB_ROOT_ALG = "sha256"

# Pipeline stage -> set of realms that stage may WRITE. Every stage forbids CANON.
# (Calibro design C.2 capability matrix.)
STAGE_WRITE_REALMS: dict[str, frozenset] = {
    "acquire": frozenset({"CONTROL", "EVIDENCE"}),
    "normalize": frozenset({"CONTROL", "DERIVED"}),
    "classify": frozenset({"CONTROL", "DERIVED"}),
    "extract": frozenset({"CONTROL", "DERIVED"}),
    "materialize": frozenset({"CONTROL", "VIEW"}),
    "reconcile-prep": frozenset({"CONTROL", "DERIVED"}),
}

# The stage that is ALLOWED to write CANON. Not part of the importable pipeline.
CANON_WRITE_STAGE = "coda_reconcile"

ALL_REALMS = frozenset(REALM_FILES.keys())
PIPELINE_STAGES = frozenset(STAGE_WRITE_REALMS.keys())


class RealmViolation(Exception):
    """Raised when a stage attempts a write it does not have capability for.

    Notably: pipeline stages may never write CANON.
    """
