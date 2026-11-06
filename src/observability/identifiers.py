"""Stable correlation identifiers; these are not access credentials."""

import hashlib
from uuid import UUID


def task_id_for(namespace: str, event_key: str) -> str:
    return hashlib.sha256(f"{namespace}:{event_key}".encode()).hexdigest()


def trace_id_for(task_id: str | None, run_id: UUID | None = None) -> str:
    if task_id:
        return hashlib.sha256(task_id.encode()).hexdigest()[:32]
    if run_id:
        return run_id.hex
    raise ValueError("Trace correlation requires a task or run identifier")
