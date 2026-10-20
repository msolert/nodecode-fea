"""HTTP API for the editor, plus static hosting of the built frontend."""

from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import cae_import, cae_mesh, cae_queries, cae_reader, cae_sets, checks, jobs, ops
from .cae_export import build_script
from .migrate import migrate_graph
from .cae_link import LinkError, link
from .dialogs import pick_odb
from .engine import Engine, inspect_item, shown_value
from .nodes import CATEGORIES, REGISTRY
from .sources import abaqus_available, abaqus_command

app = FastAPI(title="NodeCode FEA")
engine = Engine()
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/api/status")
def status():
    return {"abaqusCommand": abaqus_command(), "abaqusFound": abaqus_available()}


@app.get("/api/nodes")
def catalog():
    return {"categories": CATEGORIES, "nodes": [d.to_json() for d in REGISTRY.values()]}


@app.get("/api/formula-names")
def formula_names():
    """Inputs, functions and constants a Formula accepts, for the editor's autocomplete."""
    return {"names": ops.formula_names()}


class _Doc:
    """What the live CAE link remembers about one graph (one editor tab, ``doc`` in the payload): each tab builds and
    deletes only its own objects, and its nodes show only what CAE said about them."""

    def __init__(self):
        # Each modelling node's fingerprint at the last update sent to CAE, so the next one shows what changed.
        self.last_sent: dict[str, str] = {}
        # ...and the CAE objects each one made, kept while the node has an error.
        self.last_keys: dict[str, list[str]] = {}
        # What CAE couldn't build at the last modelling sync, per node ({nid: ["In CAE: …"]}); replaced at each sync.
        self.errors: dict[str, list[str]] = {}
        # What CAE reported about objects it built (a defeaturing's volume change), per node; kept until the node's
        # object is built again or the node is gone, since CAE says it only when it builds it.
        self.notes: dict[str, list[str]] = {}


_docs: dict[str, _Doc] = {}
_pending_keys: dict[str, tuple] = {}  # a sync CAE hasn't answered yet: (doc, its node keys)


def _doc(graph: dict | None) -> _Doc:
    return _docs.setdefault(str((graph or {}).get("doc") or ""), _Doc())


def _note_cae_failures(doc: _Doc, output: str, node_keys: dict) -> None:
    doc.errors.clear()
    doc.errors.update(checks.cae_failures(output, node_keys))
    for nid in [n for n in doc.notes if n not in node_keys]:
        del doc.notes[nid]
    doc.notes.update(checks.cae_failures(output, node_keys, checks.NOTE))


def _checked(graph: dict) -> dict:
    """Run the graph and put the model checks' warnings, and what CAE refused at the last sync, on the nodes they
    concern."""
    results, node_inputs = engine.evaluate(graph)
    try:
        found = checks.lint(graph, results, node_inputs)
        doc = _doc(graph)
        for nid, errors in doc.errors.items():
            found[nid] = errors + list(found.get(nid, []))
        for nid, notes in doc.notes.items():
            found[nid] = list(found.get(nid, [])) + notes
        for nid, warnings in found.items():
            if nid in results:
                results[nid] = {**results[nid], "warnings": warnings}
    except Exception as exc:  # a check must never stop the editor
        print(f"model checks failed: {exc!r}")
    try:
        cae_mesh.attach(results, node_inputs)
    except Exception as exc:  # nor a mesh preview
        print(f"mesh preview failed: {exc!r}")
    return results


@app.post("/api/run")
def run(graph: dict):
    # A sync endpoint runs in FastAPI's threadpool, so a slow ODB read doesn't block the server.
    return JSONResponse({"results": _checked(graph)})


@app.post("/api/inspect")
def inspect(body: dict):
    """One item of a node's nested result (the data panel's level pickers): {graph, node, path: [i, j…]}."""
    results, node_inputs = engine.evaluate(body.get("graph") or {})
    nid = body.get("node")
    if nid not in node_inputs:
        raise HTTPException(404, (results.get(nid) or {}).get("message") or "That node has no result")
    try:
        item = inspect_item(shown_value(node_inputs.outputs.get(nid, {}), node_inputs[nid]),
                            [int(i) for i in body.get("path") or []])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(item)


