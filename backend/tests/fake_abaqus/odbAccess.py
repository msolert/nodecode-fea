"""Stand-in for Abaqus' odbAccess module, backed by the mock cantilever (tests only).

The .odb "file" contains a mock:// URL. The object model mimics the parts of the
real API the worker uses, including assembly-level sets that nest members per
instance, and element results that only exist at integration points (so the
worker's CENTROID fallback and averaging are exercised).
"""

import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from nodecode_fea.sources.mock import MockSource  # noqa: E402
from abaqusConstants import SymbolicConstant  # noqa: E402


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Node(Obj):
    pass


class Element(Obj):
    pass


class FieldOutput:
    def __init__(self, src, step, index, name, desc):
        self.src, self.step, self.index, self.name = src, step, index, name
        self.description = desc["description"]
        self.type = SymbolicConstant(desc["type"])
        scalar = desc["type"] == "SCALAR"
        self.componentLabels = () if scalar else tuple(desc["components"])  # Abaqus gives none for scalars
        names = {v: k for k, v in {"MISES": "Mises", "MAX_PRINCIPAL": "Max. Principal",
                                   "MID_PRINCIPAL": "Mid. Principal", "MIN_PRINCIPAL": "Min. Principal",
                                   "TRESCA": "Tresca", "PRESS": "Pressure", "MAGNITUDE": "Magnitude"}.items()}
        self.validInvariants = tuple(SymbolicConstant(names[i]) for i in desc.get("invariants", []))
        pos = "NODAL" if desc["positions"] == ["NODAL"] else "INTEGRATION_POINT"
        self.locations = [Obj(position=SymbolicConstant(pos))]

    def getSubset(self, position=None, region=None):
        """One bulk data block per instance, like a real ODB."""
        res = self.src.field(self.step, self.index, self.name)
        blocks = []
        for name in dict.fromkeys(res["instances"]):
            rows = res["instances"] == name
            labels, data = res["labels"][rows], res["values"][rows]
            # Assembly-level results: no instance object, as for reference points in a real ODB.
            inst = Obj(name=name) if name else None
            if res["position"] == "NODAL":
                blocks.append(Obj(instance=inst, nodeLabels=labels, elementLabels=None, data=data,
                                  conjugateData=data * 0.5, integrationPoints=(), sectionPoint=None))
                continue
            if str(position) == "CENTROID":
                blocks.append(Obj(instance=inst, nodeLabels=None, elementLabels=labels, data=data,
                                  conjugateData=data * 0.5, integrationPoints=(), sectionPoint=None))
                continue
            labels = np.repeat(labels, 2)  # two integration points per element
            data = np.repeat(data, 2, axis=0)
            data[::2] *= 0.9
            data[1::2] *= 1.1  # average is the original value
            if str(position) == "ELEMENT_NODAL":  # like a real ODB: the node of each value, no integration point
                nodes = labels * 10 + np.tile([1, 2], len(labels) // 2)
                blocks.append(Obj(instance=inst, nodeLabels=nodes, elementLabels=labels, data=data,
                                  conjugateData=data * 0.5, integrationPoints=(), sectionPoint=Obj(number=1)))
                continue
            blocks.append(Obj(instance=inst, nodeLabels=None, elementLabels=labels, data=data,
                              conjugateData=data * 0.5,
                              integrationPoints=np.tile([1, 2], len(labels) // 2), sectionPoint=Obj(number=1)))
        return Obj(bulkDataBlocks=blocks)


def _build(src):
    info = src.describe()
    instances, by_node, by_elem = {}, {}, {}
    for mesh in src.mesh()["instances"]:
        name = mesh["name"]
        # Like real ODBs, assume mesh objects don't say which instance they belong to;
        # assembly-level sets map their per-instance arrays to instanceNames by position.
        nodes = [Node(label=int(l), coordinates=tuple(c)) for l, c in zip(mesh["nodeLabels"], mesh["coords"])]
        elements = [Element(label=int(l), type=t, connectivity=tuple(c))
                    for l, t, c in zip(mesh["elementLabels"], mesh["elementTypes"], mesh["connectivity"])]
        by_node[name] = {n.label: n for n in nodes}
        by_elem[name] = {e.label: e for e in elements}
        instances[name] = Obj(name=name, nodes=nodes, elements=elements, nodeSets={}, elementSets={})

    asm_node_sets, asm_elem_sets = {}, {}
    for sdesc in info["sets"]:
        name, kind = sdesc["name"], sdesc["kind"]
        s = src.set(name)
        table = by_node if kind == "node" else by_elem
        per_inst = {}
        for inst, label in zip(s["instances"], s["labels"]):
            if not inst:  # an assembly-level node: listed under 'rootAssembly', as the user's ODB does
                per_inst.setdefault("rootAssembly", []).append(Node(label=int(label), coordinates=(0.0, 0.0, 0.0)))
                continue
            per_inst.setdefault(inst, []).append(table[inst][int(label)])
        inst_name, _, short = name.partition(".")
        if short and inst_name in instances:  # instance-level set: a flat array
            items = per_inst[inst_name]
            target = instances[inst_name].nodeSets if kind == "node" else instances[inst_name].elementSets
            target[short] = Obj(nodes=items if kind == "node" else None,
                                elements=items if kind == "element" else None, instanceNames=())
        else:  # assembly-level set: one array per instance, aligned with instanceNames
            names = tuple(per_inst)
            arrays = tuple(tuple(per_inst[n]) for n in names)
            target = asm_node_sets if kind == "node" else asm_elem_sets
            target[name] = Obj(nodes=arrays if kind == "node" else None,
                               elements=arrays if kind == "element" else None, instanceNames=names)

    steps = {}
    for s in info["steps"]:
        frames = []
        for f in s["frames"]:
            fos = {d["name"]: FieldOutput(src, s["name"], f["index"], d["name"], d) for d in s["fields"]}
            frames.append(Obj(frameValue=f["value"], description=f["description"], fieldOutputs=fos))
        regions = {}
        for r in s["historyRegions"]:
            outs = {}
            for o in r["outputs"]:
                h = src.history(s["name"], r["name"], o["name"])
                outs[o["name"]] = Obj(description=o["description"], data=tuple(zip(h["x"], h["y"])))
            regions[r["name"]] = Obj(historyOutputs=outs)
        steps[s["name"]] = Obj(description=s["description"], frames=frames, historyRegions=regions)

    assembly = Obj(instances=instances, nodeSets=asm_node_sets, elementSets=asm_elem_sets)
    return Obj(steps=steps, rootAssembly=assembly, close=lambda: None)


def openOdb(path, readOnly=True):
    with open(path) as fh:
        url = fh.read().strip()
    if not url.startswith("mock://"):  # like Abaqus on a file that isn't (yet) a database
        raise RuntimeError("OdbError: %s is not a valid output database" % path)
    return _build(MockSource(url))


def isUpgradeRequiredForOdb(upgradeRequiredOdbPath):
    """Tests mark an ODB as written by an older release by naming it old-*.odb."""
    return os.path.basename(upgradeRequiredOdbPath).startswith("old-")


def upgradeOdb(existingOdbPath, upgradedOdbPath):
    import abaqus
    abaqus.LOG.append({"call": "odbAccess.upgradeOdb", "args": [],
                       "kwargs": {"existingOdbPath": existingOdbPath, "upgradedOdbPath": upgradedOdbPath}})
    shutil.copyfile(existingOdbPath, upgradedOdbPath)
