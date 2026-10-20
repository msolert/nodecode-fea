"""Stand-in for Abaqus/CAE's kernel 'abaqus' module (tests only).

``session`` records every call made on it, and the objects it returns, into a JSON
log at $NOCODE_CAE_LOG when the script ends, so tests can check what a generated
CAE script asked CAE to do.
"""

import sys
import atexit
import json
import os

LOG = []


def highlight(object=None):
    LOG.append({"call": "highlight", "args": [], "kwargs": {"object": _show(object)}})


def unhighlight(object=None):
    LOG.append({"call": "unhighlight", "args": [], "kwargs": {"object": _show(object)}})


def _show(v):
    if isinstance(v, Recorder):
        return "<" + v._path + ">"
    if isinstance(v, (list, tuple)):
        return [_show(x) for x in v]
    if isinstance(v, dict):
        return {k: _show(x) for k, x in v.items()}
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


class _Seq(object):
    """A GeomSequence stand-in: a length, joinable with +; its entities keep their repository indices."""

    def __init__(self, path, n, indices=None):
        self._path, self.n = path, n
        self.indices = list(range(n)) if indices is None else list(indices)

    def __len__(self):
        return self.n

    def __add__(self, other):
        return _Seq(self._path + "+" + getattr(other, "_path", str(other)), self.n + len(other),
                    self.indices + list(getattr(other, "indices", range(len(other)))))

    def __iter__(self):
        return iter([self[i] for i in range(self.n)])

    def __getitem__(self, i):
        child = Recorder(self._path + "[%s]" % i)
        if isinstance(i, int) and i < len(self.indices):
            object.__setattr__(child, "index", self.indices[i])
        return child

    def __str__(self):
        return "<%s>" % self._path


def _native_geometry(kwargs):
    """Real CAE (2025) refuses plain tuples of geometry for a Set or Surface: 'Feature creation failed.'"""
    for key, value in kwargs.items():
        if key in ("vertices", "edges", "faces", "cells") or key.startswith(("side1", "side2")):
            if isinstance(value, (tuple, list)):
                raise Exception("Feature creation failed.")


def _edge_tuple(kwargs):
    """Real CAE (2025) refuses a GeomSequence here: 'edgeList; found GeomSequence, expecting tuple'."""
    if not isinstance(kwargs.get("edgeList"), (tuple, list)):
        raise TypeError("edgeList; found GeomSequence, expecting tuple")


class AbaqusException(Exception):
    """What CAE raises when it refuses an operation (abaqusExceptions.AbaqusException)."""


class Recorder:
    def __init__(self, path):
        self._path = path

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        child = Recorder(self._path + "." + name)
        object.__setattr__(self, name, child)
        return child

    def __call__(self, *args, **kwargs):
        LOG.append({"call": self._path, "args": _show(list(args)), "kwargs": _show(kwargs)})
        return Recorder(self._path + "()")

    # Repositories read by generated scripts: empty, and indexing gives a recorded child; a slice is a native
    # sequence of that many entities (repo[0:0] empty, repo[i:i + 1] one), which can be added up.
    def __getitem__(self, key):
        if isinstance(key, slice):
            n = max(0, (key.stop or 0) - (key.start or 0)) if isinstance(key.stop, int) else 0
            return _Seq(self._path + "[%s:%s]" % (key.start, key.stop), n,
                        list(range(key.start or 0, (key.start or 0) + n)))
        child = Recorder(self._path + "[" + str(key) + "]")
        if isinstance(key, int):  # a geometry entity knows its index
            object.__setattr__(child, "index", key)
        return child

    def __contains__(self, key):
        return False

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __add__(self, other):
        return Recorder(self._path + "+" + _show(other).__str__())

    def __radd__(self, other):
        return Recorder(_show(other).__str__() + "+" + self._path)

    def getEdges(self):
        LOG.append({"call": self._path + ".getEdges", "args": [], "kwargs": {}})
        return (0,)


class Repo(dict):
    """A CAE repository (mdb.models, model.parts…): a dict whose deletions are logged as 'del <path>'."""

    def __init__(self, path=""):
        dict.__init__(self)
        self._path = path

    def __delitem__(self, key):
        LOG.append({"call": "del " + self._path, "args": [key], "kwargs": {}})
        dict.__delitem__(self, key)


