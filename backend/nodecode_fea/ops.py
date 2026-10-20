"""Numerical helpers: tensor invariants and a safe arithmetic expression evaluator."""

from __future__ import annotations

import ast
import functools
import math
import operator
import re
import textwrap
from collections import ChainMap

import numpy as np

INVARIANTS = ["Mises", "Max. Principal", "Mid. Principal", "Min. Principal", "Tresca", "Pressure", "Magnitude"]
# Abaqus' names for invariants (fieldOutput.validInvariants) -> ours, which are also CAE's refinement labels.
ABAQUS_INVARIANTS = {"MISES": "Mises", "MAX_PRINCIPAL": "Max. Principal", "MID_PRINCIPAL": "Mid. Principal",
                     "MIN_PRINCIPAL": "Min. Principal", "TRESCA": "Tresca", "PRESS": "Pressure",
                     "MAGNITUDE": "Magnitude"}


def available_invariants(kind: str, reported: list[str] | None) -> list[str]:
    """The invariants that exist for a variable: those the ODB reports, else those its type allows."""
    if reported is not None:
        return [i for i in INVARIANTS if i in reported]
    if kind == "TENSOR":
        return [i for i in INVARIANTS if i != "Magnitude"]
    return ["Magnitude"] if kind == "VECTOR" else []
STRAIN_PREFIXES = ("E", "LE", "PE", "NE", "EE", "IE", "THE", "ER", "LEP")


def _is_strain(name: str) -> bool:
    return name.upper().startswith(STRAIN_PREFIXES) and not name.upper().startswith(("ENER", "EVOL", "ESED"))


def tensor_matrix(components: list[str], values: np.ndarray, name: str) -> np.ndarray:
    """Build (n, 3, 3) symmetric tensors from Abaqus component columns such as S11..S23.

    Abaqus stores engineering shear strains, so shear terms of strain-like fields are halved.
    """
    n = values.shape[0]
    t = np.zeros((n, 3, 3))
    shear_factor = 0.5 if _is_strain(name) else 1.0
    found = False
    for col, comp in enumerate(components):
        ij = comp[-2:]
        if len(ij) != 2 or not ij.isdigit():
            continue
        i, j = int(ij[0]) - 1, int(ij[1]) - 1
        if not (0 <= i < 3 and 0 <= j < 3):
            continue
        found = True
        v = values[:, col] * (shear_factor if i != j else 1.0)
        t[:, i, j] = v
        t[:, j, i] = v
    if not found:
        raise ValueError(f"'{name}' has no tensor components ({', '.join(components)})")
    return t


def invariant(kind: str, components: list[str], values: np.ndarray, name: str, ftype: str) -> np.ndarray:
    if kind == "Magnitude":
        return np.linalg.norm(values, axis=1)
    if ftype in ("SCALAR", "VECTOR") or values.shape[1] == 1:
        raise ValueError(f"'{kind}' needs a tensor field; '{name}' is a {ftype.lower()}. Use Magnitude or Component.")
    t = tensor_matrix(components, values, name)
    if kind == "Mises":
        s = t - np.trace(t, axis1=1, axis2=2)[:, None, None] * np.eye(3) / 3.0
        return np.sqrt(1.5 * np.einsum("nij,nij->n", s, s))
    if kind == "Pressure":
        return -np.trace(t, axis1=1, axis2=2) / 3.0
    eig = np.linalg.eigvalsh(t)  # ascending
    if kind == "Max. Principal":
        return eig[:, 2]
    if kind == "Mid. Principal":
        return eig[:, 1]
    if kind == "Min. Principal":
        return eig[:, 0]
    if kind == "Tresca":
        return eig[:, 2] - eig[:, 0]
    raise ValueError(f"Unknown invariant '{kind}'")


def classify_type(ftype: str, ncomp: int) -> str:
    ftype = ftype.upper()
    if ftype.startswith("TENSOR"):
        return "TENSOR"
    if ftype == "VECTOR":
        return "VECTOR"
    return "SCALAR" if ncomp == 1 else "VECTOR"


REDUCERS = ["Max", "Min", "Abs Max", "Mean", "Sum"]


