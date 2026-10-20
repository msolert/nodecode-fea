# -*- coding: utf-8 -*-
"""NodeCode FEA live link: the part that runs in the Abaqus/CAE *kernel*.

The editor starts CAE in a session folder containing this file. The GUI side
(abaqus_plugins/nocode_link/nocode_link_plugin.py) reads requests from the
editor and calls the functions below with sendCommand(), so everything here runs
on CAE's own thread, between user actions. Results go back as JSON files in
outbox/. Nothing is installed in Abaqus: the files live in the session folder.

Written to run on both Python 2 (older Abaqus) and Python 3 (Abaqus 2024+).
"""
from __future__ import print_function

import json
import os
import re
import sys
import time
import traceback

try:  # Python 2
    from StringIO import StringIO
except ImportError:  # Python 3
    from io import StringIO

SESSION = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(SESSION, "outbox")
LOG = os.path.join(SESSION, "link.log")

# These lines are responses for the editor, not messages for a person.  Mesh
# responses in particular can contain thousands of coordinates, and printing
# them into CAE's message area makes the GUI needlessly slow.
_MACHINE_OUTPUT = (
    "__NODECODE_FEA_MESH__",
    "__NODECODE_FEA_MEASURE__",
    "__NODECODE_FEA_OBJECTS__",
)


def _visible_output(output):
    return "\n".join(line for line in output.splitlines()
                     if not line.lstrip().startswith(_MACHINE_OUTPUT))


def log(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write("%s kernel: %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def write_result(rid, data):
    """Write outbox/<rid>.json atomically (write then rename) so the editor never reads half a file."""
    if not os.path.isdir(OUTBOX):
        os.makedirs(OUTBOX)
    tmp = os.path.join(OUTBOX, rid + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    final = os.path.join(OUTBOX, rid + ".json")
    if os.path.exists(final):
        os.remove(final)
    os.rename(tmp, final)


def run(path, rid):
    """Execute a script the editor generated (contour viewports, XY data...), capturing what it prints."""
    buf = StringIO()
    old = sys.stdout
    ok, error = True, ""
    sys.stdout = buf
    try:
        with open(path, "rb") as fh:  # bytes, so the script's own "coding: utf-8" line decides
            code = compile(fh.read(), path, "exec")
        namespace = {"__name__": "__nocode__", "__file__": path}
        exec(code, namespace)
    except Exception:
        ok, error = False, traceback.format_exc()
    finally:
        sys.stdout = old
    output = buf.getvalue()
    visible = _visible_output(output)
    if visible:
        print(visible.rstrip())  # also show human-readable output in CAE's message area
    if error:
        log("script %s failed:\n%s" % (os.path.basename(path), error))
        print("NodeCode FEA: the update from the editor failed; see link.log in " + SESSION)
    write_result(rid, {"ok": ok, "output": output, "error": error})


def _flatten(objs):
    if objs is None:
        return
    if hasattr(objs, "label") or hasattr(objs, "partName"):
        yield objs
        return
    try:
        items = list(objs)
    except TypeError:
        yield objs
        return
    for item in items:
        for x in _flatten(item):
            yield x


_INSTANCE_RE = re.compile(r"instanceName'?\"?\s*[:=]\s*'([^']*)'")
_REPR_RE = re.compile(r"instances\[\s*'([^']+)'\s*\]")


def _instance_of(obj):
    name = getattr(obj, "instanceName", None)
    if name:
        return name
    text = repr(obj)
    m = _INSTANCE_RE.search(text) or _REPR_RE.search(text)
    return m.group(1) if m else ""


def picked(objs=None, kind="node", request="", model="", object_name=""):
    """Called (through the GUI's pick procedure) with what the user picked in the viewport."""
    try:
        if kind in ("sketch", "part"):
            if not list(_flatten(objs)):
                raise ValueError("Click geometry on one %s" % kind)
            if not model or not object_name:
                raise ValueError("The displayed %s could not be identified" % kind)
            write_result(request, {"ok": True, "kind": kind, "value": "%s|%s" % (model, object_name)})
            return
        if kind in ("geometry", "face", "edge", "vertex", "cell"):
            picked = list(_flatten(objs))
            if not picked:
                raise ValueError("Pick at least one %s" % kind)
            if kind == "geometry":
                kinds = [obj.__class__.__name__.lower() for obj in picked]
                kinds = [k for k in kinds if k in ("face", "edge", "vertex", "cell")]
                if len(kinds) != len(picked):
                    raise ValueError("Pick faces, edges, vertices, or cells")
                if len(set(kinds)) != 1:
                    raise ValueError("One Region must contain one geometry type; pick only faces, edges, vertices, or cells")
                kind = kinds[0]
            points = []
            for obj in picked:
                point_on = getattr(obj, "pointOn", None)
                if not point_on:
                    raise ValueError("CAE did not return a point on the %s" % kind)
                point = point_on[0] if isinstance(point_on[0], (tuple, list)) else point_on
                points.append(tuple(float(c) for c in point))
            points = list(dict.fromkeys(points))
            value = "@" + ";".join(",".join("%.16g" % c for c in point) for point in points)
            write_result(request, {"ok": True, "kind": kind, "value": value})
            return
        if kind == "instance":
            from abaqus import mdb
            values = []
            for obj in _flatten(objs):
                name = getattr(obj, "name", "") or _instance_of(obj)
                part = getattr(obj, "partName", "")
                model = ""
                for model_name, candidate in mdb.models.items():
                    if name in candidate.rootAssembly.instances.keys():
                        model = model_name
                        if not part:
                            part = getattr(candidate.rootAssembly.instances[name], "partName", "")
                        break
                if name and model and part:
                    values.append("%s|%s|%s" % (model, name, part))
            values = list(dict.fromkeys(values))
            if len(values) != 1:
                raise ValueError("Pick exactly one instance")
            write_result(request, {"ok": True, "kind": kind, "value": values[0]})
            return
        labels = []
        for obj in _flatten(objs):
            inst = _instance_of(obj)
            labels.append("%s.%d" % (inst, obj.label) if inst else str(obj.label))
        log("picked %d %s(s) for request %s" % (len(labels), kind, request))
        write_result(request, {"ok": True, "kind": kind, "labels": labels})
    except Exception:
        log("pick failed:\n" + traceback.format_exc())
        write_result(request, {"ok": False, "error": traceback.format_exc()})


def boot():
    """Run once when CAE starts (through 'abaqus cae script=nocode_boot.py')."""
    log("kernel ready (Python %s)" % sys.version.split()[0])
    with open(os.path.join(SESSION, "kernel_ready"), "w") as fh:
        fh.write(str(time.time()))
    # The graph's first display (initial.py) is run by the GUI half on its first tick, right after switching to
    # the Visualization module, so the ODB shows up there.
