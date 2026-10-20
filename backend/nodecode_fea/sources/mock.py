"""A synthetic ODB: a cantilever beam with a tip load, from Euler-Bernoulli theory.

Open it with a path like ``mock://cantilever?load=1000&length=100``. Supported
parameters: ``load`` (N, downward), ``length``, ``height``, ``width`` (mm),
``E`` (MPa), ``nx``, ``ny``, ``nz`` (element counts), ``frames``, and ``instances``:
with instances=2 the assembly holds BEAM-1 and BEAM-2 side by side, both meshed with
the same node and element labels (as real part instances are), BEAM-k carrying k times
the load. Assembly-level sets (FIXED, TIP…) then span every instance. ``rp=1`` adds an
assembly-level reference point (node 1 of the assembly itself, no instance), tied to
BEAM-1's tip, with sets RP and TIP_AND_RP (BEAM-1's tip nodes plus the RP). ``warnings=1`` adds the sets Abaqus
makes for what it warns about: WarnElemDistorted (the root elements) and WarnNodeOverconstr (some fixed nodes).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import numpy as np

from .base import ResultSource, SourceError

INSTANCE = "BEAM-1"
STEP = "Load"


class MockSource(ResultSource):
    def __init__(self, path: str):
        self.path = path
        url = urlparse(path)
        if url.netloc not in ("cantilever", ""):
            raise SourceError(f"Unknown mock model '{url.netloc}'. Try mock://cantilever")
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}

        def num(key, default, cast=float):
            try:
                return cast(q.get(key, default))
            except ValueError as exc:
                raise SourceError(f"Bad value for '{key}' in {path}") from exc

        self.P = num("load", 1000.0)
        self.L = num("length", 100.0)
        self.h = num("height", 10.0)
        self.b = num("width", 10.0)
        self.E = num("E", 210000.0)
        self.nu = 0.3
        self.nx = max(1, num("nx", 40, int))
        self.ny = max(1, num("ny", 4, int))
        self.nz = max(1, num("nz", 4, int))
        self.nframes = max(2, num("frames", 11, int))
        self.ninst = max(1, num("instances", 1, int))
        self.names = [f"BEAM-{k + 1}" for k in range(self.ninst)]
        self.rp = bool(num("rp", 0, int))
        self.warnings = bool(num("warnings", 0, int))
        self.I = self.b * self.h**3 / 12.0
        self._build_mesh()

    # ------------------------------------------------------------------ mesh
    def _node_label(self, i, j, k):
        return 1 + i + (self.nx + 1) * (j + (self.ny + 1) * k)

    def _build_mesh(self):
        nx, ny, nz = self.nx, self.ny, self.nz
        xs = np.linspace(0, self.L, nx + 1)
        ys = np.linspace(-self.h / 2, self.h / 2, ny + 1)
        zs = np.linspace(-self.b / 2, self.b / 2, nz + 1)
        K, J, I = np.meshgrid(np.arange(nz + 1), np.arange(ny + 1), np.arange(nx + 1), indexing="ij")
        I, J, K = I.ravel(), J.ravel(), K.ravel()
        self.node_labels = self._node_label(I, J, K)
        order = np.argsort(self.node_labels)
        self.node_labels = self.node_labels[order]
        self.coords = np.stack([xs[I], ys[J], zs[K]], axis=1)[order]

        conn, centroids = [], []
        for k in range(nz):
            for j in range(ny):
                for i in range(nx):
                    n = self._node_label
                    conn.append([n(i, j, k), n(i + 1, j, k), n(i + 1, j + 1, k), n(i, j + 1, k),
                                 n(i, j, k + 1), n(i + 1, j, k + 1), n(i + 1, j + 1, k + 1), n(i, j + 1, k + 1)])
                    centroids.append([(xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2, (zs[k] + zs[k + 1]) / 2])
        self.connectivity = conn
        self.element_labels = np.arange(1, len(conn) + 1)
        self.centroids = np.array(centroids)

        x, y = self.coords[:, 0], self.coords[:, 1]
        self.fixed_nodes = self.node_labels[np.isclose(x, 0)]
        self.tip_nodes = self.node_labels[np.isclose(x, self.L)]
        self.top_nodes = self.node_labels[np.isclose(y, self.h / 2)]
        root_len = self.L / nx * max(1, nx // 8)
        self.root_elements = self.element_labels[self.centroids[:, 0] < root_len]
        mid = np.argmin(np.abs(x - self.L) + np.abs(y) + np.abs(self.coords[:, 2]))
        self.tip_node = int(self.node_labels[mid])
        root = np.argmin(np.abs(x) + np.abs(y - self.h / 2) + np.abs(self.coords[:, 2]))
        self.root_node = int(self.node_labels[root])

    # ------------------------------------------------------------ analytics
    def _times(self):
        return np.linspace(0.0, 1.0, self.nframes)

    def _deflection(self, x, t):
        P = self.P * t
        return -P * x**2 * (3 * self.L - x) / (6 * self.E * self.I)

    def _slope(self, x, t):
        P = self.P * t
        return -P * x * (2 * self.L - x) / (2 * self.E * self.I)

    def _stress(self, xyz, t):
        x, y = xyz[:, 0], xyz[:, 1]
        P = self.P * t
        s11 = P * (self.L - x) * y / self.I
        s12 = -P * (self.h**2 / 4 - y**2) / (2 * self.I)
        out = np.zeros((len(x), 6))
        out[:, 0] = s11
        out[:, 3] = s12
        return out

    # ------------------------------------------------------------- interface
    def describe(self):
        times = self._times()
        return {
            "path": self.path,
            "steps": [{
                "name": STEP,
                "description": f"Tip load {self.P:g} N ramped linearly",
                "frames": [{"index": i, "value": float(t), "description": f"Increment {i}: Step Time = {t:.3g}"}
                           for i, t in enumerate(times)],
                "fields": [
                    # "invariants" as Abaqus reports them (validInvariants): strains have no Mises etc.
                    {"name": "U", "description": "Spatial displacement", "type": "VECTOR",
                     "components": ["U1", "U2", "U3"], "positions": ["NODAL"], "invariants": ["Magnitude"]},
                    {"name": "RF", "description": "Reaction force", "type": "VECTOR",
                     "components": ["RF1", "RF2", "RF3"], "positions": ["NODAL"], "invariants": ["Magnitude"]},
                    {"name": "S", "description": "Stress components", "type": "TENSOR_3D_FULL",
                     "components": ["S11", "S22", "S33", "S12", "S13", "S23"], "positions": ["ELEMENT"],
                     "invariants": ["Mises", "Max. Principal", "Mid. Principal", "Min. Principal", "Tresca",
                                    "Pressure"]},
                    {"name": "E", "description": "Strain components", "type": "TENSOR_3D_FULL",
                     "components": ["E11", "E22", "E33", "E12", "E13", "E23"], "positions": ["ELEMENT"],
                     "invariants": ["Max. Principal", "Mid. Principal", "Min. Principal"]},
                    {"name": "PEEQ", "description": "Equivalent plastic strain", "type": "SCALAR",
                     "components": ["PEEQ"], "positions": ["ELEMENT"], "invariants": []},
                ],
                "historyRegions": [
                    {"name": "Assembly ASSEMBLY", "outputs": [
                        {"name": "ALLSE", "description": "Strain energy"},
                        {"name": "ALLWK", "description": "External work"}]},
                    {"name": f"Node {INSTANCE}.{self.tip_node}", "outputs": [
                        {"name": "U2", "description": "Tip deflection"}]},
                    {"name": "Node set FIXED", "outputs": [
                        {"name": "RF2", "description": "Total reaction force"}]},
                ],
            }],
            "instances": [{"name": name, "nodes": int(len(self.node_labels)),
                           "elements": int(len(self.element_labels))} for name in self.names],
            "sets": [
                {"name": "FIXED", "kind": "node"},
                {"name": "TIP", "kind": "node"},
                {"name": "TOP_SURFACE", "kind": "node"},
                {"name": "ROOT_ELEMENTS", "kind": "element"},
            ] + [{"name": f"{name}.{suffix}", "kind": kind} for name in self.names
                 for suffix, kind in (("ALL_ELEMENTS", "element"), ("ALL_NODES", "node"))]
            + ([{"name": "RP", "kind": "node"}, {"name": "TIP_AND_RP", "kind": "node"}] if self.rp else [])
            + ([{"name": "WarnElemDistorted", "kind": "element"}, {"name": "WarnNodeOverconstr", "kind": "node"}]
               if self.warnings else []),
        }

    def _frame_time(self, step, frame):
        if step != STEP:
            raise SourceError(f"Step '{step}' not found (available: {STEP})")
        times = self._times()
        if not -len(times) <= frame < len(times):
            raise SourceError(f"Frame {frame} out of range (step has {len(times)} frames)")
        return float(times[frame])

    def field(self, step, frame, variable, position="Automatic", section_point=0,
              complex_part="Real", phase_angle=0.0):
        t = self._frame_time(step, frame)
        n = len(self.node_labels)
        if variable == "U":
            x, y = self.coords[:, 0], self.coords[:, 1]
            values = np.stack([-y * self._slope(x, t), self._deflection(x, t), np.zeros(n)], axis=1)
            comps, pos, ftype = ["U1", "U2", "U3"], "NODAL", "VECTOR"
        elif variable == "RF":
            values = np.zeros((n, 3))
            values[np.isin(self.node_labels, self.fixed_nodes), 1] = self.P * t / len(self.fixed_nodes)
            comps, pos, ftype = ["RF1", "RF2", "RF3"], "NODAL", "VECTOR"
        elif variable == "PEEQ":  # the beam stays elastic
            values = np.zeros((len(self.element_labels), 1))
            comps, pos, ftype = ["PEEQ"], "ELEMENT", "SCALAR"
        elif variable in ("S", "E"):
            s = self._stress(self.centroids, t)
            if variable == "S":
                values = s
            else:
                G = self.E / (2 * (1 + self.nu))
                values = np.zeros_like(s)
                values[:, 0] = s[:, 0] / self.E
                values[:, 1] = values[:, 2] = -self.nu * s[:, 0] / self.E
                values[:, 3] = s[:, 3] / G  # engineering shear strain, as Abaqus stores it
            comps = [f"{variable}{c}" for c in ("11", "22", "33", "12", "13", "23")]
            pos, ftype = "ELEMENT", "TENSOR_3D_FULL"
        else:
            raise SourceError(f"Field '{variable}' not in frame (available: U, RF, S, E, PEEQ)")
        labels = self.node_labels if pos == "NODAL" else self.element_labels
        # Results are linear in the load, and BEAM-k carries k times the load.
        instances = np.repeat(np.array(self.names, dtype=object), len(labels))
        labels_all = np.tile(labels, self.ninst)
        values_all = np.concatenate([values * (k + 1) for k in range(self.ninst)])
        if self.rp and pos == "NODAL":  # the reference point moves with BEAM-1's tip and carries no reaction
            rp_value = np.zeros((1, values.shape[1]))
            if variable == "U":
                rp_value[0, 1] = self._deflection(self.L, t)
            instances = np.append(instances, "")
            labels_all = np.append(labels_all, 1)
            values_all = np.vstack([values_all, rp_value])
        if complex_part == "Imaginary":
            values_all = np.zeros_like(values_all)
        elif complex_part == "Magnitude":
            values_all = np.abs(values_all)
        elif complex_part == "Phase":
            values_all = np.where(values_all < 0, 180.0, 0.0)
        elif complex_part == "At phase angle":
            values_all = values_all * np.cos(np.deg2rad(float(phase_angle)))
        return {"position": pos, "type": ftype, "components": comps, "instances": instances,
                "labels": labels_all, "values": values_all, "frameValue": t}

    def set(self, name):
        assembly = {
            "FIXED": ("node", self.fixed_nodes),
            "TIP": ("node", self.tip_nodes),
            "TOP_SURFACE": ("node", self.top_nodes),
            "ROOT_ELEMENTS": ("element", self.root_elements),
        }
        if self.warnings:
            assembly.update(WarnElemDistorted=("element", self.root_elements),
                            WarnNodeOverconstr=("node", self.fixed_nodes[:3]))
        if name in assembly:  # assembly-level sets span every instance
            kind, labels = assembly[name]
            return {"kind": kind, "instances": np.repeat(np.array(self.names, dtype=object), len(labels)),
                    "labels": np.tile(labels, self.ninst)}
        if self.rp and name in ("RP", "TIP_AND_RP"):
            tip = self.tip_nodes if name == "TIP_AND_RP" else np.zeros(0, dtype=int)
            return {"kind": "node", "instances": np.array([self.names[0]] * len(tip) + [""], dtype=object),
                    "labels": np.append(tip, 1)}
        inst, _, short = name.partition(".")
        local = {"ALL_ELEMENTS": ("element", self.element_labels), "ALL_NODES": ("node", self.node_labels)}
        if inst not in self.names or short not in local:
            raise SourceError(f"Set '{name}' not found")
        kind, labels = local[short]
        return {"kind": kind, "instances": np.full(len(labels), inst, dtype=object), "labels": labels.copy()}

    def history(self, step, region, output):
        self._frame_time(step, 0)
        t = self._times()
        tip_defl = self._deflection(self.L, t)
        series = {
            ("Assembly ASSEMBLY", "ALLSE"): 0.5 * self.P * t * -tip_defl,
            ("Assembly ASSEMBLY", "ALLWK"): 0.5 * self.P * t * -tip_defl,
            (f"Node {INSTANCE}.{self.tip_node}", "U2"): tip_defl,
            ("Node set FIXED", "RF2"): self.P * t,
        }
        if (region, output) not in series:
            raise SourceError(f"History output '{output}' not found in region '{region}'")
        return {"x": t, "y": series[(region, output)]}

    def mesh(self):
        return {"instances": [{
            "name": name,
            "nodeLabels": self.node_labels,
            "coords": self.coords + np.array([0.0, 0.0, 2.0 * self.b * k]),
            "elementLabels": self.element_labels,
            "elementTypes": ["C3D8R"] * len(self.element_labels),
            "connectivity": self.connectivity,
        } for k, name in enumerate(self.names)]}