def reduce(op: str, values: np.ndarray) -> tuple[float, int | None]:
    """Reduce a 1-D array, ignoring NaNs. Returns (value, index of the extreme or None)."""
    v = np.asarray(values, dtype=float)
    ok = ~np.isnan(v)
    if not ok.any():
        raise ValueError("No values to reduce (is the set empty?)")
    if op == "Mean":
        return float(np.nanmean(v)), None
    if op == "Sum":
        return float(np.nansum(v)), None
    if op == "Max":
        i = int(np.nanargmax(v))
    elif op == "Min":
        i = int(np.nanargmin(v))
    elif op == "Abs Max":
        i = int(np.nanargmax(np.abs(v)))
    else:
        raise ValueError(f"Unknown operation '{op}'")
    return float(v[i]), i


# ------------------------------------------------------------- expressions
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
           ast.MatMult: lambda a, b: np.matmul(a, b)}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "log": np.log, "log10": np.log10,
          "sin": np.sin, "cos": np.cos, "tan": np.tan, "min": np.minimum, "max": np.maximum,
          "round": np.round}
_CONSTS = {"pi": math.pi, "e": math.e, "True": True, "False": False, "true": True, "false": False}
_COMPARE = {ast.Gt: np.greater, ast.GtE: np.greater_equal, ast.Lt: np.less, ast.LtE: np.less_equal,
            ast.Eq: np.equal, ast.NotEq: np.not_equal}
_FUNCS["where"] = lambda c, a, b: np.where(c, a, b)
_FUNCS["len"] = len


def _as_number(x, kind: str):
    """int(x) / float(x) as in Python, but element by element on arrays (fields, lists, table columns)."""
    arr = np.asarray(x)
    if arr.dtype.kind in "USO":
        try:
            arr = arr.astype(float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{kind}() needs numbers or texts that are numbers: {exc}") from exc
    arr = np.trunc(arr) if kind == "int" else arr.astype(float)
    if arr.ndim == 0:
        v = arr.item()
        return int(v) if kind == "int" and math.isfinite(v) else float(v)
    return arr


_FUNCS["int"] = lambda x: _as_number(x, "int")


def _range(*args):
    r = range(*(int(a) for a in args))
    if len(r) > MAX_STEPS:
        raise ValueError(f"range of {len(r):,} numbers: too many")
    return r


def _minmax(reduce, pairwise):
    """min(list) / max(list): the smallest / largest; min(a, b, …): element by element."""
    def f(*args, **kw):
        if len(args) == 1:
            return reduce(args[0], **kw)
        return functools.reduce(pairwise, args)
    return f


_FUNCS["min"] = _minmax(np.min, np.minimum)
_FUNCS["max"] = _minmax(np.max, np.maximum)
_FUNCS.update(range=_range, enumerate=lambda x, start=0: list(enumerate(x, int(start))), zip=lambda *a: list(zip(*a)),
              sorted=sorted, reversed=lambda x: list(reversed(x)), list=list, dict=dict, str=str, bool=bool)
_FUNCS["float"] = lambda x: _as_number(x, "float")

# NumPy functions allowed in formulas, as np.<name> (or numpy.<name>). A curated list of maths only:
# nothing that reads or writes files, imports modules or runs code, so a shared graph stays harmless.
_NP_NAMES = """
abs absolute sign sqrt cbrt square exp exp2 expm1 log log10 log2 log1p power float_power reciprocal
sin cos tan arcsin arccos arctan arctan2 sinh cosh tanh arcsinh arccosh arctanh hypot deg2rad rad2deg degrees radians
floor ceil trunc rint round around fix mod fmod remainder clip minimum maximum fmin fmax
sum prod mean median average std var min max amin amax ptp argmin argmax argsort sort unique count_nonzero
nansum nanprod nanmean nanmedian nanstd nanvar nanmin nanmax nanargmin nanargmax
cumsum cumprod diff gradient percentile quantile nanpercentile nanquantile interp trapz trapezoid convolve
where select piecewise isnan isinf isfinite nan_to_num logical_and logical_or logical_not logical_xor
all any greater greater_equal less less_equal equal not_equal isclose allclose
linspace arange zeros ones full zeros_like ones_like full_like concatenate stack hstack vstack repeat tile flip roll
dot cross outer inner matmul linalg_norm
transpose reshape ravel trace diag diagonal eye identity swapaxes moveaxis kron tensordot array asarray
""".split()
_NP_FUNCS = {n: getattr(np, n) for n in _NP_NAMES if hasattr(np, n)}
_NP_FUNCS["norm"] = np.linalg.norm
# Matrix algebra, as np.linalg.<name> or just <name>. A stack of matrices (one 3x3 per node/element) works too.
_LINALG = {n: getattr(np.linalg, n) for n in ("inv", "det", "eig", "eigvals", "eigh", "eigvalsh", "solve", "pinv",
                                              "norm", "matrix_rank", "matrix_power", "cholesky", "svd")}
for _n, _f in _LINALG.items():
    _NP_FUNCS.setdefault(_n, _f)
_NP_FUNCS["transpose"] = lambda a, axes=None: transpose(a) if axes is None else np.transpose(a, axes)
_NP_FUNCS["trace"] = lambda a, *args, **kw: np.trace(a, axis1=-2, axis2=-1) if not args and not kw else np.trace(a, *args, **kw)


def _safe_function(f) -> bool:
    return any(f is g for table in (_FUNCS, _NP_FUNCS, _LINALG) for g in table.values())


def transpose(a):
    """Transpose a matrix; for a stack of matrices (one per node/element) transpose each one."""
    a = np.asarray(a)
    return np.swapaxes(a, -1, -2) if a.ndim >= 2 else a
_NP_CONSTS = {"pi": np.pi, "e": np.e, "inf": np.inf, "nan": np.nan}


def _is_np(node) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in ("np", "numpy"))