class FeatureRepo(Repo):
    def changeKey(self, fromName, toName):
        LOG.append({"call": self._path + ".changeKey", "args": [],
                    "kwargs": {"fromName": _show(fromName), "toName": toName}})
        value = self.pop(fromName)
        object.__setattr__(value, "name", toName)
        self[toName] = value


def _mesh_quality(self, criterion, **kwargs):
    LOG.append({"call": self._path + ".verifyMeshQuality", "args": [], "kwargs": _show(dict(criterion=criterion, **kwargs))})
    if str(criterion) == "ASPECT_RATIO":
        return {"average": 1.4, "worst": 2.1, "failedElements": (), "naElements": (), "numElements": 68}
    return {"failedElements": (), "warningElements": (), "naElements": (), "numElements": 68}


class Part(Recorder):
    verifyMeshQuality = _mesh_quality

    def __init__(self, path, name):
        Recorder.__init__(self, path)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "features", FeatureRepo(path + ".features"))
        object.__setattr__(self, "sets", Repo(path + ".sets"))
        object.__setattr__(self, "surfaces", Repo(path + ".surfaces"))
        object.__setattr__(self, "referencePoints", {})

    def _region(self, repository, name, kwargs):
        value = Recorder(repository._path + "[" + name + "]")
        for kind in ("vertices", "edges", "faces", "cells", "referencePoints"):
            object.__setattr__(value, kind, kwargs.get(kind, kwargs.get("side1" + kind.title(),
                               kwargs.get("side2" + kind.title(), ()))))
        repository[name] = value
        return value

    def Set(self, name, **kwargs):
        LOG.append({"call": self._path + ".Set", "args": [], "kwargs": _show(dict({"name": name}, **kwargs))})
        _native_geometry(kwargs)
        return self._region(self.sets, name, kwargs)

    def Surface(self, name, **kwargs):
        LOG.append({"call": self._path + ".Surface", "args": [], "kwargs": _show(dict({"name": name}, **kwargs))})
        _native_geometry(kwargs)
        return self._region(self.surfaces, name, kwargs)

    def _feature(self, method, label=None, **kwargs):
        LOG.append({"call": self._path + "." + method, "args": [], "kwargs": _show(kwargs)})
        name = (label or method) + "-" + str(len(self.features) + 1)
        value = Recorder(self._path + ".features[" + name + "]")
        object.__setattr__(value, "name", name)
        object.__setattr__(value, "id", len(self.features) + 1)
        self.features[name] = value
        return value

    def DatumPlaneByPrincipalPlane(self, **kwargs):
        return self._feature("DatumPlane", **kwargs)

    def DatumPlaneByThreePoints(self, **kwargs):
        return self._feature("DatumPlane", **kwargs)

    def DatumPlaneByOffset(self, **kwargs):
        return self._feature("DatumPlane", **kwargs)

    def DatumAxisByPrincipalAxis(self, **kwargs):
        return self._feature("DatumAxis", **kwargs)

    def DatumAxisByTwoPoint(self, **kwargs):
        return self._feature("DatumAxis", **kwargs)

    def DatumPointByCoordinate(self, **kwargs):
        return self._feature("DatumPoint", **kwargs)

    def ReferencePoint(self, **kwargs):
        feature = self._feature("ReferencePoint", "RP", **kwargs)
        self.referencePoints[feature.id] = Recorder(self._path + ".referencePoints[%d]" % feature.id)
        return feature

    def DatumCsysByThreePoints(self, **kwargs):
        feature = self._feature("DatumCsysByThreePoints", "Datum csys", **kwargs)
        self.features.changeKey(fromName=feature.name, toName=kwargs["name"])  # CAE names it as asked
        return feature

    def PartitionCellByDatumPlane(self, **kwargs):
        return self._feature("PartitionCell", **kwargs)

    def PartitionFaceByDatumPlane(self, **kwargs):
        return self._feature("PartitionFace", **kwargs)

    def Round(self, **kwargs):
        _edge_tuple(kwargs)
        return self._feature("Round", **kwargs)

    def Chamfer(self, **kwargs):
        _edge_tuple(kwargs)
        return self._feature("Chamfer", **kwargs)

    def MakeSketchTransform(self, **kwargs):
        LOG.append({"call": self._path + ".MakeSketchTransform", "args": [], "kwargs": _show(kwargs)})
        return Recorder(self._path + ".MakeSketchTransform()")

    def SolidExtrude(self, **kwargs):
        return self._feature("SolidExtrude", **kwargs)

    def CutExtrude(self, **kwargs):
        return self._feature("CutExtrude", **kwargs)

    def PartitionCellByExtendFace(self, **kwargs):
        return self._feature("PartitionCellByExtendFace", "Partition cell", **kwargs)

    def PartitionFaceBySketch(self, **kwargs):
        return self._feature("PartitionFaceBySketch", "Partition face", **kwargs)

    def PartitionEdgeByParam(self, **kwargs):
        return self._feature("PartitionEdgeByParam", "Partition edge", **kwargs)

    def RemoveCells(self, **kwargs):
        return self._feature("RemoveCells", "Remove cells", **kwargs)

    def RemoveFaces(self, **kwargs):
        return self._feature("RemoveFaces", "Remove faces", **kwargs)

    def AssignMidsurfaceRegion(self, **kwargs):
        return self._feature("AssignMidsurfaceRegion", "Assign midsurface", **kwargs)

    def OffsetFaces(self, **kwargs):
        if "targetFaces" in kwargs and "targetFacesMethod" not in kwargs:  # what real CAE says
            raise AbaqusException("'targetFacesMethod' must be specified when using 'targetFaces' for offset.")
        return self._feature("OffsetFaces", "Offset faces", **kwargs)

    # Geometry edits (defeaturing): like CAE they add a feature; the volume grows back as holes are filled.
    def RemoveFacesAndStitch(self, **kwargs):
        object.__setattr__(self, "_filled", self.__dict__.get("_filled", 0) + 1)
        return self._feature("RemoveFacesAndStitch", "Remove faces", **kwargs)

    def RemoveWireEdges(self, **kwargs):
        return self._feature("RemoveWireEdges", "Remove wire", **kwargs)

    def RepairSmallEdges(self, **kwargs):
        return self._feature("RepairSmallEdges", "Repair small edges", **kwargs)

    def RepairSmallFaces(self, **kwargs):
        return self._feature("RepairSmallFaces", "Repair small faces", **kwargs)

    def RemoveRedundantEntities(self, **kwargs):
        # like real CAE on a part with nothing to remove (the fakes' parts are boxes): it refuses, adding no feature
        LOG.append({"call": self._path + ".RemoveRedundantEntities", "args": [], "kwargs": _show(kwargs)})
        raise AbaqusException("None of the selected vertices\nor edges are redundant.")

    def getVolume(self, **kwargs):
        return 1000.0 + 10.0 * self.__dict__.get("_filled", 0)


