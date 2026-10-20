"""ODB worker: runs inside Abaqus Python (2024+, Python 3) and serves ODB data.

Start it with ``abaqus python odb_worker.py``. It reads one JSON request per line
on stdin and writes one JSON response per line on stdout, prefixed with
``@@NOCODE@@`` so any banner text Abaqus prints is ignored by the client.

Request:  {"id": 1, "method": "field", "params": {...}}
Response: {"id": 1, "result": ...} or {"id": 1, "error": "message", "trace": "..."}

Numeric arrays are sent as {"__nd__": dtype, "shape": [...], "b64": ...}.
This file must only depend on the standard library, numpy and the Abaqus
modules, because it runs on the user's Abaqus interpreter.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import traceback

import numpy as np
from abaqusConstants import CENTROID, ELEMENT_NODAL, INTEGRATION_POINT, NODAL, WHOLE_ELEMENT
from odbAccess import isUpgradeRequiredForOdb, openOdb, upgradeOdb

PREFIX = "@@NOCODE@@"
ELEMENT_POSITIONS = ("INTEGRATION_POINT", "CENTROID", "WHOLE_ELEMENT", "ELEMENT_NODAL", "ELEMENT_FACE")

_odbs = {}
_tmpdirs = []


def nd(array, dtype):
    a = np.ascontiguousarray(np.asarray(array, dtype=dtype))
    return {"__nd__": a.dtype.str, "shape": list(a.shape), "b64": base64.b64encode(a.tobytes()).decode("ascii")}


def get_odb(path):
    path = os.path.abspath(path)
    if path in _odbs:
        return _odbs[path]
    if not os.path.exists(path):
        raise ValueError("ODB not found: %s" % path)
    target = path
    if isUpgradeRequiredForOdb(upgradeRequiredOdbPath=path):
        tmp = tempfile.mkdtemp(prefix="nocode_")
        _tmpdirs.append(tmp)
        target = os.path.join(tmp, os.path.basename(path))
        upgradeOdb(existingOdbPath=path, upgradedOdbPath=target)
    odb = openOdb(path=target, readOnly=True)
    _odbs[path] = odb
    return odb


def simple_position(name):
    return "NODAL" if name == "NODAL" else "ELEMENT" if name in ELEMENT_POSITIONS else name


# ------------------------------------------------------------------ methods
INVARIANT_NAMES = {"MISES": "Mises", "MAX_PRINCIPAL": "Max. Principal", "MID_PRINCIPAL": "Mid. Principal",
                   "MIN_PRINCIPAL": "Min. Principal", "TRESCA": "Tresca", "PRESS": "Pressure", "MAGNITUDE": "Magnitude"}


def _invariants(fo):
    """The invariants Abaqus defines for this output (e.g. no Mises for strains); None if it doesn't say."""
    try:
        valid = fo.validInvariants
    except AttributeError:
        return None
    return [INVARIANT_NAMES[str(v)] for v in (valid or ()) if str(v) in INVARIANT_NAMES]


def m_describe(path):
    odb = get_odb(path)
    steps = []
    for sname, step in odb.steps.items():
        frames = [{"index": i, "value": float(f.frameValue), "description": f.description}
                  for i, f in enumerate(step.frames)]
        fields = []
        if len(step.frames):
            for fname, fo in step.frames[-1].fieldOutputs.items():
                positions = sorted(set(simple_position(str(loc.position)) for loc in fo.locations))
                fields.append({"name": fname, "description": fo.description, "type": str(fo.type),
                               "components": list(fo.componentLabels) or [fname], "positions": positions,
                               "invariants": _invariants(fo)})
        regions = [{"name": rname, "outputs": [{"name": oname, "description": ho.description}
                                               for oname, ho in region.historyOutputs.items()]}
                   for rname, region in step.historyRegions.items()]
        steps.append({"name": sname, "description": step.description, "frames": frames,
                      "fields": fields, "historyRegions": regions,
                      "totalTime": float(getattr(step, "totalTime", 0.0) or 0.0)})

    asm = odb.rootAssembly
    instances = [{"name": n, "nodes": len(i.nodes), "elements": len(i.elements)} for n, i in asm.instances.items()]
    sets = [{"name": n, "kind": "node"} for n in asm.nodeSets.keys()]
    sets += [{"name": n, "kind": "element"} for n in asm.elementSets.keys()]
    for iname, inst in asm.instances.items():
        sets += [{"name": "%s.%s" % (iname, n), "kind": "node"} for n in inst.nodeSets.keys()]
        sets += [{"name": "%s.%s" % (iname, n), "kind": "element"} for n in inst.elementSets.keys()]
    return {"path": path, "steps": steps, "instances": instances, "sets": sets}


def _frame(odb, step, frame):
    if step not in odb.steps.keys():
        raise ValueError("Step '%s' not found (available: %s)" % (step, ", ".join(odb.steps.keys())))
    frames = odb.steps[step].frames
    if not -len(frames) <= frame < len(frames):
        raise ValueError("Frame %d out of range (step has %d frames)" % (frame, len(frames)))
    return frames[frame]


