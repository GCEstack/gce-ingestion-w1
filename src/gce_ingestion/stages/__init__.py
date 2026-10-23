"""Pipeline stages package.

IMPORT-GRAPH RULE: this package (and realms/store/adapters/transport/control) must
NEVER import ``gce_ingestion.coda_reconcile``. CANON writes live only behind the
separate coda_reconcile entrypoint.
"""
