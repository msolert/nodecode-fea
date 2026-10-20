# -*- coding: utf-8 -*-
"""NodeCode FEA live link: the part that runs in the Abaqus/CAE *GUI*.

The editor starts CAE in a session folder with this file in
<session>/abaqus_plugins/nocode_link/, where CAE loads plug-ins from at
start-up. Nothing is copied into the Abaqus installation or your home folder;
the link exists only for the CAE session the editor started.

A timer in CAE's own event loop checks <session>/inbox for requests from the
editor every half second, so kernel commands always run on CAE's main thread
(no background threads touching CAE). Everything that happens is written to
<session>/link.log; please send that file if something doesn't work.

Written to run on both Python 2 (older Abaqus) and Python 3 (Abaqus 2024+).
"""
import json
import os
import re
import time
import traceback

from abaqusGui import *  # noqa: F401,F403  (FXObject, getAFXApp, sendCommand, AFX...)

SESSION = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INBOX = os.path.join(SESSION, "inbox")
OUTBOX = os.path.join(SESSION, "outbox")
LOG = os.path.join(SESSION, "link.log")
HEARTBEAT = os.path.join(SESSION, "heartbeat")
TICK_MS = 500


def log(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write("%s gui: %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def write_result(rid, data):
    if not os.path.isdir(OUTBOX):
        os.makedirs(OUTBOX)
    tmp = os.path.join(OUTBOX, rid + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    final = os.path.join(OUTBOX, rid + ".json")
    if os.path.exists(final):
        os.remove(final)
    os.rename(tmp, final)


def message(text):
    try:
        getAFXApp().getAFXMainWindow().writeToMessageArea(text)
    except Exception:
        log("message area: " + text)


_kernel = {"ready": False}


def kernel(cmd):
    """Run a command in the CAE kernel (on CAE's thread). Raises if the command fails."""
    if not _kernel["ready"]:
        sendCommand("import sys; sys.path.insert(0, %r) if %r not in sys.path else None; import nocode_kernel"
                    % (SESSION, SESSION))
        _kernel["ready"] = True
    sendCommand(cmd)


# ------------------------------------------------------------------ picking
KERNEL_INIT = ("import sys; sys.path.insert(0, %r) if %r not in sys.path else None; import nocode_kernel"
               % (SESSION, SESSION))


def to_visualization(module="Visualization"):
    """Switch CAE to a module: Visualization for results (and picking ODB nodes/elements), Assembly for models."""
    try:
        switchModule(module)
        # CAE can retain the Results tab even after a modelling graph switches to Part/Assembly.  Explicitly show
        # the Model tab so the tree follows the model/part placed in the viewport instead of appearing to remain
        # on the original Model-1 results context.
        if module != "Visualization":
            getAFXApp().getAFXMainWindow().setCurrentTreeTab("Model")
    except Exception:
        log("could not switch to the %s module:\n%s" % (module, traceback.format_exc()))


# ------------------------------------------------------------------ opening a sketch
# CAE's Sketch module only shows a sketch opened from its context-bar Sketch list (that starts the module's sketch
# edit procedure); the kernel can't do it, so after the script ran the link picks the model and the sketch in those
# lists, as the user would. The lists fill from the kernel a little later, so it retries for a few ticks.
_sketch = {"want": None, "tries": 0}
SKETCH_TRIES = 10


def _context_combo(win, label):
    """The context bar's combo box labelled `label` ("Model:", "Sketch:"…), or None."""
    child = win.getFirst()
    while child:
        if child.__class__.__name__.startswith("AFXComboBox"):
            first = child.getFirst()
            if first is not None and first.__class__.__name__.startswith("FXLabel") and first.getText() == label:
                return child
        found = _context_combo(child, label)
        if found is not None:
            return found
        child = child.getNext()
    return None


def _choose(label, text, again=False):
    """Pick `text` in a context-bar list as a click does. True when done (or already chosen, unless again)."""
    combo = _context_combo(getAFXApp().getAFXMainWindow(), label)
    if combo is None:
        return False
    items = [combo.getItemText(i) for i in range(combo.getNumItems())]
    if text not in items:
        return False
    if combo.getText() == text and not again:
        return True
    combo.setCurrentItem(items.index(text))
    combo.getTarget().handle(combo, MKUINT(combo.getSelector(), SEL_COMMAND), None)
    return True


def open_sketch():
    """Open the wanted sketch in the Sketch module; called each tick until done or given up."""
    model, name = _sketch["want"]
    _sketch["tries"] += 1
    try:
        if _choose("Model:", model) and _choose("Sketch:", name, again=True):
            _sketch["want"] = None
            log("sketch %s opened" % name)
            return
    except Exception:
        _sketch["want"] = None
        log("could not open sketch %s:\n%s" % (name, traceback.format_exc()))
        return
    if _sketch["tries"] >= SKETCH_TRIES:
        _sketch["want"] = None
        log("sketch %s of model %s isn't in the Sketch module's lists; not opened" % (name, model))


def want_sketch(sketch):
    _sketch["want"] = tuple(sketch) if sketch else None
    _sketch["tries"] = 0


# ------------------------------------------------------------------ the model tree
# Showing a model's part or assembly makes it CAE's current model (context bar), but the Model tree keeps the
# start-up Model-1 open and the new model closed. After a modelling script the link opens the current model's
# branch, closing the others when the model changes. The tree fills a little later, so it retries for a few ticks.
_tree = {"want": False, "tries": 0, "opened": None}
TREE_TRIES = 10


def _model_tree():
    """The Model tab's tree list (its first item is "Model Database"), or None."""
    def find(win):
        child = win.getFirst()
        while child:
            if child.__class__.__name__.startswith("FXTreeList"):
                first = child.getFirstItem()
                if first is not None and first.getText() == "Model Database":
                    return child
            found = find(child)
            if found is not None:
                return found
            child = child.getNext()
        return None
    return find(getAFXApp().getAFXMainWindow())


def open_model_branch():
    """Open the current model's branch of the Model tree; called each tick until done or given up."""
    _tree["tries"] += 1
    try:
        model = getCurrentContext().get("modelName")
        tree = _model_tree()
        models = tree.getFirstItem().getFirst() if tree is not None else None
        while models is not None and models.getText() != "Models":
            models = models.getNext()
        items = []
        item = models.getFirst() if models is not None else None
        while item is not None:
            items.append(item)
            item = item.getNext()
        mine = [i for i in items if i.getText() == model]
        if mine:
            _tree["want"] = False
            if not tree.isItemExpanded(mine[0]):
                tree.expandTree(mine[0], True)
            if model != _tree["opened"]:  # a new model in the viewport: close the others (Model-1)
                for other in items:
                    if other is not mine[0] and tree.isItemExpanded(other):
                        tree.collapseTree(other, True)
                tree.makeItemVisible(mine[0])
                _tree["opened"] = model
                log("model tree opened on %s" % model)
            return
    except Exception:
        _tree["want"] = False
        log("could not open the model tree:\n%s" % traceback.format_exc())
        return
    if _tree["tries"] >= TREE_TRIES:
        _tree["want"] = False


def want_model_branch(module):
    _tree["want"] = bool(module) and module != "Visualization"
    _tree["tries"] = 0


class NoCodePick(AFXProcedure):
    """Pick nodes or elements in the viewport; the kernel half sends the labels to the editor.

    CAE only accepts procedures owned by a toolset or module (not the main window), so the
    two of these are created once, at start-up, owned by the plug-in toolset.
    """

    def __init__(self, owner, kind):
        AFXProcedure.__init__(self, owner)
        self.kind_name = kind
        self.cmd = AFXGuiCommand(mode=self, method="picked", objectName="nocode_kernel", registerQuery=False)
        self.objs = AFXObjectKeyword(self.cmd, "objs", TRUE)
        self.kindKw = AFXStringKeyword(self.cmd, "kind", TRUE, kind)
        self.requestKw = AFXStringKeyword(self.cmd, "request", TRUE, "manual")
        self.modelKw = AFXStringKeyword(self.cmd, "model", TRUE, "")
        self.objectKw = AFXStringKeyword(self.cmd, "object_name", TRUE, "")

    def getFirstStep(self):
        # CAE does not expose instances themselves as an AFX pick entity. Picking any of their geometry identifies
        # the owning instance; this also gives the user the familiar face/edge/cell selection cursor.
        entities = {"node": NODES, "element": ELEMENTS, "sketch": EDGES,
                    "part": FACES | EDGES | CELLS, "instance": FACES | EDGES | CELLS,
                    "geometry": FACES | EDGES | VERTICES | CELLS,
                    "face": FACES, "edge": EDGES, "vertex": VERTICES, "cell": CELLS}[self.kind_name]
        if self.kind_name == "geometry":
            selection = getAFXApp().getAFXMainWindow().getToolset("Selection")
            selection.setFilterTypes(SELECTION_FILTER_VERTEX | SELECTION_FILTER_FACE |
                                     SELECTION_FILTER_CELL | SELECTION_FILTER_EDGE,
                                     SELECTION_FILTER_VERTEX)
        prompt = ("NodeCode FEA: pick the closest geometry (Vertex first; switch to Face, Cell, or Edge as needed), then click Done"
                  if self.kind_name == "geometry" else
                  "NodeCode FEA: click geometry on one %s, then click Done" % self.kind_name
                  if self.kind_name in ("sketch", "part", "instance") else
                  "NodeCode FEA: pick %ss, then click Done (or press the middle mouse button)" % self.kind_name)
        return AFXPickStep(self, self.objs, prompt, entities, MANY)


_procedures = {}


def create_pick_procedures():
    toolset = getAFXApp().getAFXMainWindow().getPluginToolset()
    for kind in ("node", "element", "sketch", "part", "instance", "geometry", "face", "edge", "vertex", "cell"):
        proc = NoCodePick(toolset, kind)
        _procedures[kind] = proc
        try:  # also reachable by hand: Plug-ins > NodeCode FEA
            toolset.registerGuiMenuButton(
                buttonText="NodeCode FEA|Pick %ss for the editor" % kind, object=proc,
                messageId=AFXMode.ID_ACTIVATE, kernelInitString=KERNEL_INIT,
                version="1.0", author="NodeCode FEA", description="Send picked %ss to the NodeCode FEA editor" % kind)
        except Exception:
            log("menu entry for picking %ss not added:\n%s" % (kind, traceback.format_exc()))
    log("pick procedures ready")


def start_pick(kind, rid):
    """Ask the user to click mesh entities or geometry belonging to one modelling object."""
    model_name, object_name = "", ""
    if kind in ("sketch", "part"):
        context = getCurrentContext()
        path = context.get("objectPath", "")
        expected = "Sketch" if kind == "sketch" else "Part"
        repo = "sketches" if kind == "sketch" else "parts"
        match = re.search(r"\.%s\[['\"](.+?)['\"]\]$" % repo, path)
        if context.get("objectType") != expected or not context.get("modelName") or not match:
            write_result(rid, {"ok": False, "error": "Show exactly one %s in the current CAE viewport first" % kind})
            return
        model_name, object_name = context["modelName"], match.group(1)
    proc = _procedures.get(kind)
    if proc is None:
        raise RuntimeError("the pick procedures weren't created at start-up (see the start of link.log)")
    end_pick("a new pick was started")
    kernel("pass")  # make sure nocode_kernel is imported before the procedure issues its command
    if kind in ("node", "element", "instance"):
        to_visualization("Assembly" if kind == "instance" else "Visualization")
    proc.requestKw.setValue(rid)
    proc.modelKw.setValue(model_name)
    proc.objectKw.setValue(object_name)
    proc.activate()
    _pick.update(proc=proc, rid=rid, idle=0)
    log("pick of %s started (request %s)" % (kind, rid))


# A pick runs while the user clicks in the viewport. Switching module cancels it (even to the module CAE is in, which
# every update does first), so updates that switch module wait in the inbox until the pick is done or cancelled.
# A pick that ends without the kernel answering (Cancel, Esc, another CAE tool) is reported as cancelled.
_pick = {"proc": None, "rid": None, "idle": 0}
PICK_GRACE = 2  # ticks the kernel's answer may take after the procedure ends


def _answered(rid):
    return os.path.exists(os.path.join(OUTBOX, rid + ".json"))


def end_pick(reason):
    """Stop waiting for the current pick, reporting it cancelled unless the kernel already answered."""
    proc, rid = _pick["proc"], _pick["rid"]
    if proc is None:
        return
    _pick.update(proc=None, rid=None, idle=0)
    try:
        if proc.isActive():
            proc.deactivate()
    except Exception:
        log("could not end the pick:\n" + traceback.format_exc())
    if not _answered(rid):
        write_result(rid, {"ok": False, "cancelled": True, "error": "Pick cancelled in CAE (%s)" % reason})
        log("pick %s cancelled: %s" % (rid, reason))


def picking():
    """True while a pick waits for the user's clicks in the viewport."""
    proc, rid = _pick["proc"], _pick["rid"]
    if proc is None:
        return False
    if _answered(rid):  # Done: the kernel has the picks (end the procedure in case CAE loops back to its step)
        end_pick("done")
        return False
    if proc.isActive():
        return True
    _pick["idle"] += 1
    if _pick["idle"] > PICK_GRACE:
        end_pick("it was closed in CAE")
    return False


# ------------------------------------------------------------------ requests
def handle(msg):
    rid, kind = msg.get("id", "?"), msg.get("type")
    if kind == "ping":
        write_result(rid, {"ok": True, "pong": time.time()})
    elif kind == "exec":
        if msg.get("module", "Visualization"):  # results in Visualization, models in Assembly / Part / Sketch
            to_visualization(msg.get("module", "Visualization"))
        want_sketch(msg.get("sketch"))
        want_model_branch(msg.get("module", "Visualization"))
        try:
            kernel("nocode_kernel.run(%r, %r)" % (msg["script"], rid))
        except Exception as exc:  # the kernel module reports script errors itself; this is a failed command
            log("exec failed: %s" % exc)
            write_result(rid, {"ok": False, "error": str(exc)})
    elif kind == "pick":
        try:
            start_pick(msg.get("kind", "node"), rid)
        except Exception:
            log("pick failed to start:\n" + traceback.format_exc())
            write_result(rid, {"ok": False, "error": "Couldn't start picking in CAE; see link.log"})
    else:
        write_result(rid, {"ok": False, "error": "unknown request %r" % kind})


_state = {"ticks": 0, "waited": 0}
_held = set()  # updates waiting for a pick (logged once)
STARTUP_TICKS = 240  # give up waiting for the main window after 2 minutes


def _main_window_visible():
    """Whether this process has a visible top-level Abaqus window (Windows), else None (can't tell)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32, me, found = ctypes.windll.user32, os.getpid(), []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value.startswith("Abaqus/"):
                    found.append(hwnd)
            return True

        user32.EnumWindows(each, 0)
        return bool(found)
    except Exception:
        return None


def gui_ready():
    """CAE 2026 runs plug-ins before its main window is shown; a module switched then only changes the Module box
    (the Part module's menus and toolbox stay, the viewport shows nothing). Wait until the window is up."""
    try:
        if getAFXApp().getAFXMainWindow().shown():
            return True
    except Exception:
        pass
    visible = _main_window_visible()  # CAE 2026's shown() stays false: ask Windows
    return True if visible is None else visible  # no way to tell: go on


def poll():
    if not _state["ticks"]:
        if _state["waited"] % 4 == 0:
            with open(HEARTBEAT, "w") as fh:  # the editor sees CAE alive while it starts
                fh.write(str(time.time()))
        if not gui_ready() and _state["waited"] < STARTUP_TICKS:
            if _state["waited"] % 20 == 0:
                log("waiting for CAE's main window")
            _state["waited"] += 1
            return
        log("CAE main window up after %.1f s" % (_state["waited"] * TICK_MS / 1000.0))
    _state["ticks"] += 1
    if _state["ticks"] == 1:  # CAE is up: post-processing happens in the Visualization module
        module, sketch = "Visualization", None
        try:
            with open(os.path.join(SESSION, "initial_module.txt")) as fh:
                lines = [ln.strip() for ln in fh.read().splitlines()]
            module = lines[0] or module
            sketch = lines[1:3] if len(lines) >= 3 else None  # the sketch to open: model, name
        except (IOError, OSError, IndexError):
            pass
        to_visualization(module)
        first = os.path.join(SESSION, "initial.py")
        if os.path.exists(first):  # then show the graph's ODB / outputs there
            want_sketch(sketch)
            want_model_branch(module)
            try:
                kernel("nocode_kernel.run(%r, 'initial')" % first)
            except Exception:
                log("initial display failed:\n" + traceback.format_exc())
    if _state["ticks"] % 4 == 1:
        with open(HEARTBEAT, "w") as fh:
            fh.write(str(time.time()))
    if _sketch["want"] and _state["ticks"] > 1:
        # alone in its tick: the sketch edit procedure needs the event loop before the next kernel command (an
        # update run in the same tick hangs CAE 2026)
        open_sketch()
        return
    if _tree["want"] and _state["ticks"] > 1:
        open_model_branch()
    if not os.path.isdir(INBOX):
        return
    busy = picking()
    for name in sorted(os.listdir(INBOX)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(INBOX, name)
        try:
            with open(path) as fh:
                msg = json.load(fh)
        except Exception:
            continue  # still being written; next tick
        if busy and msg.get("type") == "exec" and msg.get("module", "Visualization"):
            if name not in _held:
                _held.add(name)
                log("request %s waits for the pick in CAE" % msg.get("id"))
            continue  # switching module would cancel the pick; run it once the pick is over
        _held.discard(name)
        os.remove(path)
        log("request %s (%s)" % (msg.get("id"), msg.get("type")))
        handle(msg)
        if msg.get("type") == "pick":
            busy = picking()  # updates queued behind a new pick wait for it


# ------------------------------------------------------------------ timer
class NoCodeTicker(FXObject):
    """Calls poll() from CAE's event loop every TICK_MS milliseconds."""

    [ID_TICK] = range(1001, 1002)

    def __init__(self):
        FXObject.__init__(self)
        FXMAPFUNC(self, SEL_TIMEOUT, NoCodeTicker.ID_TICK, NoCodeTicker.onTick)
        self.schedule()

    def schedule(self):
        app = getAFXApp()
        try:
            app.addTimeout(self, NoCodeTicker.ID_TICK, TICK_MS)  # FOX 1.6 argument order
        except TypeError:
            app.addTimeout(TICK_MS, self, NoCodeTicker.ID_TICK)  # older FOX argument order

    def onTick(self, sender, sel, ptr):
        try:
            poll()
        except Exception:
            log("tick failed:\n" + traceback.format_exc())
        self.schedule()
        return 1


try:
    create_pick_procedures()
except Exception:
    log("pick procedures could not be created:\n" + traceback.format_exc())

try:
    _ticker = NoCodeTicker()
    log("link started in %s" % SESSION)
    message("NodeCode FEA: live link to the editor is active.")
except Exception:
    log("link could not start:\n" + traceback.format_exc())
    message("NodeCode FEA: the live link could not start; see " + LOG)