def _is_linalg(node) -> bool:
    return isinstance(node, ast.Attribute) and _is_np(node.value) and node.value.attr == "linalg"


def tidy(out):
    """Part of a table (an object array of names and numbers): numbers if it's all numbers, texts if all texts,
    else kept mixed, like a row of the table."""
    if not isinstance(out, np.ndarray) or out.dtype != object:
        return out
    try:
        return out.astype(float)
    except (TypeError, ValueError):
        pass
    return out.astype(str) if all(isinstance(v, str) for v in out.flat) else out


def parse(expr: str):
    """A formula as a syntax tree: one expression, or a script (several lines, assignments, if, for, def…)."""
    text = (expr or "").strip() or "0"
    try:
        return ast.parse(text, mode="eval")
    except SyntaxError:
        pass
    try:
        return ast.parse(textwrap.dedent((expr or "").strip("\n")), mode="exec")
    except SyntaxError as exc:
        where = f" (line {exc.lineno})" if exc.lineno and "\n" in text else ""
        raise ValueError(f"Invalid formula{where}: {exc.msg}") from exc


def is_script(expr: str) -> bool:
    """True when the formula is a script (statements, several lines) rather than one expression."""
    return isinstance(parse(expr), ast.Module)


def evaluate(expr: str, names: dict):
    """Evaluate formulas like ``250 / a``, ``sqrt(x**2 + 1)``, ``x > 355`` or ``x if x > 0 else 0`` without exec/eval.

    Comparisons, ``and``/``or``/``not``, ``a if c else b`` and ``where(c, a, b)`` work element by element on
    arrays (field values), so they can be used inside Field Math too. A script of several lines (assignments,
    if/for/while, small functions) works too: see :func:`run_script`.
    """
    return run_script(expr, names)[0]


def run_script(expr: str, names: dict) -> tuple:
    """Run a formula or a small script; returns (its value, the variables it set).

    The value is the last line when that is an expression, else the variable ``result``. Scripts are interpreted
    here, statement by statement, from a whitelist (no imports, no files, no attributes but a few safe ones), so a
    shared graph stays harmless; loops are capped at MAX_STEPS steps.
    """
    tree = parse(expr)
    interp = _Interp(names, script=isinstance(tree, ast.Module))
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if isinstance(tree, ast.Expression):
            try:
                return _not_a_function(interp.ev(tree.body, interp.top)), {}
            except (OverflowError, ZeroDivisionError, TypeError, IndexError, KeyError, AttributeError) as exc:
                raise ValueError(f"Math error: {exc}") from exc
        value, last = None, False
        for stmt in tree.body:
            last = isinstance(stmt, ast.Expr)
            value = interp.ex(stmt, interp.top)
        env = interp.top.maps[0]
        if not last:
            if "result" not in env:
                raise ValueError("The script must end with the value to give (a last line like 'peak') "
                                 "or set 'result = …'")
            value = env["result"]
        return _not_a_function(value), env