class Assembly(Recorder):
    verifyMeshQuality = _mesh_quality

    def __init__(self, path, parts=None):
        Recorder.__init__(self, path)
        object.__setattr__(self, "_parts", parts)
        object.__setattr__(self, "instances", Repo(path + ".instances"))
        object.__setattr__(self, "features", FeatureRepo(path + ".features"))
        object.__setattr__(self, "sets", Repo(path + ".sets"))
        object.__setattr__(self, "surfaces", Repo(path + ".surfaces"))
        object.__setattr__(self, "referencePoints", {})
        object.__setattr__(self, "sectionAssignments", Repo(path + ".sectionAssignments"))
        object.__setattr__(self, "_feature_edges", {})

    def _feature(self, method, **kwargs):
        LOG.append({"call": self._path + "." + method, "args": [], "kwargs": _show(kwargs)})
        name = method + "-" + str(len(self.features) + 1)
        value = Recorder(self._path + ".features[" + name + "]")
        object.__setattr__(value, "name", name)
        object.__setattr__(value, "id", 3000 + len(self.features))
        self.features[name] = value
        return value

    def DatumPlaneByPrincipalPlane(self, **kwargs):
        return self._feature("DatumPlane", **kwargs)

    def DatumPlaneByThreePoints(self, **kwargs):
        return self._feature("DatumPlane", **kwargs)

    def DatumPlaneByOffset(self, **kwargs):
        return self._feature("DatumPlane", **kwargs)

    def DatumAxisByPrincipalAxis(self, **kwargs):
        return self._feature("DatumAxis", **kwargs)

    def DatumAxisByTwoPoint(self, **kwargs):
        return self._feature("DatumAxis", **kwargs)

    def DatumPointByCoordinate(self, **kwargs):
        return self._feature("DatumPoint", **kwargs)

    def WirePolyLine(self, points, **kwargs):
        LOG.append({"call": self._path + ".WirePolyLine", "args": [],
                    "kwargs": dict(_show(kwargs), points=_show(points))})
        name = "Wire-%d" % (len(self.features) + 1)
        feature = Recorder(self._path + ".features[" + name + "]")
        object.__setattr__(feature, "name", name)
        self.features[name] = feature
        return feature

    def DatumCsysByDefault(self, coordSysType):
        LOG.append({"call": self._path + ".DatumCsysByDefault", "args": [_show(coordSysType)], "kwargs": {}})
        name = "Datum csys-%d" % (len(self.features) + 1)
        feature = Recorder(self._path + ".features[" + name + "]")
        object.__setattr__(feature, "name", name)
        object.__setattr__(feature, "id", 2000 + len(self.features))
        self.features[name] = feature
        return feature

    def getFeatureEdges(self, name):
        if name not in self.features:
            raise KeyError(name)
        return Recorder(self._path + ".getFeatureEdges(" + name + ")")

    def SectionAssignment(self, region, sectionName, **kwargs):
        LOG.append({"call": self._path + ".SectionAssignment", "args": [],
                    "kwargs": {"region": _show(region), "sectionName": sectionName}})
        value = Recorder(self._path + ".sectionAssignments[%d]" % len(self.sectionAssignments))
        object.__setattr__(value, "region", (getattr(region, "_path", "").split("[")[-1].rstrip("]"), "Assembly"))
        self.sectionAssignments[len(self.sectionAssignments)] = value
        return value

    def ReferencePoint(self, point):
        LOG.append({"call": self._path + ".ReferencePoint", "args": [], "kwargs": {"point": _show(point)}})
        name = "RP-%d" % (len(self.referencePoints) + 1)
        feature = Recorder(self._path + ".features[" + name + "]")
        object.__setattr__(feature, "name", name)
        object.__setattr__(feature, "id", 1000 + len(self.referencePoints))
        self.referencePoints[feature.id] = Recorder(self._path + ".referencePoints[%d]" % feature.id)
        self.features[name] = feature
        return feature

    def deleteFeatures(self, featureNames):
        LOG.append({"call": self._path + ".deleteFeatures", "args": [list(featureNames)], "kwargs": {}})
        for name in featureNames:
            dict.__delitem__(self.features, name)

    def _region(self, repository, name, kwargs):
        value = Recorder(repository._path + "[" + name + "]")
        for kind in ("vertices", "edges", "faces", "cells", "referencePoints"):
            object.__setattr__(value, kind, kwargs.get(kind, kwargs.get("side1" + kind.title(),
                               kwargs.get("side2" + kind.title(), ()))))
        repository[name] = value
        return value

    def Set(self, name, **kwargs):
        LOG.append({"call": self._path + ".Set", "args": [], "kwargs": _show(dict({"name": name}, **kwargs))})
        _native_geometry(kwargs)
        return self._region(self.sets, name, kwargs)

    def Surface(self, name, **kwargs):
        LOG.append({"call": self._path + ".Surface", "args": [], "kwargs": _show(dict({"name": name}, **kwargs))})
        _native_geometry(kwargs)
        return self._region(self.surfaces, name, kwargs)

    def InstanceFromBooleanMerge(self, name, instances, **kwargs):
        LOG.append({"call": self._path + ".InstanceFromBooleanMerge", "args": [],
                    "kwargs": _show(dict({"name": name, "instances": instances}, **kwargs))})
        merged_name = name + "-1"
        for original in instances:
            original_name = getattr(original, "name", "")
            if original_name and original_name not in self.features:
                self.features[original_name] = Recorder(self._path + ".features[" + original_name + "]")
            if original_name and str(kwargs.get("originalInstances")) == "DELETE":
                self.instances.pop(original_name, None)
                self.features.pop(original_name, None)
        merged = Recorder(self._path + ".instances[" + merged_name + "]")
        object.__setattr__(merged, "name", merged_name)
        if self._parts is not None:
            part = Part(self._parts._path + "[" + name + "]", name)
            self._parts[name] = part
            object.__setattr__(merged, "partName", name)
            object.__setattr__(merged, "part", part)
        self.instances[merged_name] = merged
        self.features[merged_name] = Recorder(self._path + ".features[" + merged_name + "]")
        return merged

    def InstanceFromBooleanCut(self, name, instanceToBeCut, cuttingInstances, **kwargs):
        all_instances = (instanceToBeCut,) + tuple(cuttingInstances)
        LOG.append({"call": self._path + ".InstanceFromBooleanCut", "args": [],
                    "kwargs": _show(dict({"name": name, "instanceToBeCut": instanceToBeCut,
                                          "cuttingInstances": cuttingInstances}, **kwargs))})
        return self.InstanceFromBooleanMerge(name, all_instances, **kwargs)

    def Instance(self, name, part, dependent=None):
        LOG.append({"call": self._path + ".Instance", "args": [],
                    "kwargs": {"name": name, "part": _show(part), "dependent": _show(dependent)}})
        inst = Recorder(self._path + ".instances[" + name + "]")
        object.__setattr__(inst, "name", name)
        object.__setattr__(inst, "partName", part.name)
        object.__setattr__(inst, "part", part)
        # an instance's reference points are its part's, under the same ids
        object.__setattr__(inst, "referencePoints", dict(
            (k, Recorder(inst._path + ".referencePoints[%d]" % k)) for k in getattr(part, "referencePoints", {})))
        self.instances[name] = inst
        self.features[name] = Recorder(self._path + ".features[" + name + "]")
        return inst


