"""Text nodes: format, split and match text."""

from __future__ import annotations

import re
import string

from ..values import Bool, Number, Text
from .core import _fmt, Param, Port, register
from .common import _as_text


def _broadcast(values: dict) -> list[dict]:
    """Inputs where some are lists -> one dict per item (single values repeat)."""
    sizes = {len(v) for v in values.values() if isinstance(v, list)}
    if not sizes:
        return [values]
    if len(sizes) > 1:
        raise ValueError(f"Lists have different lengths ({', '.join(map(str, sorted(sizes)))})")
    n = sizes.pop()
    return [{k: (v[i] if isinstance(v, list) else v) for k, v in values.items()} for i in range(n)]


def _one_or_many(items: list):
    return items[0] if len(items) == 1 else items


@register("text.value", "Text", "Input", "A piece of text you type in: a name, a label, a folder…",
          outputs=[Port("text", "text", "Text")], params=[Param("text", "text", "", "Text")])
def text_value(inputs, params):
    return {"text": Text(str(params["text"]), "Text")}


@register("text.join", "Join Text", "Text",
          "Add texts (and numbers, Yes/No, names) together, in order A, B, C, D, with a separator between them. "
          "With lists, joins item by item.",
          inputs=[Port(k, "any", k.upper(), optional=True) for k in ("a", "b", "c", "d")],
          outputs=[Port("text", "text", "Text")],
          params=[Param("sep", "text", "", "Separator")], lists=True, hidden=True)
def text_join(inputs, params):
    parts = {k: inputs.get(k) for k in ("a", "b", "c", "d") if inputs.get(k) is not None}
    if not parts:
        raise ValueError("Connect at least one input")
    out = [Text(str(params["sep"]).join(_as_text(row[k]) for k in sorted(row)), "Joined") for row in _broadcast(parts)]
    return {"text": _one_or_many(out)}


@register("text.replace", "Replace / Remove", "Text",
          "Take part of a text out (leave Replace with empty), or swap it for something else.",
          inputs=[Port("text", "text", "Text")], outputs=[Port("text", "text", "Text")],
          params=[Param("find", "text", "", "Find"), Param("replace", "text", "", "Replace with"),
                  Param("case", "select", "Match case", "Case", options=["Match case", "Ignore case"])])
def text_replace(inputs, params):
    t, find = inputs["text"].value, str(params["find"])
    if not find:
        return {"text": Text(t, inputs["text"].label)}
    if params["case"] == "Ignore case":
        out = re.sub(re.escape(find), lambda m: str(params["replace"]), t, flags=re.IGNORECASE)
    else:
        out = t.replace(find, str(params["replace"]))
    return {"text": Text(out, inputs["text"].label)}


_FIELD_RE = re.compile(r"\{([^{}]*)\}")


class _TemplateFormatter(string.Formatter):
    """str.format, but plain {a} numbers read like the rest of the editor (311.2, not 311.24000001)."""

    def format_field(self, value, format_spec):
        if isinstance(value, float) and not format_spec:
            return _fmt(value)
        return super().format_field(value, format_spec)


@register("text.format", "Format Text", "Text",
          "Make text from values: join them ('{a}_{b}'), turn a number into text ('{a:.3g}'), or fill a sentence "
          "('Max {a:.1f} MPa at {b}'). Use {a} {b} {c} {d}; numbers can take a format like {a:.2f} or {a:.3e}. "
          "With lists, makes one text per item.",
          inputs=[Port(k, "any", k, optional=True, depth=1) for k in ("a", "b", "c", "d")],
          outputs=[Port("text", "text", "Text")],
          params=[Param("template", "text", "{a}", "Template")], lists=True)
def text_format(inputs, params):
    template = str(params["template"])
    for name in _FIELD_RE.findall(template):
        key = name.split(":", 1)[0].split("!", 1)[0]
        if key not in ("a", "b", "c", "d"):
            raise ValueError(f"Use {{a}}, {{b}}, {{c}} or {{d}} in the template (found {{{name}}})")
    parts = {k: inputs.get(k) for k in ("a", "b", "c", "d")}
    out = []
    for row in _broadcast({k: v for k, v in parts.items() if v is not None} or {"_": None}):
        vals = {k: (row[k].value if isinstance(row.get(k), Number) else _as_text(row.get(k)))
                for k in ("a", "b", "c", "d")}
        try:
            out.append(Text(_TemplateFormatter().format(template, **vals), "Formatted"))
        except (ValueError, KeyError, IndexError) as exc:
            raise ValueError(f"Template problem: {exc}") from exc
    return {"text": out if any(isinstance(v, list) for v in parts.values()) else out[0]}  # a list in, a list out


@register("text.from", "To Text", "Text", "Any value as text (numbers with the chosen significant digits).",
          inputs=[Port("value", "any", "Value")], outputs=[Port("text", "text", "Text")],
          params=[Param("digits", "int", 4, "Significant digits")], lists=True, hidden=True)
def text_from(inputs, params):
    def one(v):
        if isinstance(v, Number):
            return Text(f"{v.value:.{max(1, int(params['digits']))}g}", v.label)
        return Text(_as_text(v), getattr(v, "label", ""))
    v = inputs["value"]
    return {"text": [one(x) for x in v] if isinstance(v, list) else one(v)}


@register("text.to_number", "Text to Number", "Text", "Read a number from text, e.g. '355' or '2.5e3'.",
          inputs=[Port("text", "text", "Text")], outputs=[Port("value", "number", "Number")], hidden=True)
def text_to_number(inputs, params):
    t = inputs["text"].value.strip()
    try:
        return {"value": Number(float(t), t)}
    except ValueError as exc:
        raise ValueError(f"'{t}' isn't a number") from exc


@register("text.split", "Split Text", "Text", "Cut a text into a list of pieces at a separator.",
          inputs=[Port("text", "text", "Text")], outputs=[Port("list", "text", "Pieces")],
          params=[Param("sep", "text", ",", "Separator"), Param("trim", "select", "Trim spaces", "Spaces",
                                                               options=["Trim spaces", "Keep spaces"])])
def text_split(inputs, params):
    sep = str(params["sep"])
    pieces = inputs["text"].value.split(sep) if sep else list(inputs["text"].value)
    if params["trim"] == "Trim spaces":
        pieces = [p.strip() for p in pieces]
    return {"list": [Text(p, f"piece {i}") for i, p in enumerate(pieces)]}


@register("text.test", "Text Test", "Text", "Does a text equal / contain / start with / end with another?",
          inputs=[Port("text", "text", "Text"), Port("other", "text", "Other", optional=True)],
          outputs=[Port("result", "bool", "Yes/No")],
          params=[Param("op", "select", "contains", "Test",
                        options=["equals", "contains", "starts with", "ends with"]),
                  Param("value", "text", "", "Other (if not connected)"),
                  Param("case", "select", "Ignore case", "Case", options=["Ignore case", "Match case"])], hidden=True)
def text_test(inputs, params):
    a = inputs["text"].value
    b = inputs["other"].value if inputs.get("other") is not None else str(params["value"])
    if params["case"] == "Ignore case":
        a, b = a.lower(), b.lower()
    result = {"equals": a == b, "contains": b in a, "starts with": a.startswith(b), "ends with": a.endswith(b)}[
        params["op"]]
    return {"result": Bool(result, f"'{inputs['text'].value}' {params['op']} '{b}'")}