def _not_a_function(v):
    if isinstance(v, _Func) or callable(v):
        raise ValueError("The formula gives a function, not a value: call it, e.g. f(a)")
    return v


MAX_STEPS = 2_000_000  # loop rounds and function calls in one script run
_MAX_DEPTH = 100  # nested function calls
_MAX_REPEAT = 10_000_000  # [0] * n, "x" * n

# What scripts may call on their values: nothing that changes inputs in place, opens files or reaches internals.
_METHODS = {
    list: {"append", "extend", "insert", "pop", "index", "count", "remove", "reverse", "copy"},
    dict: {"keys", "values", "items", "get", "copy", "update", "pop"},
    str: {"upper", "lower", "strip", "lstrip", "rstrip", "split", "join", "replace", "startswith", "endswith",
          "find", "title", "capitalize", "count", "zfill", "isdigit"},
    np.ndarray: {"sum", "mean", "max", "min", "std", "var", "prod", "argmax", "argmin", "cumsum", "cumprod",
                 "reshape", "flatten", "ravel", "copy", "round", "tolist", "any", "all", "clip", "argsort", "dot",
                 "nonzero", "transpose", "squeeze", "item", "trace", "diagonal"},
}
_ATTRS = {"shape", "size", "ndim", "real", "imag"}


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Return(Exception):
    def __init__(self, value):
        super().__init__()
        self.value = value


class _Func:
    """A function the script defines (def or lambda): run by the same interpreter, with its own variables."""

    def __init__(self, interp: "_Interp", name: str, args: ast.arguments, body, scope: ChainMap, lam: bool):
        if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
            raise ValueError(f"'{name}': only plain arguments (with defaults) are supported")
        self.interp, self.name, self.body, self.scope, self.lam = interp, name, body, scope, lam
        self.params = [a.arg for a in args.args]
        defaults = [interp.ev(d, scope) for d in args.defaults]
        self.defaults = dict(zip(self.params[len(self.params) - len(defaults):], defaults))

    def __call__(self, *args, **kwargs):
        if len(args) > len(self.params):
            raise ValueError(f"{self.name}() takes {len(self.params)} arguments, got {len(args)}")
        bound = dict(self.defaults) | dict(zip(self.params, args))
        for k, v in kwargs.items():
            if k not in self.params:
                raise ValueError(f"{self.name}() has no argument '{k}'")
            bound[k] = v
        missing = [p for p in self.params if p not in bound]
        if missing:
            raise ValueError(f"{self.name}() needs {', '.join(missing)}")
        it = self.interp
        it.step()
        if it.depth >= _MAX_DEPTH:
            raise ValueError(f"{self.name}() calls itself too deeply (more than {_MAX_DEPTH} levels)")
        it.depth += 1
        try:
            scope = self.scope.new_child(bound)
            if self.lam:
                return it.ev(self.body, scope)
            for stmt in self.body:
                it.ex(stmt, scope)
            return None
        except _Return as r:
            return r.value
        finally:
            it.depth -= 1


def _repeat_guard(a, b):
    for seq, n in ((a, b), (b, a)):
        if isinstance(seq, (list, tuple, str)) and isinstance(n, (int, np.integer)) and n * max(len(seq), 1) > _MAX_REPEAT:
            raise ValueError(f"That repeats a list or text {n} times: too big")