def _interaction_repo(name):
    def get(self):
        if "interaction" not in sys.modules:
            raise AttributeError("'Model' object has no attribute '%s'" % name)
        return object.__getattribute__(self, "_" + name)
    return property(get)


class Model(Recorder):
    """A model with real repositories, so scripts can check what exists and delete it."""

    interactions = _interaction_repo("interactions")
    interactionProperties = _interaction_repo("interactionProperties")
    constraints = _interaction_repo("constraints")

    def __getattr__(self, name):
        if name in ("interactions", "interactionProperties", "constraints"):
            raise AttributeError("'Model' object has no attribute '%s'" % name)
        return Recorder.__getattr__(self, name)

    def __init__(self, name):
        Recorder.__init__(self, "model[" + name + "]")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "parts", Repo(self._path + ".parts"))
        object.__setattr__(self, "sketches", Repo(self._path + ".sketches"))
        object.__setattr__(self, "materials", Repo(self._path + ".materials"))
        object.__setattr__(self, "sections", Repo(self._path + ".sections"))
        object.__setattr__(self, "profiles", Repo(self._path + ".profiles"))
        object.__setattr__(self, "steps", Repo(self._path + ".steps"))
        object.__setattr__(self, "loads", Repo(self._path + ".loads"))
        object.__setattr__(self, "boundaryConditions", Repo(self._path + ".boundaryConditions"))
        object.__setattr__(self, "predefinedFields", Repo(self._path + ".predefinedFields"))
        # like CAE without its GUI: these appear once the interaction module is imported
        object.__setattr__(self, "_interactions", Repo(self._path + ".interactions"))
        object.__setattr__(self, "_interactionProperties", Repo(self._path + ".interactionProperties"))
        object.__setattr__(self, "_constraints", Repo(self._path + ".constraints"))
        object.__setattr__(self, "amplitudes", Repo(self._path + ".amplitudes"))
        object.__setattr__(self, "analyticalFields", Repo(self._path + ".analyticalFields"))
        self.steps["Initial"] = Recorder(self._path + ".steps[Initial]")
        object.__setattr__(self, "rootAssembly", Assembly(self._path + ".rootAssembly", self.parts))

    def ConstrainedSketch(self, name, sheetSize=None, **kwargs):
        LOG.append({"call": self._path + ".ConstrainedSketch", "args": [], "kwargs": {"name": name}})
        sketch = Recorder(self._path + ".sketches[" + name + "]")
        self.sketches[name] = sketch
        return sketch

    def Part(self, name, **kwargs):
        LOG.append({"call": self._path + ".Part", "args": [], "kwargs": dict(_show(kwargs), name=name)})
        part = Part(self._path + ".parts[" + name + "]", name)
        self.parts[name] = part
        return part

    def PartFromGeometryFile(self, name, geometryFile, **kwargs):
        LOG.append({"call": self._path + ".PartFromGeometryFile", "args": [],
                    "kwargs": _show(dict({"name": name, "geometryFile": geometryFile}, **kwargs))})
        part = Part(self._path + ".parts[" + name + "]", name)
        self.parts[name] = part
        return part

    def Material(self, name):
        LOG.append({"call": self._path + ".Material", "args": [], "kwargs": {"name": name}})
        value = Recorder(self._path + ".materials[" + name + "]")
        self.materials[name] = value
        return value

    def _named(self, method, repo, name, kwargs):
        LOG.append({"call": self._path + "." + method, "args": [], "kwargs": dict(_show(kwargs), name=name)})
        value = Recorder(self._path + "." + repo._path.split(".")[-1] + "[" + name + "]")
        repo[name] = value
        return value

    def HomogeneousSolidSection(self, name, **kwargs):
        return self._named("HomogeneousSolidSection", self.sections, name, kwargs)

    def HomogeneousShellSection(self, name, **kwargs):
        return self._named("HomogeneousShellSection", self.sections, name, kwargs)

    def BeamSection(self, name, **kwargs):
        return self._named("BeamSection", self.sections, name, kwargs)

    def RectangularProfile(self, name, **kwargs):
        return self._named("RectangularProfile", self.profiles, name, kwargs)

    def CircularProfile(self, name, **kwargs):
        return self._named("CircularProfile", self.profiles, name, kwargs)

    def StaticStep(self, name, **kwargs):
        return self._named("StaticStep", self.steps, name, kwargs)

    def ImplicitDynamicsStep(self, name, **kwargs):
        return self._named("ImplicitDynamicsStep", self.steps, name, kwargs)

    def ExplicitDynamicsStep(self, name, **kwargs):
        return self._named("ExplicitDynamicsStep", self.steps, name, kwargs)

    def FrequencyStep(self, name, **kwargs):
        return self._named("FrequencyStep", self.steps, name, kwargs)

    def EncastreBC(self, name, **kwargs):
        return self._named("EncastreBC", self.boundaryConditions, name, kwargs)

    def DisplacementBC(self, name, **kwargs):
        return self._named("DisplacementBC", self.boundaryConditions, name, kwargs)

    def ConcentratedForce(self, name, **kwargs):
        return self._named("ConcentratedForce", self.loads, name, kwargs)

    def Pressure(self, name, **kwargs):
        return self._named("Pressure", self.loads, name, kwargs)

    def Gravity(self, name, **kwargs):
        return self._named("Gravity", self.loads, name, kwargs)

    def BoltLoad(self, name, **kwargs):
        return self._named("BoltLoad", self.loads, name, kwargs)

    def Temperature(self, name, **kwargs):
        return self._named("Temperature", self.predefinedFields, name, kwargs)

    def Velocity(self, name, **kwargs):
        return self._named("Velocity", self.predefinedFields, name, kwargs)

    def TabularAmplitude(self, name, **kwargs):
        return self._named("TabularAmplitude", self.amplitudes, name, kwargs)

    def SmoothStepAmplitude(self, name, **kwargs):
        return self._named("SmoothStepAmplitude", self.amplitudes, name, kwargs)

    def EquallySpacedAmplitude(self, name, **kwargs):
        return self._named("EquallySpacedAmplitude", self.amplitudes, name, kwargs)

    def PeriodicAmplitude(self, name, **kwargs):
        return self._named("PeriodicAmplitude", self.amplitudes, name, kwargs)

    def ModulatedAmplitude(self, name, **kwargs):
        return self._named("ModulatedAmplitude", self.amplitudes, name, kwargs)

    def DecayAmplitude(self, name, **kwargs):
        return self._named("DecayAmplitude", self.amplitudes, name, kwargs)

    def SolutionDependentAmplitude(self, name, **kwargs):
        return self._named("SolutionDependentAmplitude", self.amplitudes, name, kwargs)

    def UserAmplitude(self, name, **kwargs):
        return self._named("UserAmplitude", self.amplitudes, name, kwargs)

    def ExpressionField(self, name, **kwargs):
        return self._named("ExpressionField", self.analyticalFields, name, kwargs)

    def MappedField(self, name, **kwargs):
        return self._named("MappedField", self.analyticalFields, name, kwargs)

    def ConnectorSection(self, name, **kwargs):
        return self._named("ConnectorSection", self.sections, name, kwargs)

    def ContactStd(self, name, createStepName, **kwargs):
        if createStepName != "Initial":  # as CAE 2025 says
            raise Exception("The specified type of interaction cannot be created or resumed in this type of step.")
        return self._named("ContactStd", self.interactions, name, dict(kwargs, createStepName=createStepName))

    def ContactExp(self, name, createStepName, **kwargs):
        return self._named("ContactExp", self.interactions, name, dict(kwargs, createStepName=createStepName))

    def SurfaceToSurfaceContactExp(self, name, *args, **kwargs):
        if set(kwargs) & {"thickness", "adjustMethod"}:  # Standard-only options, not in its CAE signature
            raise TypeError("keyword error on " + sorted(set(kwargs) & {"thickness", "adjustMethod"})[0])
        return self._named("SurfaceToSurfaceContactExp", self.interactions, name, kwargs)

    def Coupling(self, name, **kwargs):
        return self._named("Coupling", self.constraints, name, kwargs)

    def Tie(self, name, *args, **kwargs):
        return self._named("Tie", self.constraints, name, kwargs)

    def ContactProperty(self, name, **kwargs):
        return self._named("ContactProperty", self.interactionProperties, name, kwargs)

    def SurfaceToSurfaceContactStd(self, name, *args, **kwargs):
        return self._named("SurfaceToSurfaceContactStd", self.interactions, name, kwargs)

    def FilmCondition(self, name, **kwargs):
        return self._named("FilmCondition", self.interactions, name, kwargs)

    def RigidBody(self, name, **kwargs):
        return self._named("RigidBody", self.constraints, name, kwargs)

    def Equation(self, name, **kwargs):
        return self._named("Equation", self.constraints, name, kwargs)