@app.post("/api/node-action")
def node_action(body: dict):
    """A node's button (an "action" setting, e.g. Run / Kill on Job): {graph, node, action}."""
    graph, nid, action = body.get("graph") or {}, body.get("node"), str(body.get("action") or "")
    node = next((n for n in graph.get("nodes", []) if n.get("id") == nid), None)
    defn = REGISTRY.get((node or {}).get("type"))
    if defn is None or defn.action is None:
        raise HTTPException(404, "That node has no actions")
    if not any(p.kind == "action" and action in (p.options or []) for p in defn.params):
        raise HTTPException(400, f"{defn.title} has no action '{action}'")
    results, node_inputs = engine.evaluate(graph)
    if nid not in node_inputs.outputs:
        raise HTTPException(400, (results.get(nid) or {}).get("message") or f"{defn.title} has no result yet")
    try:
        done = defn.action(node_inputs.outputs[nid], action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # an action gives notes, or {"notes", "open": [ODB paths]}: the editor wires the Job into Open ODB
    extra = done if isinstance(done, dict) else {"notes": done}
    return {**extra, **jobs.runner.poll()}


@app.get("/api/jobs")
def jobs_status():
    """Revision of the jobs the editor follows (it changes when one is queued, starts, progresses or ends), and
    how many are queued or running: the editor re-runs the graph when the revision moves."""
    return jobs.runner.poll()


@app.post("/api/migrate")
def migrate(graph: dict):
    """A saved graph with nodes that were merged into general ones turned into those general nodes."""
    new, notes = migrate_graph(graph)
    return {"graph": new, "notes": notes}


@app.post("/api/open-cae")
def open_cae(body: dict):
    """A model of a .cae as a graph of modelling nodes that rebuilds it (body: path, optional model). A file with
    several models and none named gives their names, to choose from."""
    try:
        dump = cae_reader.read_cae(str(body.get("path") or ""), str(body.get("model") or ""))
    except cae_reader.ReadError as exc:
        raise HTTPException(400, str(exc))
    if not dump.get("model"):
        return {"models": dump.get("models", [])}
    out = cae_import.graph_from_dump(dump)
    new, _ = migrate_graph(out["graph"])
    return {"graph": new, "notes": out["notes"], "model": out["model"]}


@app.post("/api/export-cae")
def export_cae(graph: dict):
    """An Abaqus/CAE script that recreates the graph's contour plots, charts and values."""
    results, node_inputs = engine.evaluate(graph)
    try:
        return build_script(graph, results, node_inputs)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ------------------------------------------------------------ live CAE link


def _script_for(graph: dict | None) -> tuple[str | None, str, tuple | None]:
    """The graph's CAE script, the module to show it in (None when there's nothing to show yet) and the sketch to
    open there."""
    _docs.clear()  # a new CAE session: nothing built for any tab yet
    doc = _doc(graph)
    if not graph or not graph.get("nodes"):
        return None, "Visualization", None
    results, node_inputs = engine.evaluate(graph)
    try:
        out = build_script(graph, results, node_inputs)
        doc.last_sent.update(out["fingerprints"])
        doc.last_keys.update(out["node_keys"])
        return out["script"], out["module"] or "Visualization", out["sketch"]
    except ValueError:
        return None, "Visualization", None


@app.post("/api/cae/launch")
def cae_launch(body: dict):
    """Start Abaqus/CAE (or Abaqus/Viewer) with the live link, showing the graph's outputs once it's up.

    product: 'cae', 'viewer', or 'auto' (Viewer when the graph only post-processes results, CAE when it models).
    """
    graph = body.get("graph") or {}
    product = body.get("product") or "auto"
    if product == "auto":
        models = any(REGISTRY.get(n.get("type")) and REGISTRY[n["type"]].category == "Model"
                     for n in graph.get("nodes", []))
        product = "cae" if models else "viewer"
    try:
        script, module, sketch = _script_for(graph)
        return link.launch(script, product, module, sketch)
    except LinkError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/cae/status")
def cae_status():
    return link.status()


@app.post("/api/cae/sync")
def cae_sync(graph: dict):
    """Rebuild the graph's contour plots, XY plots and values in the running CAE session; for modelling, rebuild
    only the CAE objects whose nodes (or anything upstream of them) changed."""
    results, node_inputs = engine.evaluate(graph)
    doc = _doc(graph)
    try:
        out = build_script(graph, results, node_inputs, previous=dict(doc.last_sent),
                           previous_keys=dict(doc.last_keys))
        rid = link.send("exec", script=out["script"], module=out["module"], sketch=out["sketch"])
    except (ValueError, LinkError) as exc:
        raise HTTPException(400, str(exc))
    if graph.get("tab") == "model" or out["fingerprints"]:
        doc.last_sent.clear()
        doc.last_sent.update(out["fingerprints"])
        doc.last_keys.clear()
        doc.last_keys.update(out["node_keys"])
    modelling = graph.get("tab") == "model" or bool(out["node_keys"])
    res = link.wait(rid, 60)
    if res is None:
        if modelling:
            _pending_keys[rid] = (doc, dict(out["node_keys"]))
        return {"ok": None, "id": rid, "pending": True, "contents": out["contents"]}
    before = (dict(doc.errors), dict(doc.notes))
    if modelling:
        _note_cae_failures(doc, str(res.get("output") or ""), out["node_keys"])
    output = str(res.get("output") or "")
    updated = ((bool(res.get("ok")) and (cae_queries.ingest(output) | cae_mesh.ingest(output))) or
               before != (doc.errors, doc.notes))
    return {**res, "id": rid, "contents": out["contents"],
            **({"results": _checked(graph)} if updated else {})}


@app.post("/api/cae/pick")
def cae_pick(body: dict):
    """Start a viewport pick (or capture the displayed sketch/part); poll the result endpoint."""
    kind = str(body.get("kind") or "node")
    if kind not in ("node", "element", "sketch", "part", "instance", "geometry", "face", "edge", "vertex", "cell"):
        raise HTTPException(400, f"Unknown CAE pick kind '{kind}'")
    try:
        return {"id": link.send("pick", kind=kind)}
    except LinkError as exc:
        raise HTTPException(400, str(exc))


OBJECTS = "__NODECODE_FEA_OBJECTS__"


@app.get("/api/cae/objects")
def cae_objects(kind: str = "sketch"):
    """The sketches, parts, instances, materials or sections of every model in the running CAE, as "model|name" (an
    instance: "model|name|part", as the Input reads it), for an Input to choose from instead of picking in the
    viewport."""
    repo = {"sketch": "sketches", "part": "parts", "instance": "rootAssembly.instances", "material": "materials",
            "section": "sections"}.get(kind)
    if repo is None:
        raise HTTPException(400, f"Can't list CAE objects of kind '{kind}'")
    script = "\n".join([
        "# -*- coding: utf-8 -*-", "from __future__ import print_function", "import json", "from abaqus import mdb",
        f"print({OBJECTS!r} + json.dumps([m + '|' + n + ('|' + mdb.models[m].{repo}[n].partName "
        f"if {kind == 'instance'!r} else '') for m in sorted(mdb.models.keys()) "
        f"for n in sorted(mdb.models[m].{repo}.keys()) if not n.startswith('__')]))", ""])
    try:
        rid = link.send("exec", script=script, module="")
    except LinkError as exc:
        raise HTTPException(400, str(exc))
    res = link.wait(rid, 30)
    if res is None:
        raise HTTPException(504, "CAE didn't answer (is it busy?)")
    line = next((ln for ln in str(res.get("output") or "").splitlines() if ln.startswith(OBJECTS)), None)
    if line is None:
        raise HTTPException(400, str(res.get("error") or "CAE didn't list its objects"))
    return {"items": json.loads(line[len(OBJECTS):])}


@app.get("/api/cae/sets")
def cae_sets_list():
    """The geometry Sets and Surfaces defined in the running CAE, each as Find-at points a Region can take over."""
    try:
        rid = link.send("exec", script=cae_sets.script(), module="")
    except LinkError as exc:
        raise HTTPException(400, str(exc))
    res = link.wait(rid, 60)
    if res is None:
        raise HTTPException(504, "CAE didn't answer (is it busy?)")
    items = cae_sets.parse(str(res.get("output") or ""))
    if items is None:
        raise HTTPException(400, str(res.get("error") or "CAE didn't list its sets"))
    return {"items": items}


@app.get("/api/cae/result/{rid}")
def cae_result(rid: str):
    res = link.result(rid)
    if res is not None and rid in _pending_keys:
        doc, node_keys = _pending_keys.pop(rid)
        _note_cae_failures(doc, str(res.get("output") or ""), node_keys)
    return {"done": res is not None, "result": res}


@app.post("/api/native-browse")
def native_browse(body: dict):
    """Open the operating system's file dialog on this machine and return the chosen path."""
    return pick_odb(str(body.get("current") or ""), _extensions(body.get("extensions")))


def _extensions(value) -> list[str]:
    """The file extensions a picker lists: a setting's accepted ones, else ODBs."""
    items = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    exts = [("." + str(e).strip().lstrip(".")).lower() for e in items if str(e).strip()]
    return exts or [".odb"]


@app.get("/api/browse")
def browse(path: str = "", extensions: str = ""):
    """List folders and the files a setting accepts (default .odb) so the editor can offer a file picker.

    The server only listens on localhost by default; this exposes directory names
    on this machine, so don't run it on a public interface.
    """
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
        entries = list(os.scandir(target))
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, f"Can't open {target}: {exc.strerror if isinstance(exc, OSError) else exc}")
    dirs, files = [], []
    wanted = tuple(_extensions(extensions))
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append(e.name)
            elif e.name.lower().endswith(wanted):
                st = e.stat()
                files.append({"name": e.name, "size": st.st_size, "modified": st.st_mtime})
        except OSError:
            continue
    roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")] if os.name == "nt" else ["/"]
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "home": str(Path.home()), "roots": roots,
            "dirs": sorted(dirs, key=str.lower), "files": sorted(files, key=lambda f: f["name"].lower())}