class _Interp:
    def __init__(self, names: dict, script: bool):
        # Scripts may change their variables in place (x[0] = 1): work on copies, so inputs upstream stay as they were.
        env = {k: (v.copy() if script and isinstance(v, np.ndarray) else v) for k, v in names.items()}
        self.missing = {k for k, v in names.items() if v is None}
        self.top = ChainMap(env)
        self.steps = 0
        self.depth = 0
        self.script = script

    def step(self):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise ValueError(f"The script ran more than {MAX_STEPS:,} loop rounds; is a loop endless?")

    # ------------------------------------------------------------ statements
    def ex(self, stmt, scope):
        """Run one statement; errors say which line they came from."""
        try:
            return self._ex(stmt, scope)
        except (_Break, _Continue, _Return):
            raise
        except _Lined:
            raise
        except (ValueError, OverflowError, ZeroDivisionError, TypeError, IndexError, KeyError, AttributeError) as exc:
            msg = str(exc) if isinstance(exc, ValueError) else (
                f"'{exc.args[0]}' isn't there" if isinstance(exc, KeyError) else f"Math error: {exc}")
            raise _Lined(f"Line {stmt.lineno}: {msg}") from exc

    def _ex(self, s, scope):
        if isinstance(s, ast.Expr):
            return self.ev(s.value, scope)
        if isinstance(s, ast.Assign):
            v = self.ev(s.value, scope)
            for t in s.targets:
                self.store(t, v, scope)
            return None
        if isinstance(s, ast.AnnAssign):
            if s.value is not None:
                self.store(s.target, self.ev(s.value, scope), scope)
            return None
        if isinstance(s, ast.AugAssign):
            if type(s.op) not in _BINOPS:
                raise ValueError("Only + - * / // % ** @ can be combined with =")
            cur = self.ev(_load(s.target), scope)
            self.store(s.target, self.binop(s.op, cur, self.ev(s.value, scope)), scope)  # not in place
            return None
        if isinstance(s, ast.If):
            for stmt in (s.body if self.truth(self.ev(s.test, scope), "if") else s.orelse):
                self.ex(stmt, scope)
            return None
        if isinstance(s, ast.For):
            broke = False
            for item in self.iterate(self.ev(s.iter, scope)):
                self.step()
                self.store(s.target, item, scope)
                try:
                    for stmt in s.body:
                        self.ex(stmt, scope)
                except _Continue:
                    continue
                except _Break:
                    broke = True
                    break
            if not broke:
                for stmt in s.orelse:
                    self.ex(stmt, scope)
            return None
        if isinstance(s, ast.While):
            broke = False
            while self.truth(self.ev(s.test, scope), "while"):
                self.step()
                try:
                    for stmt in s.body:
                        self.ex(stmt, scope)
                except _Continue:
                    continue
                except _Break:
                    broke = True
                    break
            if not broke:
                for stmt in s.orelse:
                    self.ex(stmt, scope)
            return None
        if isinstance(s, ast.Break):
            raise _Break()
        if isinstance(s, ast.Continue):
            raise _Continue()
        if isinstance(s, ast.Pass):
            return None
        if isinstance(s, ast.FunctionDef):
            if s.decorator_list:
                raise ValueError("Decorators aren't available")
            scope[s.name] = _Func(self, s.name, s.args, s.body, scope, lam=False)
            return None
        if isinstance(s, ast.Return):
            if self.depth == 0:
                raise ValueError("'return' only works inside a def; end the script with the value instead")
            raise _Return(None if s.value is None else self.ev(s.value, scope))
        if isinstance(s, ast.Assert):
            if not self.truth(self.ev(s.test, scope), "assert"):
                raise ValueError(str(self.ev(s.msg, scope)) if s.msg is not None else "Check failed (assert)")
            return None
        if isinstance(s, ast.Delete):
            for t in s.targets:
                if isinstance(t, ast.Name):
                    scope.maps[0].pop(t.id, None)
                elif isinstance(t, ast.Subscript):
                    del self.ev(t.value, scope)[self.index(t.slice, scope)]
                else:
                    raise ValueError("del takes a name or an item: del x, del d['k']")
            return None
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            raise ValueError("Imports aren't available: np (NumPy) and the maths functions are already there")
        raise ValueError(f"'{type(s).__name__}' isn't available in scripts; use assignments, if/elif/else, for, "
                         "while, def and return")

    def store(self, t, v, scope):
        if isinstance(t, ast.Name):
            if t.id in ("np", "numpy"):
                raise ValueError(f"'{t.id}' is NumPy; give the variable another name")
            scope[t.id] = v
            self.missing.discard(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            items = list(self.iterate(v))
            if len(items) != len(t.elts):
                raise ValueError(f"Unpacking {len(items)} values into {len(t.elts)} names")
            for sub, item in zip(t.elts, items):
                self.store(sub, item, scope)
        elif isinstance(t, ast.Subscript):
            obj = self.ev(t.value, scope)
            if not isinstance(obj, (np.ndarray, list, dict)):
                raise ValueError(f"Can't change part of a {_kind(obj)}")
            obj[self.index(t.slice, scope)] = v
        else:
            raise ValueError("Assign to a name (x = …), several names (x, y = …) or an item (x[0] = …)")

    # ----------------------------------------------------------- expressions
    def lookup(self, name: str, scope):
        if name in scope:
            v = scope[name]
            if v is None and name in self.missing:
                raise ValueError(f"Input '{name}' is not connected")
            return v
        if name in _CONSTS:
            return _CONSTS[name]
        if name in _FUNCS:
            return _FUNCS[name]
        if name in _NP_FUNCS:
            return _NP_FUNCS[name]
        raise ValueError(f"Unknown name '{name}'")

    def truth(self, v, what: str) -> bool:
        if isinstance(v, np.ndarray) and v.size != 1:
            raise ValueError(f"'{what}' got {v.size} values, not one yes/no: use np.any(…) or np.all(…)")
        return bool(v)

    def iterate(self, v):
        if isinstance(v, (int, float, bool, np.number, np.bool_)) or v is None:
            raise ValueError(f"Can't loop over a single {_kind(v)}; use range(n) to count")
        return v

    def binop(self, op, left, right):
        if isinstance(op, ast.Pow):  # floats keep 10**10**10 from building a huge int
            if type(left) is int and type(right) is int and right >= 0 and abs(float(left) ** right) < 2 ** 53:
                return left ** right  # small whole powers stay whole: range(10**3), [0] * 2**4
            return np.power(np.asarray(left, dtype=float), right) if np.ndim(left) else float(left) ** right
        if isinstance(op, ast.Mult):
            _repeat_guard(left, right)
        return _BINOPS[type(op)](left, right)

    def index(self, sl, scope):
        return tuple(self.ev(e, scope) for e in sl.elts) if isinstance(sl, ast.Tuple) else self.ev(sl, scope)

    def call(self, node, scope):
        f = node.func
        func = None
        if isinstance(f, ast.Name):
            if f.id in scope:
                func = scope[f.id]
                if not (isinstance(func, _Func) or _safe_function(func)):
                    raise ValueError(f"'{f.id}' is a {_kind(func)}, not a function")
            elif f.id in _FUNCS:
                func = _FUNCS[f.id]
            elif f.id in _NP_FUNCS:
                func = _NP_FUNCS[f.id]
        elif _is_linalg(f):
            func = _LINALG.get(f.attr)
            if func is None:
                raise ValueError(f"np.linalg.{f.attr} isn't available in formulas")
        elif _is_np(f):
            func = _NP_FUNCS.get(f.attr)
            if func is None:
                raise ValueError(f"np.{f.attr} isn't available in formulas")
        elif isinstance(f, ast.Attribute):  # a method: x.append(1), s.upper(), a.reshape(3, 3)
            obj = self.ev(f.value, scope)
            allowed = next((m for t, m in _METHODS.items() if isinstance(obj, t)), set())
            if f.attr not in allowed:
                raise ValueError(f"A {_kind(obj)} has no '.{f.attr}()' here" + (
                    f" (available: {', '.join(sorted(allowed))})" if allowed else ""))
            func = getattr(obj, f.attr)
        if func is None:
            raise ValueError(f"Unknown function in '{ast.unparse(f)}'")
        args = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                args.extend(self.iterate(self.ev(a.value, scope)))
            else:
                args.append(self.ev(a, scope))
        kwargs = {}
        for k in node.keywords:
            if not k.arg:
                raise ValueError("**keywords aren't available")
            if k.arg == "out":
                raise ValueError("out= isn't available: assign the result instead")
            kwargs[k.arg] = self.ev(k.value, scope)
        return func(*args, **kwargs)

    def comprehension(self, node, scope):
        out = {} if isinstance(node, ast.DictComp) else []
        inner = scope.new_child()

        def gen(i):
            if i == len(node.generators):
                if isinstance(node, ast.DictComp):
                    out[self.ev(node.key, inner)] = self.ev(node.value, inner)
                else:
                    out.append(self.ev(node.elt, inner))
                return
            g = node.generators[i]
            for item in self.iterate(self.ev(g.iter, inner)):
                self.step()
                self.store(g.target, item, inner)
                if all(self.truth(self.ev(c, inner), "if") for c in g.ifs):
                    gen(i + 1)

        gen(0)
        return out

    def ev(self, node, scope):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (bool, int, float, str)) or node.value is None:
                return node.value  # whole numbers stay ints (np.round(x, 2)); ** works in floats, see binop
            raise ValueError(f"{type(node.value).__name__} values aren't available")
        if isinstance(node, ast.Compare):
            left, result = self.ev(node.left, scope), True
            for op, right_node in zip(node.ops, node.comparators):
                right = self.ev(right_node, scope)
                if type(op) in _COMPARE:
                    r = _COMPARE[type(op)](left, right)
                elif isinstance(op, (ast.In, ast.NotIn)):
                    r = (left in right) == isinstance(op, ast.In)
                elif isinstance(op, (ast.Is, ast.IsNot)):
                    r = (left is right) == isinstance(op, ast.Is)
                else:
                    raise ValueError("Use < <= > >= == != in is to compare")
                result = np.logical_and(result, r)
                left = right
            return result
        if isinstance(node, ast.BoolOp):
            is_and = isinstance(node.op, ast.And)
            combine = np.logical_and if is_and else np.logical_or
            out = self.ev(node.values[0], scope)
            for v in node.values[1:]:
                if np.ndim(out) == 0 and bool(out) != is_and:  # one value decides: x is not None and x > 0
                    return np.bool_(not is_and)
                out = combine(out, self.ev(v, scope))
            return out
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            v = self.ev(node.operand, scope)
            return np.logical_not(v) if isinstance(v, (np.ndarray, np.generic, int, float)) else not v
        if isinstance(node, ast.IfExp):
            test = self.ev(node.test, scope)
            if np.ndim(test) == 0:  # one yes/no: only the chosen side runs
                return self.ev(node.body if bool(test) else node.orelse, scope)
            return np.where(test, self.ev(node.body, scope), self.ev(node.orelse, scope))
        if isinstance(node, ast.Name):
            return self.lookup(node.id, scope)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return self.binop(node.op, self.ev(node.left, scope), self.ev(node.right, scope))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](self.ev(node.operand, scope))
        if isinstance(node, ast.Call):
            return self.call(node, scope)
        if isinstance(node, ast.Attribute) and node.attr == "T" and not _is_np(node):
            return transpose(self.ev(node.value, scope))  # a.T
        if _is_np(node):
            if node.attr in _NP_CONSTS:
                return _NP_CONSTS[node.attr]
            raise ValueError(f"np.{node.attr} isn't available in formulas")
        if isinstance(node, ast.Attribute):
            obj = self.ev(node.value, scope)
            if node.attr in _ATTRS and isinstance(obj, (np.ndarray, np.generic)):
                return getattr(obj, node.attr)
            raise ValueError(f"'.{node.attr}' isn't available (a.shape, a.size, a.ndim and a.T are)")
        if isinstance(node, ast.Subscript):  # x[0], x[-3:], x[x > 0], m[:, -1], d["key"]
            obj, idx = self.ev(node.value, scope), self.index(node.slice, scope)
            if isinstance(obj, (dict, str)):
                return obj[idx]
            if isinstance(obj, (list, tuple)) and isinstance(idx, (int, np.integer)):
                return obj[idx]
            try:
                return tidy(np.asarray(obj)[idx])
            except ValueError:
                if isinstance(obj, (list, tuple)) and isinstance(idx, slice):
                    return obj[idx]  # a list of lists of different lengths, texts…
                raise
        if isinstance(node, ast.Slice):
            return slice(*(None if v is None else int(self.ev(v, scope)) for v in (node.lower, node.upper, node.step)))
        if isinstance(node, (ast.Tuple, ast.List)):
            out = []
            for v in node.elts:
                if isinstance(v, ast.Starred):
                    out.extend(self.iterate(self.ev(v.value, scope)))
                else:
                    out.append(self.ev(v, scope))
            return out
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                raise ValueError("**dict isn't available")
            return {self.ev(k, scope): self.ev(v, scope) for k, v in zip(node.keys, node.values)}
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.DictComp)):
            return self.comprehension(node, scope)
        if isinstance(node, ast.Lambda):
            return _Func(self, "lambda", node.args, node.body, scope, lam=True)
        if isinstance(node, ast.JoinedStr):  # f"{peak:.1f} MPa"
            parts = []
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    x = self.ev(v.value, scope)
                    x = repr(x) if v.conversion == ord("r") else str(x) if v.conversion == ord("s") else x
                    spec = "" if v.format_spec is None else self.ev(v.format_spec, scope)
                    if isinstance(x, np.ndarray) and x.ndim == 0:
                        x = x.item()
                    parts.append(format(x, spec))
                else:
                    parts.append(str(v.value))
            return "".join(parts)
        if self.script:
            raise ValueError(f"'{ast.unparse(node)}' isn't available in scripts")
        raise ValueError("Only numbers, + - * / ** %, comparisons (< > == …), and/or/not, 'a if c else b', "
                         "indexing like x[x > 0], and maths functions (sqrt(), where(), np.percentile(), …) "
                         "are allowed")


