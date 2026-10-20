"""Stand-in for Abaqus/CAE's GUI module (tests only).

Just enough of the AFX/FOX API for the NodeCode FEA link plug-in: a timer, sendCommand
into the (simulated) kernel, and a pick procedure that "picks" the entities listed
in $FAKE_PICK (e.g. "BEAM-1:5,BEAM-1:6") as soon as it is activated.
"""

import os

TRUE, FALSE = True, False
SEL_TIMEOUT = "SEL_TIMEOUT"
NODES, ELEMENTS, FACES, EDGES, CELLS, VERTICES = 1, 2, 4, 8, 16, 32
MANY, ONE = "MANY", "ONE"
SELECTION_FILTER_VERTEX, SELECTION_FILTER_EDGE = 1, 2
SELECTION_FILTER_FACE, SELECTION_FILTER_CELL = 4, 8

KERNEL = {}  # the simulated kernel's globals; fake_cae.py runs the boot script in it
ALL = "ALL"
MODULE = {"current": "Part"}  # CAE starts in the Part module
# CAE 2026 runs plug-ins before its main window is shown ($FAKE_HIDDEN_TICKS event-loop passes here); a module
# switched then only changes the Module box, its menus and toolbox are not loaded (seen 2026-10-20).
HIDDEN = {"ticks": int(os.environ.get("FAKE_HIDDEN_TICKS", "0"))}
TREE_TAB = {"current": "Results"}
MENU = []
CONTEXT = {"objectPath": "mdb.models['Fake-Model'].parts['Complex-Part']", "objectType": "Part",
           "moduleName": "Part", "modelName": "Fake-Model", "mdbName": "fake", "viewportName": "Viewport: 1"}


ACTIVE = []  # the pick procedure waiting for clicks ($FAKE_PICK_HOLD), as in real CAE


def switchModule(name):
    # Real CAE 2026 cancels an active pick on any module switch, even to the current module (probed 2026-10-14).
    for proc in list(ACTIVE):
        proc.deactivate()
    MODULE["current"] = name if HIDDEN["ticks"] <= 0 else name + " (box only: window hidden)"


def getCurrentContext():
    return dict(CONTEXT)


class AFXMode(object):
    ID_ACTIVATE = "ID_ACTIVATE"


class _Toolset(object):
    def setFilterTypes(self, types, defaultType):
        self.filterTypes, self.defaultType = types, defaultType

    def registerGuiMenuButton(self, buttonText, object, messageId=None, icon=None, kernelInitString="",
                              applicableModules=ALL, version="", author="", description="", helpUrl=""):
        MENU.append(buttonText)
_handlers = {}
_timers = []
MESSAGES = []


class FXObject(object):
    def __init__(self):
        pass


def FXMAPFUNC(obj, sel_type, sel_id, func):
    _handlers[(id(obj), sel_type, sel_id)] = (obj, func)


class _MainWindow(object):
    def writeToMessageArea(self, text):
        MESSAGES.append(text)

    def setCurrentTreeTab(self, name):
        TREE_TAB["current"] = name

    def shown(self):
        return HIDDEN["ticks"] <= 0


class _App(object):
    def addTimeout(self, tgt, sel, ms):  # FOX 1.6 argument order
        _timers.append((ms, tgt, sel))

    def getAFXMainWindow(self):
        return _MAIN


SEL_COMMAND = "SEL_COMMAND"
OPENED = []  # sketches opened from the Sketch module's Sketch list, as (model, name)


def MKUINT(sel, kind):
    return (sel, kind)


def _mdb():
    import sys
    abaqus = sys.modules.get("abaqus")
    return getattr(abaqus, "mdb", None)


class _Widget(object):
    """A FOX window: children reached by getFirst() / getNext()."""

    def __init__(self, *children):
        self._children = list(children)
        self._next = None
        for a, b in zip(self._children, self._children[1:]):
            a._next = b

    def getFirst(self):
        return self._children[0] if self._children else None

    def getNext(self):
        return self._next


class FXLabel_Ptr(_Widget):
    def __init__(self, text):
        _Widget.__init__(self)
        self.text = text

    def getText(self):
        return self.text


class AFXComboBox_Ptr(_Widget):
    """A context-bar list (Model:, Sketch:): its items follow the kernel's mdb; choosing one is a GUI action."""

    def __init__(self, label, items, chosen):
        _Widget.__init__(self, FXLabel_Ptr(label))
        self._items, self._chosen, self.current = items, chosen, ""

    def getNumItems(self):
        return len(self._items())

    def getItemText(self, i):
        return self._items()[i]

    def getText(self):
        return self.current

    def setCurrentItem(self, i):
        self.current = self._items()[i]

    def getSelector(self):
        return 264

    def getTarget(self):
        return self

    def handle(self, sender, sel, ptr):
        assert sel == (264, SEL_COMMAND), sel
        self._chosen(self.current)
        return 1