def _instance_key(odb):
    """Map any instance name to the name used in results: a real part instance keeps its name,
    and anything else ('rootAssembly', 'ASSEMBLY', None...) means the assembly level, sent as ''.
    """
    real = set(odb.rootAssembly.instances.keys())

    def key(name):
        name = getattr(name, "name", name)  # an OdbInstance / OdbAssembly object, or a name
        return name if name in real else ""
    return key


def _collect(sub, nodal, key, section_point=0, complex_part="Real", phase_angle=0.0):
    names, inst_idx, labels, data, integration_points, section_points = [], [], [], [], [], []
    for block in sub.bulkDataBlocks:
        sp = getattr(getattr(block, "sectionPoint", None), "number", 0) or 0
        if section_point and int(sp) != int(section_point):
            continue
        iname = key(block.instance)
        if iname not in names:
            names.append(iname)
        lab = np.asarray(block.nodeLabels if nodal else block.elementLabels, dtype=np.int64).ravel()
        if lab.size == 0:
            continue
        real = np.asarray(block.data, dtype=np.float64).reshape(lab.size, -1)
        raw_imag = getattr(block, "conjugateData", None)
        imag = np.zeros_like(real) if raw_imag is None else np.asarray(raw_imag, dtype=np.float64).reshape(real.shape)
        if complex_part == "Imaginary":
            vals = imag
        elif complex_part == "Magnitude":
            vals = np.hypot(real, imag)
        elif complex_part == "Phase":
            vals = np.degrees(np.arctan2(imag, real))
        elif complex_part == "At phase angle":
            angle = np.deg2rad(float(phase_angle))
            vals = real * np.cos(angle) - imag * np.sin(angle)
        else:
            vals = real
        labels.append(lab)
        data.append(vals)
        inst_idx.append(np.full(lab.size, names.index(iname), dtype=np.int32))
        raw_ips = getattr(block, "integrationPoints", None)
        ips = np.asarray(() if raw_ips is None else raw_ips, dtype=np.int32).ravel()
        if not nodal and ips.size != lab.size:
            # Element-nodal values have no integration point; the node they're at tells the rows of an element apart.
            raw_nodes = getattr(block, "nodeLabels", None)
            ips = np.asarray(() if raw_nodes is None else raw_nodes, dtype=np.int32).ravel()
        integration_points.append(ips if ips.size == lab.size else np.zeros(lab.size, dtype=np.int32))
        section_points.append(np.full(lab.size, int(sp), dtype=np.int32))
    if not labels:
        return (names, np.zeros(0, np.int32), np.zeros(0, np.int64), np.zeros((0, 1)),
                np.zeros(0, np.int32), np.zeros(0, np.int32))
    return (names, np.concatenate(inst_idx), np.concatenate(labels), np.concatenate(data),
            np.concatenate(integration_points), np.concatenate(section_points))


def _average_duplicates(inst_idx, labels, values):
    """Average integration-point / section-point values down to one value per element."""
    key = inst_idx.astype(np.int64) * (int(labels.max()) + 1 if labels.size else 1) + labels
    uniq, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    if uniq.size == key.size:
        return inst_idx, labels, values
    sums = np.zeros((uniq.size, values.shape[1]))
    np.add.at(sums, inverse, values)
    counts = np.bincount(inverse, minlength=uniq.size)[:, None]
    return inst_idx[first], labels[first], sums / counts


def m_field(path, step, frame, variable, position="Automatic", section_point=0,
            complex_part="Real", phase_angle=0.0):
    odb = get_odb(path)
    fr = _frame(odb, step, int(frame))
    if variable not in fr.fieldOutputs.keys():
        raise ValueError("Field '%s' not in frame (available: %s)" % (variable, ", ".join(fr.fieldOutputs.keys())))
    fo = fr.fieldOutputs[variable]
    raw = set(str(loc.position) for loc in fo.locations)
    key = _instance_key(odb)
    requested = str(position or "Automatic")
    if requested == "Nodal" or (requested == "Automatic" and "NODAL" in raw):
        names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=NODAL), nodal=True, key=key,
                                                   section_point=section_point, complex_part=complex_part,
                                                   phase_angle=phase_angle)
        position = "NODAL"
    else:
        position = "ELEMENT"
        if requested == "Integration point":
            names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=INTEGRATION_POINT), False, key,
                                                       section_point, complex_part, phase_angle)
        elif requested == "Element nodal":
            names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=ELEMENT_NODAL), False, key,
                                                       section_point, complex_part, phase_angle)
        elif requested == "Centroid":
            names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=CENTROID), False, key, section_point,
                                                       complex_part, phase_angle)
        elif "WHOLE_ELEMENT" in raw:
            names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=WHOLE_ELEMENT), False, key,
                                                       section_point, complex_part, phase_angle)
        else:
            names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=CENTROID), False, key, section_point,
                                                       complex_part, phase_angle)
            if lab.size == 0:
                names, idx, lab, vals, ips, sps = _collect(fo.getSubset(position=INTEGRATION_POINT), False, key,
                                                           section_point, complex_part, phase_angle)
            idx, lab, vals = _average_duplicates(idx, lab, vals)
            ips, sps = np.zeros(lab.size, np.int32), np.zeros(lab.size, np.int32)
    if lab.size == 0:
        detail = " at section point %d" % int(section_point) if section_point else ""
        raise ValueError("Field '%s' has no values at %s%s" % (variable, requested, detail))
    comps = list(fo.componentLabels) or [variable]
    return {"position": position, "type": str(fo.type), "components": comps,
            "instanceNames": names, "instanceIndex": nd(idx, np.int32),
            "labels": nd(lab, np.int64), "values": nd(vals, np.float64),
            "integrationPoints": nd(ips, np.int32), "sectionPoints": nd(sps, np.int32),
            "frameValue": float(fr.frameValue)}


def _members(s, kind, default_inst, key):
    """(instance name, label) for every member of a node or element set.

    Instance-level sets hold a flat array. Assembly-level sets hold one array per
    instance, in the same order as ``s.instanceNames``; the mesh objects themselves
    don't reliably say which instance they belong to, so the position is what counts.
    """
    items = s.nodes if kind == "node" else s.elements
    if items is None:
        return []
    names = list(s.instanceNames or ())
    out = []
    for i, group in enumerate(items):
        if hasattr(group, "label"):  # flat array: an instance-level set
            inst = default_inst or getattr(group, "instanceName", None) or (names[0] if len(names) == 1 else "")
            out.append((key(inst), group.label))
            continue
        inst = names[i] if i < len(names) else default_inst
        for item in group:
            out.append((key(inst or getattr(item, "instanceName", None)), item.label))
    return out


def m_set(path, name):
    asm = get_odb(path).rootAssembly
    found = None
    if name in asm.nodeSets.keys():
        found = ("node", asm.nodeSets[name], "")
    elif name in asm.elementSets.keys():
        found = ("element", asm.elementSets[name], "")
    elif "." in name:
        iname, sname = name.split(".", 1)
        if iname in asm.instances.keys():
            inst = asm.instances[iname]
            if sname in inst.nodeSets.keys():
                found = ("node", inst.nodeSets[sname], iname)
            elif sname in inst.elementSets.keys():
                found = ("element", inst.elementSets[sname], iname)
    if found is None:
        raise ValueError("Set '%s' not found" % name)
    kind, s, default_inst = found
    names, idx, labels = [], [], []
    for iname, label in _members(s, kind, default_inst, _instance_key(get_odb(path))):
        if iname not in names:
            names.append(iname)
        idx.append(names.index(iname))
        labels.append(label)
    return {"kind": kind, "instanceNames": names, "instanceIndex": nd(idx, np.int32), "labels": nd(labels, np.int64)}


def m_history(path, step, region, output):
    odb = get_odb(path)
    if step not in odb.steps.keys():
        raise ValueError("Step '%s' not found" % step)
    regions = odb.steps[step].historyRegions
    if region not in regions.keys():
        raise ValueError("History region '%s' not found" % region)
    outputs = regions[region].historyOutputs
    if output not in outputs.keys():
        raise ValueError("History output '%s' not found in region '%s'" % (output, region))
    data = np.asarray(outputs[output].data, dtype=np.float64).reshape(-1, 2)
    return {"x": nd(data[:, 0], np.float64), "y": nd(data[:, 1], np.float64)}


def m_mesh(path):
    out = []
    for iname, inst in get_odb(path).rootAssembly.instances.items():
        if not len(inst.elements):
            continue
        nodes = inst.nodes
        out.append({
            "name": iname,
            "nodeLabels": nd([n.label for n in nodes], np.int64),
            "coords": nd([n.coordinates for n in nodes], np.float64),
            "elementLabels": nd([e.label for e in inst.elements], np.int64),
            "elementTypes": [e.type for e in inst.elements],
            "connectivity": [list(e.connectivity) for e in inst.elements],
        })
    return {"instances": out}


def m_close(path):
    odb = _odbs.pop(os.path.abspath(path), None)
    if odb is not None:
        odb.close()
    return True


METHODS = {"describe": m_describe, "field": m_field, "set": m_set, "history": m_history,
           "mesh": m_mesh, "close": m_close, "ping": lambda: "pong"}


def send(obj):
    sys.stdout.write(PREFIX + json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    send({"id": 0, "result": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            if req.get("method") == "shutdown":
                send({"id": rid, "result": True})
                break
            send({"id": rid, "result": METHODS[req["method"]](**req.get("params", {}))})
        except Exception as exc:  # report every failure back to the client
            send({"id": rid, "error": str(exc), "trace": traceback.format_exc()})
    for odb in _odbs.values():
        try:
            odb.close()
        except Exception:
            pass
    for tmp in _tmpdirs:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