class _Lined(ValueError):
    """An error already labelled with its script line."""


def _load(target):
    """An assignment target (x, x[0]) as something to read."""
    import copy
    node = copy.deepcopy(target)
    for n in ast.walk(node):
        if hasattr(n, "ctx"):
            n.ctx = ast.Load()
    return node


def _kind(v) -> str:
    if isinstance(v, (bool, np.bool_)):
        return "yes/no"
    if isinstance(v, (int, float, np.number)):
        return "number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, np.ndarray):
        return f"array {'×'.join(map(str, v.shape))}" if v.ndim else "number"
    if isinstance(v, (list, tuple)):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "nothing (None)" if v is None else type(v).__name__


# What a formula can use, for the editor's autocomplete: always this module's own whitelist.
_DOCS = {
    "a": "input a (b, c, d likewise); a field, curve, list, matrix or number",
    "t": "the x values (times) of the first curve, or the frame times of a list of fields",
    "where": "where(cond, p, q): p where cond is true, else q",
    "len": "number of items", "min": "min(list): the smallest; min(a, b): the smaller, element by element",
    "int": "whole number, cut toward zero: int(2.7) = 2 (element by element; texts like '355' too)",
    "float": "a number: float('2.5e3') = 2500 (element by element on lists, fields, table columns)",
    "max": "max(list): the largest; max(a, b): the larger, element by element",
    "round": "round to n decimals: round(a, 2)",
    "range": "range(n): 0, 1, … n-1, to count in a for loop",
    "enumerate": "for i, v in enumerate(x): each item with its position",
    "zip": "for p, q in zip(a, b): items of two lists side by side",
    "sorted": "a sorted copy of a list", "reversed": "a list backwards", "list": "make a list",
    "dict": "make a dict (names to values)", "str": "a value as text", "bool": "a value as yes/no",
    "pi": "3.14159…", "e": "2.71828…", "inf": "infinity", "nan": "not a number (missing value)",
    "norm": "length of a vector (np.linalg.norm)", "transpose": "transpose a matrix (each one, for a stack)",
    "trace": "sum of the diagonal (of each matrix, for a stack)", "linalg_norm": "length of a vector",
}