def _repo_maker(method, repo):
    def make(self, name, **kwargs):
        return self._named(method, getattr(self, repo), name, kwargs)
    make.__name__ = method
    return make


for _method, _repo in (("BuckleStep", "steps"), ("StaticRiksStep", "steps"), ("HeatTransferStep", "steps"),
                       ("CoupledTempDisplacementStep", "steps"), ("TemperatureBC", "boundaryConditions"),
                       ("PinnedBC", "boundaryConditions"), ("XsymmBC", "boundaryConditions"),
                       ("YsymmBC", "boundaryConditions"), ("ZsymmBC", "boundaryConditions"),
                       ("XasymmBC", "boundaryConditions"), ("YasymmBC", "boundaryConditions"),
                       ("ZasymmBC", "boundaryConditions"), ("SurfaceHeatFlux", "loads")):
    setattr(Model, _method, _repo_maker(_method, _repo))


class XYPlot(Recorder):
    def __init__(self, name):
        Recorder.__init__(self, "XYPlot[" + name + "]")
        self.charts = {"Chart-1": Recorder("XYPlot[" + name + "].chart")}


class Viewport(Recorder):
    """A viewport with real geometry (CAE scripts read origin/width/height to place new ones)."""

    def __init__(self, session, name, origin=(0.0, 0.0), width=200.0, height=150.0):
        Recorder.__init__(self, "viewport[" + name + "]")
        for k, v in (("_session", session), ("name", name), ("origin", tuple(origin)), ("width", width),
                     ("height", height)):
            object.__setattr__(self, k, v)

    def makeCurrent(self):
        LOG.append({"call": self._path + ".makeCurrent", "args": [], "kwargs": {}})
        object.__setattr__(self._session, "currentViewportName", self.name)