def _models():
    mdb = _mdb()
    return sorted(mdb.models.keys()) if mdb is not None else []


def _sketches():
    mdb = _mdb()
    model = CONTEXT["modelName"]
    if MODULE["current"] != "Sketch" or mdb is None or model not in mdb.models:
        return []
    return sorted(k for k in mdb.models[model].sketches.keys() if k != "__edit__")


def _choose_model(name):
    CONTEXT["modelName"] = name


def _open_sketch(name):  # what the Sketch module's edit procedure does
    OPENED.append((CONTEXT["modelName"], name))
    CONTEXT["objectPath"] = "mdb.models[%r].sketches['__edit__']" % CONTEXT["modelName"]
    CONTEXT["objectType"] = "Sketch"


_CONTEXT_BAR = _Widget(_Widget(AFXComboBox_Ptr("Model:", _models, _choose_model),
                               AFXComboBox_Ptr("Sketch:", _sketches, _open_sketch)))
_MainWindow.getFirst = lambda self: _CONTEXT_BAR
_MainWindow.getPluginToolset = lambda self: _TOOLSET
_MainWindow.getToolset = lambda self, name: _TOOLSET
_TOOLSET = _Toolset()


_MAIN = _MainWindow()
_APP = _App()


def getAFXApp():
    return _APP


def run_timers():
    """One pass of the event loop: fire every pending timeout once."""
    pending = list(_timers)
    del _timers[:]
    HIDDEN["ticks"] -= 1
    for ms, tgt, sel in pending:
        obj, func = _handlers[(id(tgt), SEL_TIMEOUT, sel)]
        func(obj, None, sel, None)


def sendCommand(cmd):
    exec(cmd, KERNEL)


class AFXGuiCommand(object):
    def __init__(self, mode, method, objectName, registerQuery=False):
        self.method, self.objectName, self.keywords = method, objectName, []
        mode._commands = getattr(mode, "_commands", []) + [self]


class _Keyword(object):
    def __init__(self, command, name, required, default=None):
        self.name, self.value = name, default
        command.keywords.append(self)

    def setValue(self, value):
        self.value = value


class AFXObjectKeyword(_Keyword):
    pass


class AFXStringKeyword(_Keyword):
    pass


class AFXPickStep(object):
    def __init__(self, owner, keyword, prompt, entities, number=ONE):
        self.keyword, self.prompt, self.entities = keyword, prompt, entities


class _Picked(object):
    def __init__(self, inst, label):
        self.label = label
        self.index = label
        self.pointOn = ((float(label), 0.0, 0.0),)
        self._inst = inst
        self.name = inst
        self.partName = "Part-1"

    def __repr__(self):  # like a real ODB mesh object: the instance only shows up in the repr
        return "({'instanceName': '%s', 'label': %d})" % (self._inst, self.label)


class AFXProcedure(object):
    def __init__(self, owner):
        if isinstance(owner, _MainWindow):  # what CAE 2026 does with a main-window owner
            raise SystemError("<built-in function new_AFXProcedure> returned NULL without setting an exception")
        self.owner = owner

    def activate(self):
        if self.kind_name in ("node", "element") and MODULE["current"] != "Visualization":
            raise RuntimeError("fake CAE: ODB nodes/elements can only be picked in the Visualization module")
        if os.environ.get("FAKE_PICK_HOLD"):  # wait for "Done": the file $FAKE_PICK_HOLD appearing
            ACTIVE.append(self)
            return
        self._finish()

    def _finish(self):
        step = self.getFirstStep()
        picks = [p.split(":") for p in os.environ.get("FAKE_PICK", "").split(",") if p]
        picked_type = (type("Face", (_Picked,), {}) if self.kind_name == "geometry" else _Picked)
        KERNEL["_fake_pick"] = [picked_type(i, int(l)) for i, l in picks]
        step.keyword.value = "_fake_pick"
        for cmd in self._commands:
            args = ", ".join("%s=%s" % (k.name, k.value if isinstance(k, AFXObjectKeyword) else repr(k.value))
                             for k in cmd.keywords)
            sendCommand("%s.%s(%s)" % (cmd.objectName, cmd.method, args))

    def deactivate(self):
        if self in ACTIVE:
            ACTIVE.remove(self)

    def isActive(self):
        hold = os.environ.get("FAKE_PICK_HOLD")
        if self in ACTIVE and hold and os.path.exists(hold):  # the user clicked Done
            os.remove(hold)
            ACTIVE.remove(self)
            self._finish()
        return self in ACTIVE