def _first_line(f) -> str:
    """The summary line of a NumPy docstring (after its signature)."""
    for line in (getattr(f, "__doc__", None) or "").strip().splitlines():
        line = line.strip()
        if line and not re.match(r"^[\w.]+\(.*\)$", line) and not line.startswith(("Returns", "-")):
            return line.rstrip(".")
    return ""


def formula_names() -> list[dict]:
    """[{name, kind, doc}] for every input, function and constant a formula accepts (kind: input/function/const)."""
    out = [{"name": "a", "kind": "input", "doc": _DOCS["a"]}]
    out += [{"name": n, "kind": "input", "doc": f"input {n}"} for n in "bcd"]
    out.append({"name": "t", "kind": "input", "doc": _DOCS["t"]})
    for n, f in sorted(_FUNCS.items()):
        out.append({"name": n, "kind": "function", "doc": _DOCS.get(n) or _first_line(f)})
    for n in ("pi", "e"):
        out.append({"name": n, "kind": "const", "doc": _DOCS[n]})
    for n, f in sorted(_NP_FUNCS.items()):
        custom = n in ("where", "norm", "transpose", "trace")  # ours (lambdas), not NumPy's docstring
        out.append({"name": f"np.{n}", "kind": "function", "doc": _DOCS[n] if custom else _first_line(f)})
    for n, f in sorted(_LINALG.items()):
        out.append({"name": f"np.linalg.{n}", "kind": "function", "doc": _first_line(f)})
    for n in sorted(_NP_CONSTS):
        out.append({"name": f"np.{n}", "kind": "const", "doc": _DOCS[n]})
    return out