FRONTEND_SRC = FRONTEND.parent


@app.get("/{path:path}")
def spa(path: str):
    """The built editor (checked per request, so a build made after startup is picked up)."""
    index = FRONTEND / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<h2>The editor hasn't been built yet</h2><p>Run <code>npm install</code> and <code>npm run build</code> "
            "in the <code>frontend</code> folder, or restart <code>nodecode-fea</code> with Node.js installed.</p>",
            status_code=503)
    target = (FRONTEND / path).resolve()
    if path and target.is_file() and FRONTEND.resolve() in target.parents:
        return FileResponse(target)
    return FileResponse(index)


def frontend_stale() -> bool:
    """True when the editor's source is newer than its build (e.g. after a git pull)."""
    index = FRONTEND / "index.html"
    if not FRONTEND_SRC.joinpath("package.json").exists():
        return False  # installed without the frontend sources: nothing to rebuild from
    if not index.exists():
        return True
    built = index.stat().st_mtime
    sources = [FRONTEND_SRC / "package.json", FRONTEND_SRC / "index.html", *FRONTEND_SRC.joinpath("src").rglob("*")]
    return any(p.is_file() and p.stat().st_mtime > built for p in sources)


def build_frontend() -> bool:
    """Rebuild the editor with npm if its sources changed. Returns False if it couldn't."""
    if not frontend_stale():
        return True
    npm = shutil.which("npm")
    if npm is None:
        print("WARNING: the editor's source changed but Node.js/npm isn't installed, so it can't be rebuilt.\n"
              "         You may be looking at an old version of the editor. Install Node.js 18+ and restart.")
        return False
    print("The editor's source changed since it was last built; rebuilding (about 20 s)...")
    steps = [] if FRONTEND_SRC.joinpath("node_modules").exists() else [[npm, "install", "--no-audit", "--no-fund"]]
    steps.append([npm, "run", "build"])
    for cmd in steps:
        if subprocess.run(cmd, cwd=FRONTEND_SRC).returncode != 0:
            print(f"WARNING: '{' '.join(cmd[1:])}' failed; the editor may be out of date.")
            return False
    print("Editor rebuilt.")
    return True


def main():
    import argparse
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(description="NodeCode FEA server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="open the editor in a browser")
    parser.add_argument("--no-build", action="store_true", help="don't rebuild the editor when its source changed")
    args = parser.parse_args()
    if not args.no_build:
        build_frontend()
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