class Session(Recorder):
    def __init__(self):
        Recorder.__init__(self, "session")
        # Like CAE: no session.annotations (annotations live in mdb.annotations, made with mdb.Text/mdb.Arrow).
        for repo in ("odbs", "viewports", "xyDataObjects", "xyPlots", "displayGroups", "views"):
            object.__setattr__(self, repo, Repo("session." + repo))
        # Like a fresh CAE session: one viewport, which is current.
        self.viewports["Viewport: 1"] = Viewport(self, "Viewport: 1")
        for v in ("Front", "Back", "Top", "Bottom", "Left", "Right", "Iso"):  # CAE's standard views
            self.views[v] = Recorder("views[" + v + "]")
        object.__setattr__(self, "currentViewportName", "Viewport: 1")

    def openOdb(self, name, path=None, readOnly=False):
        LOG.append({"call": "session.openOdb", "args": [], "kwargs": {"name": name, "path": path}})
        odb = Recorder("odb[" + name + "]")
        self.odbs[name] = odb
        return odb

    def Viewport(self, name, origin=(0.0, 0.0), width=200.0, height=150.0):
        LOG.append({"call": "session.Viewport", "args": [], "kwargs": {"name": name}})
        vp = Viewport(self, name, origin, width, height)
        self.viewports[name] = vp
        object.__setattr__(self, "currentViewportName", name)  # a new viewport becomes current in CAE
        return vp

    def XYData(self, name, data, **kwargs):
        LOG.append({"call": "session.XYData", "args": [], "kwargs": dict(_show(kwargs), name=name, data=_show(data))})
        xy = Recorder("xy[" + name + "]")
        self.xyDataObjects[name] = xy
        return xy

    def XYPlot(self, name):
        LOG.append({"call": "session.XYPlot", "args": [], "kwargs": {"name": name}})
        plot = XYPlot(name)
        self.xyPlots[name] = plot
        return plot


