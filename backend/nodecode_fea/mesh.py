"""Turn an ODB mesh into a renderable outer surface, and map field values onto it."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

# Corner-node faces for each element family (0-based, Abaqus node ordering).
HEX = [[0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1], [1, 5, 6, 2], [2, 6, 7, 3], [3, 7, 4, 0]]
TET = [[0, 1, 2, -1], [0, 3, 1, -1], [1, 3, 2, -1], [2, 3, 0, -1]]
WEDGE = [[0, 1, 2, -1], [3, 5, 4, -1], [0, 3, 4, 1], [1, 4, 5, 2], [2, 5, 3, 0]]
QUAD = [[0, 1, 2, 3]]
TRI = [[0, 1, 2, -1]]
SOLIDS = {"hex": HEX, "tet": TET, "wedge": WEDGE}
PLANAR = {"quad": QUAD, "tri": TRI}

_SOLID_RE = re.compile(r"^(?:C|DC|AC|DCC|COH|GK)3D(\d+)")
_PLANAR_RE = re.compile(r"^(?:S|STRI|M3D|CPS|CPE|CPEG|CAX|CGAX|DC2D|DCAX|R3D|R2D|SFM3D)")


def classify(etype: str, nnodes: int) -> str | None:
    m = _SOLID_RE.match(etype)
    if m:
        k = int(m.group(1))
        return "hex" if k in (8, 20, 27) else "tet" if k in (4, 10) else "wedge" if k in (6, 15) else None
    if etype.startswith("SC"):
        return "hex" if nnodes >= 8 else "wedge" if nnodes >= 6 else None
    if _PLANAR_RE.match(etype):
        return "quad" if nnodes in (4, 8, 9) else "tri" if nnodes in (3, 6, 7) else None
    return None


@dataclass
class Surface:
    coords: np.ndarray        # (N, 3) every node of every instance
    node_inst: np.ndarray     # (N,) instance name per node
    node_label: np.ndarray    # (N,)
    elem_inst: np.ndarray     # (M,)
    elem_label: np.ndarray    # (M,)
    pair_elem: np.ndarray     # element index of each (element, node) incidence
    pair_node: np.ndarray     # node index of each incidence
    vertices: np.ndarray      # (V,) global node index of each surface vertex
    triangles: np.ndarray     # (T, 3) indices into vertices
    lines: np.ndarray         # (E, 2) indices into vertices (element edges on the surface)
    solids: list              # [(family, corner node indices (m, k))] for hex and tet groups

    def lookup(self, kind: str, instances: np.ndarray, labels: np.ndarray) -> np.ndarray:
        """Global index for each (instance, label); -1 where not found."""
        inst_all, lab_all = (self.node_inst, self.node_label) if kind == "node" else (self.elem_inst, self.elem_label)
        out = np.full(len(labels), -1, dtype=np.int64)
        for name in np.unique(instances):
            rows = np.nonzero(instances == name)[0]
            cand = np.nonzero(inst_all == name)[0]
            if cand.size == 0:
                continue
            order = np.argsort(lab_all[cand])
            sorted_labels = lab_all[cand][order]
            pos = np.clip(np.searchsorted(sorted_labels, labels[rows]), 0, len(sorted_labels) - 1)
            hit = sorted_labels[pos] == labels[rows]
            out[rows[hit]] = cand[order[pos[hit]]]
        return out

    def element_center(self, elem_index: int, coords: np.ndarray) -> np.ndarray:
        nodes = self.pair_node[(self.pair_elem == elem_index) & (self.pair_node >= 0)]
        return coords[nodes].mean(axis=0)

    def element_centers(self, elem_indices: np.ndarray, coords: np.ndarray) -> np.ndarray:
        """Centres of many elements at once (mean of their nodes)."""
        ok = self.pair_node >= 0
        n_el = len(self.elem_label)
        counts = np.bincount(self.pair_elem[ok], minlength=n_el)
        sums = np.stack([np.bincount(self.pair_elem[ok], weights=coords[self.pair_node[ok], k], minlength=n_el)
                         for k in range(3)], axis=1)
        return sums[elem_indices] / np.maximum(counts[elem_indices], 1)[:, None]

    def interpolate(self, points: np.ndarray, nodal: np.ndarray) -> np.ndarray:
        """Values at arbitrary points, using the shape functions of the solid element containing each point.

        Linear hex and tet shape functions are used (quadratic elements use their corner nodes).
        Points outside every hex/tet element get NaN.
        """
        out = np.full(len(points), np.nan)
        size = float(np.ptp(self.coords, axis=0).max()) if len(self.coords) else 1.0
        tol = 1e-6 * (size or 1.0)
        for family, corners in self.solids:
            xyz = self.coords[corners]
            lo, hi = xyz.min(axis=1) - tol, xyz.max(axis=1) + tol
            for i in np.nonzero(np.isnan(out))[0]:
                p = points[i]
                for c in np.nonzero(np.all((p >= lo) & (p <= hi), axis=1))[0]:
                    w = _hex_weights(xyz[c], p) if family == "hex" else _tet_weights(xyz[c], p)
                    if w is not None:
                        out[i] = float(w @ nodal[corners[c]])
                        break
        return out

    def nearest(self, points: np.ndarray, nodal: np.ndarray) -> np.ndarray:
        """Value of the closest node that has a value."""
        ok = np.nonzero(~np.isnan(nodal))[0]
        if ok.size == 0:
            return np.full(len(points), np.nan)
        out = np.empty(len(points))
        for i, p in enumerate(points):
            out[i] = nodal[ok[np.argmin(((self.coords[ok] - p) ** 2).sum(axis=1))]]
        return out

    def nodal(self, position: str, instances: np.ndarray, labels: np.ndarray, values: np.ndarray) -> np.ndarray:
        """Per-node values (NaN where missing); element values are averaged onto their nodes."""
        n = len(self.coords)
        values = np.asarray(values, dtype=float)
        if position == "NODAL":
            idx = self.lookup("node", instances, labels)
            out = np.full((n,) + values.shape[1:], np.nan)
            ok = idx >= 0
            out[idx[ok]] = values[ok]
            return out
        idx = self.lookup("element", instances, labels)
        per_elem = np.full(len(self.elem_label), np.nan)
        ok = idx >= 0
        # Integration-/section-point fields contain repeated element labels. Average them for rendering and
        # spatial interpolation while preserving the individual rows in the Field itself.
        if ok.any():
            sums = np.bincount(idx[ok], weights=values[ok], minlength=len(per_elem))
            counts = np.bincount(idx[ok], minlength=len(per_elem))
            per_elem[counts > 0] = sums[counts > 0] / counts[counts > 0]
        v = per_elem[self.pair_elem]
        have = ~np.isnan(v) & (self.pair_node >= 0)
        sums = np.bincount(self.pair_node[have], weights=v[have], minlength=n)
        counts = np.bincount(self.pair_node[have], minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)


# Natural coordinates of the C3D8 corner nodes, in Abaqus node order.
_HEX_NAT = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                     [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)


def _hex_weights(xyz: np.ndarray, p: np.ndarray) -> np.ndarray | None:
    """Trilinear shape-function weights of point p in a hexahedron, or None if p is outside."""
    xi = np.zeros(3)
    for _ in range(20):
        t = 1 + _HEX_NAT * xi  # (8, 3)
        n = t.prod(axis=1) / 8
        dn = np.stack([_HEX_NAT[:, k] * np.prod(np.delete(t, k, axis=1), axis=1) / 8 for k in range(3)], axis=1)
        r = n @ xyz - p
        try:
            step = np.linalg.solve((dn.T @ xyz).T, r)
        except np.linalg.LinAlgError:
            return None
        xi -= step
        if np.abs(step).max() < 1e-10:
            break
    if np.abs(xi).max() > 1 + 1e-6:
        return None
    return (1 + _HEX_NAT * xi).prod(axis=1) / 8


def _tet_weights(xyz: np.ndarray, p: np.ndarray) -> np.ndarray | None:
    try:
        lam = np.linalg.solve((xyz[1:] - xyz[0]).T, p - xyz[0])
    except np.linalg.LinAlgError:
        return None
    w = np.concatenate([[1 - lam.sum()], lam])
    return w if w.min() >= -1e-6 else None


def build_surface(mesh: dict) -> Surface:
    coords, node_inst, node_label = [], [], []
    elem_inst, elem_label, pair_elem, pair_node = [], [], [], []
    solid_faces, planar_faces, solids = [], [], []
    node_offset = elem_offset = 0

    for inst in mesh["instances"]:
        name = inst["name"]
        labels = np.asarray(inst["nodeLabels"], dtype=np.int64)
        xyz = np.asarray(inst["coords"], dtype=float).reshape(len(labels), -1)
        if xyz.shape[1] < 3:
            xyz = np.hstack([xyz, np.zeros((len(xyz), 3 - xyz.shape[1]))])
        coords.append(xyz[:, :3])
        node_inst.append(np.full(len(labels), name, dtype=object))
        node_label.append(labels)
        order = np.argsort(labels)
        sorted_labels = labels[order]

        def to_index(conn_labels):
            conn_labels = np.asarray(conn_labels, dtype=np.int64)
            pos = np.clip(np.searchsorted(sorted_labels, conn_labels), 0, len(sorted_labels) - 1)
            idx = order[pos] + node_offset
            return np.where(sorted_labels[pos] == conn_labels, idx, -1)

        etypes = inst["elementTypes"]
        conns = inst["connectivity"]
        elabels = np.asarray(inst["elementLabels"], dtype=np.int64)
        elem_inst.append(np.full(len(elabels), name, dtype=object))
        elem_label.append(elabels)

        groups: dict[tuple[str, int], list[int]] = {}
        for e, (etype, conn) in enumerate(zip(etypes, conns)):
            groups.setdefault((etype, len(conn)), []).append(e)
        for (etype, nn), members in groups.items():
            members = np.array(members)
            conn = to_index(np.array([conns[e] for e in members]).reshape(len(members), nn))
            pair_elem.append(np.repeat(members + elem_offset, nn))
            pair_node.append(conn.ravel())
            family = classify(etype, nn)
            if family is None:
                continue
            if family in ("hex", "tet"):
                k = 8 if family == "hex" else 4
                corners = conn[:, :k]
                solids.append((family, corners[(corners >= 0).all(axis=1)]))
            table = np.array(SOLIDS.get(family) or PLANAR[family])
            faces = np.where(table[None, :, :] >= 0, conn[:, np.maximum(table, 0)], -1).reshape(-1, 4)
            faces = faces[(faces[:, :3] >= 0).all(axis=1)]
            (solid_faces if family in SOLIDS else planar_faces).append(faces)

        node_offset += len(labels)
        elem_offset += len(elabels)

    faces = np.zeros((0, 4), dtype=np.int64)
    if solid_faces:
        allf = np.concatenate(solid_faces)
        keys = np.sort(allf, axis=1)
        _, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
        faces = allf[counts[inverse.ravel()] == 1]
    if planar_faces:
        faces = np.concatenate([faces] + planar_faces)

    quads = faces[faces[:, 3] >= 0]
    tris = faces[faces[:, 3] < 0][:, :3]
    triangles = np.concatenate([tris, quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]])
    edges = np.concatenate([
        quads[:, [0, 1]], quads[:, [1, 2]], quads[:, [2, 3]], quads[:, [3, 0]],
        tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]],
    ])
    edges = np.unique(np.sort(edges, axis=1), axis=0) if len(edges) else edges.reshape(0, 2)

    used = np.unique(np.concatenate([triangles.ravel(), edges.ravel()]))
    remap = np.full(node_offset, -1, dtype=np.int64)
    remap[used] = np.arange(len(used))

    def cat(parts, dtype):
        return np.concatenate(parts) if parts else np.zeros(0, dtype=dtype)

    return Surface(
        coords=cat(coords, float).reshape(-1, 3),
        node_inst=cat(node_inst, object), node_label=cat(node_label, np.int64),
        elem_inst=cat(elem_inst, object), elem_label=cat(elem_label, np.int64),
        pair_elem=cat(pair_elem, np.int64), pair_node=cat(pair_node, np.int64),
        vertices=used, triangles=remap[triangles], lines=remap[edges], solids=solids,
    )


_surfaces: "OrderedDict[tuple[str, float], Surface]" = OrderedDict()


def surface_for(odb) -> Surface:
    key = (odb.path, odb.version)
    if key not in _surfaces:
        _surfaces[key] = build_surface(odb.source.mesh())
        while len(_surfaces) > 8:
            _surfaces.popitem(last=False)
    _surfaces.move_to_end(key)
    return _surfaces[key]
