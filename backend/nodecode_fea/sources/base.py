"""The interface every results source implements.

A source wraps one output database. The real implementation talks to a worker
process running inside Abaqus Python; the mock implementation synthesises an
analytical cantilever so the editor can be developed without an Abaqus install.

All methods return plain dicts whose array members are numpy arrays:

describe() -> {
    "path": str,
    "steps": [{"name", "description",
               "frames": [{"index", "value", "description"}],
               "fields": [{"name", "description", "type", "components", "positions"}],
               "historyRegions": [{"name", "outputs": [{"name", "description"}]}]}],
    "instances": [{"name", "nodes", "elements"}],
    "sets": [{"name", "kind"}],          # kind is "node" or "element"
}
field(step, frame, variable) -> {
    "position": "NODAL" | "ELEMENT", "type": str, "components": [str],
    "instances": ndarray[str], "labels": ndarray[int], "values": ndarray[float, (n, ncomp)],
    "frameValue": float,
}
set(name) -> {"kind": "node" | "element", "instances": ndarray[str], "labels": ndarray[int]}
history(step, region, output) -> {"x": ndarray, "y": ndarray}
mesh() -> {"instances": [{"name", "nodeLabels": ndarray[int], "coords": ndarray[(n, 3)],
                          "elementLabels": ndarray[int], "elementTypes": [str],
                          "connectivity": [[int]]}]}
"""

from __future__ import annotations

from typing import Any


class SourceError(Exception):
    """Raised when a source cannot satisfy a request (bad step name, missing field...)."""


class NotReady(SourceError):
    """Not an error: what's asked for doesn't exist yet (the results of a job still to run, or running)."""


class ResultSource:
    path: str

    def describe(self) -> dict[str, Any]:
        raise NotImplementedError

    def field(self, step: str, frame: int, variable: str, position: str = "Automatic",
              section_point: int = 0, complex_part: str = "Real", phase_angle: float = 0.0) -> dict[str, Any]:
        raise NotImplementedError

    def set(self, name: str) -> dict[str, Any]:
        raise NotImplementedError

    def history(self, step: str, region: str, output: str) -> dict[str, Any]:
        raise NotImplementedError

    def mesh(self) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass
