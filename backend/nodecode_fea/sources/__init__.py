from __future__ import annotations

import os
import threading

from .abaqus import AbaqusSource, abaqus_available, abaqus_command
from .base import NotReady, ResultSource, SourceError
from .mock import MockSource

__all__ = ["NotReady", "ResultSource", "SourceError", "open_source", "source_version", "abaqus_available", "abaqus_command"]

_cache: dict[str, tuple[float, ResultSource]] = {}
_lock = threading.Lock()


def source_version(path: str) -> float:
    """A value that changes when the ODB on disk changes (e.g. the job was re-run)."""
    path = path.strip()
    if path.startswith("mock://"):
        return 0.0
    try:
        version = os.path.getmtime(path)
    except OSError:
        return -1.0
    stem = os.path.splitext(path)[0]
    if os.path.exists(stem + ".lck"):  # a running job: each increment it finishes adds a line to its .sta
        try:
            version = max(version, os.path.getmtime(stem + ".sta"))
        except OSError:
            pass
    return version


def open_source(path: str) -> ResultSource:
    """Return a (cached) source for an ODB path or a mock:// URL, reopening it if the file changed."""
    path = path.strip()
    if not path:
        raise SourceError("No ODB path given")
    version = source_version(path)
    with _lock:
        cached = _cache.get(path)
        if cached and cached[0] != version:
            cached[1].close()
            cached = None
        if cached is None:
            src = MockSource(path) if path.startswith("mock://") else AbaqusSource(path)
            cached = _cache[path] = (version, src)
        return cached[1]