class Mdb(Recorder):
    """The model database: CAE keeps annotations (Text, Arrow) here, not in session."""

    def __init__(self):
        Recorder.__init__(self, "mdb")
        object.__setattr__(self, "annotations", Repo("mdb.annotations"))
        object.__setattr__(self, "models", Repo("mdb.models"))
        object.__setattr__(self, "jobs", Repo("mdb.jobs"))

    def Model(self, name, **kwargs):
        LOG.append({"call": "mdb.Model", "args": [], "kwargs": {"name": name}})
        model = Model(name)
        self.models[name] = model
        return model

    def Job(self, name, **kwargs):
        LOG.append({"call": "mdb.Job", "args": [], "kwargs": _show(dict({"name": name}, **kwargs))})
        job = Recorder("mdb.jobs[" + name + "]")
        self.jobs[name] = job
        return job

    def _open_geometry(self, method, fileName, **kwargs):
        LOG.append({"call": "mdb." + method, "args": [],
                    "kwargs": _show(dict({"fileName": fileName}, **kwargs))})
        return Recorder("mdb." + method + "[" + fileName + "]")

    def openStep(self, fileName, **kwargs):
        return self._open_geometry("openStep", fileName, **kwargs)

    def openIges(self, fileName, **kwargs):
        return self._open_geometry("openIges", fileName, **kwargs)

    def openAcis(self, fileName, **kwargs):
        return self._open_geometry("openAcis", fileName, **kwargs)


# Names real CAE doesn't have on session (a generated script using them must fail here too).
NOT_ON_SESSION = ("annotations", "Text", "Arrow", "highlight", "unhighlight")
_session_getattr = Session.__getattr__


def _strict(self, name):
    if name in NOT_ON_SESSION:
        raise AttributeError("'Session' object has no attribute '%s'" % name)
    return _session_getattr(self, name)


Session.__getattr__ = _strict
session = Session()
mdb = Mdb()


@atexit.register
def _dump():
    path = os.environ.get("NOCODE_CAE_LOG")
    if path:
        with open(path, "w") as fh:
            json.dump(LOG, fh, indent=1)
