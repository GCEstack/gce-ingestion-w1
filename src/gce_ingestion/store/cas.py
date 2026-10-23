"""Filesystem content-addressed storage (CAS) for raw evidence blobs.

Path: <store_root>/EVIDENCE/blobs/sha256/<first-2-hex>/<64-hex>  (design C.3)
Identical payload -> shared blob. No in-place mutate/delete.
"""

from __future__ import annotations

import hashlib
import os

from .migrations import BLOB_ROOT_ALG


def blob_root(store_root: str) -> str:
    return os.path.join(store_root, "EVIDENCE", "blobs", BLOB_ROOT_ALG)


def _path_for(store_root: str, digest: str) -> str:
    if not (len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise ValueError("digest must be a 64-hex sha256")
    return os.path.join(blob_root(store_root), digest[:2], digest)


def put_blob(store_root: str, payload: bytes) -> str:
    """Store payload content-addressed; returns its sha256 hex digest.

    Identical payloads share one blob (dedup). Never overwrites an existing blob.
    """
    digest = hashlib.sha256(payload).hexdigest()
    target = _path_for(store_root, digest)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.exists(target):
        # Write via temp + atomic rename to avoid partial blobs.
        tmp = target + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    return digest


def get_blob(store_root: str, digest: str) -> bytes:
    with open(_path_for(store_root, digest), "rb") as fh:
        return fh.read()


def blob_exists(store_root: str, digest: str) -> bool:
    return os.path.exists(_path_for(store_root, digest))
