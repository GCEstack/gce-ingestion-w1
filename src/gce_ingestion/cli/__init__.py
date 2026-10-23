"""CLI entrypoints for gce_ingestion.

Each module is a separate entrypoint so a stage can never be reached by importing a
different stage's code path by accident.
"""
