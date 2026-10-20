"""Stand-in for Abaqus' section module (tests only): SectionLayer, the plies of a composite section."""

from abaqus import LOG, Recorder, _show


def SectionLayer(**kwargs):
    LOG.append({"call": "section.SectionLayer", "args": [], "kwargs": _show(kwargs)})
    return Recorder("SectionLayer(%s)" % kwargs.get("plyName", ""))
