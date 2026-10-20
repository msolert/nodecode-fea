"""The generated script's runtime helpers (HELPERS) and what every builder shares: fingerprints, number
formatting, build order, and the part as built (final_part)."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from ..values import (AnalysisStep, Condition, DatumPlane, Instance, Interaction, Job, Material, MergedPart, Model,
    OutputRequests, Part, PartOperation, Region, Section, Sketch)


# Helpers for the generated script. Not passed through str.format: braces are literal here.
HELPERS = '''
import os
import sys
import types

# What this update left as another editor tab built it: measuring it would answer for that tab's geometry
_left = []


def _ledgers():
    """The module that keeps, between updates, what earlier ones built in this CAE session: per editor tab (doc),
    [[key, fingerprint], ...] in build order. A restarted CAE starts empty and builds everything again."""
    mod = sys.modules.get('nocode_ledger')
    if mod is None or not hasattr(mod, 'docs'):
        mod = types.ModuleType('nocode_ledger')
        mod.docs = {}
        sys.modules['nocode_ledger'] = mod
    return mod


def _held(key, doc):
    """[(tab, fingerprint)] of the other editor tabs whose last update built the object."""
    return [(d, e[1]) for d, led in list(_ledgers().docs.items()) if d != doc for e in led if e[0] == key]


def _ledger(doc=''):
    return _ledgers().docs.setdefault(doc, [])


def _exists(key):
    """Is the object 'model|M', 'sketch|M|S', 'part|M|P' or 'instance|M|I' in CAE?"""
    k = key.split('|')
    if k[1] not in mdb.models.keys():
        return False
    m = mdb.models[k[1]]
    if k[0] == 'sketch':
        return k[2] in m.sketches.keys()
    if k[0] == 'part':
        return k[2] in m.parts.keys()
    if k[0] == 'instance':
        return k[2] in m.rootAssembly.instances.keys()
    if k[0] == 'material':
        return k[2] in m.materials.keys()
    if k[0] == 'section':
        return k[2] in m.sections.keys()
    if k[0] == 'step':
        return k[2] in m.steps.keys()
    if k[0] == 'condition':
        if k[2] == 'film':
            import interaction
        repo = (m.boundaryConditions if k[2] == 'bc' else m.predefinedFields if k[2] == 'field' else
                m.interactions if k[2] == 'film' else m.loads)
        return k[3] in repo.keys()
    if k[0] == 'datum' and k[2] == 'assembly':
        return k[3] in m.rootAssembly.features.keys()
    if k[0] in ('datum', 'feature'):
        return k[3] in m.parts[k[2]].features.keys()
    if k[0] == 'interaction':
        import interaction  # constraints and interactions appear on models once it is loaded
        if k[2] == 'connector':
            return k[3] + '-Section' in m.sections.keys()
        if k[2] == 'inertia':
            return k[3] in m.rootAssembly.engineeringFeatures.inertias.keys()
        repo = m.constraints if k[2] in ('tie', 'coupling') else m.interactions
        return k[3] in repo.keys()
    if k[0] == 'job':
        return k[2] in mdb.jobs.keys()
    if k[0] == 'output':
        return k[2] in m.steps.keys()
    if k[0] == 'region':
        owner = m.parts[k[3]] if k[2] == 'part' else m.rootAssembly
        repo = owner.sets if k[4] == 'set' else owner.surfaces
        return k[5] in repo.keys()
    return True


def _delete_owned(m, name):
    """What a coupling, connector or general contact made besides itself: points, wire, sets, surfaces, section."""
    a = m.rootAssembly
    for i in reversed(range(len(a.sectionAssignments))):
        if a.sectionAssignments[i].region[0] == name + '-Wire':
            del a.sectionAssignments[i]
    for owned in (name + '-RP', name + '-Wire', name + '-A', name + '-B'):
        if owned in a.sets.keys():
            del a.sets[owned]
        if owned in a.surfaces.keys():
            del a.surfaces[owned]
    for owned in (name + '-Wire', name + '-Csys', name + '-RP', name + '-RP1', name + '-RP2'):
        if owned in a.features.keys():
            a.deleteFeatures((owned,))
    if name + '-Section' in m.sections.keys():
        del m.sections[name + '-Section']


def _delete(key):
    k = key.split('|')
    if k[1] not in mdb.models.keys():
        return
    m = mdb.models[k[1]]
    if k[0] == 'model':
        del mdb.models[k[1]]
    elif k[0] == 'sketch' and k[2] in m.sketches.keys():
        del m.sketches[k[2]]
    elif k[0] == 'part' and k[2] in m.parts.keys():
        a = m.rootAssembly
        for name in list(a.instances.keys()):  # instances of a part go with it
            inst = a.instances[name]
            if getattr(inst, 'partName', None) == k[2] or getattr(getattr(inst, 'part', None), 'name', None) == k[2]:
                del a.instances[name]
        del m.parts[k[2]]
    elif k[0] == 'instance' and k[2] in m.rootAssembly.instances.keys():
        del m.rootAssembly.instances[k[2]]
    elif k[0] == 'material' and k[2] in m.materials.keys():
        del m.materials[k[2]]
    elif k[0] == 'section' and k[2] in m.sections.keys():
        del m.sections[k[2]]
        if k[2] + '-Profile' in m.profiles.keys():
            del m.profiles[k[2] + '-Profile']
    elif k[0] == 'step' and k[2] in m.steps.keys():
        del m.steps[k[2]]
    elif k[0] == 'condition':
        if k[2] == 'film':
            import interaction
        repo = (m.boundaryConditions if k[2] == 'bc' else m.predefinedFields if k[2] == 'field' else
                m.interactions if k[2] == 'film' else m.loads)
        for name in [n for n in repo.keys() if n == k[3] or n.startswith(k[3] + '-from-')]:
            del repo[name]  # itself and its copies turned on again in later steps
        for name, repo in ((k[3] + '-Amp', m.amplitudes), (k[3] + '-Field', m.analyticalFields)):
            if name in repo.keys():  # its own amplitude and field, once nothing uses them
                del repo[name]
        for name in [n for n in m.amplitudes.keys() if n.startswith(k[3] + '-Amp-')]:
            del m.amplitudes[name]  # the amplitudes of later steps
        if k[3] + '-Axis' in m.rootAssembly.features.keys():  # a bolt load's axis and force output
            m.rootAssembly.deleteFeatures((k[3] + '-Axis',))
        if k[3] + '-Force' in m.historyOutputRequests.keys():
            del m.historyOutputRequests[k[3] + '-Force']
    elif k[0] == 'datum' and k[2] == 'assembly' and k[3] in m.rootAssembly.features.keys():
        m.rootAssembly.deleteFeatures((k[3],))
    elif k[0] in ('datum', 'feature') and k[2] in m.parts.keys() and k[3] in m.parts[k[2]].features.keys():
        del m.parts[k[2]].features[k[3]]
        for _name in [n for n in m.parts[k[2]].features.keys() if n.startswith(k[3] + '-step')]:
            del m.parts[k[2]].features[_name]  # the earlier features of a multi-feature operation
    elif k[0] == 'interaction':
        import interaction
        repo = m.constraints if k[2] in ('tie', 'coupling') else m.interactions
        _inertias = m.rootAssembly.engineeringFeatures.inertias
        if k[2] == 'inertia' and k[3] in _inertias.keys():
            del _inertias[k[3]]
        for name in [n for n in repo.keys() if n == k[3] or n.startswith(k[3] + '-from-')]:
            del repo[name]
        for name in [n for n in m.interactionProperties.keys() if n == k[3] + '-Property'
                     or n.startswith(k[3] + '-Property-')]:
            del m.interactionProperties[name]
        _delete_owned(m, k[3])
    elif k[0] == 'region':
        owner = m.parts[k[3]] if k[2] == 'part' else m.rootAssembly
        repo = owner.sets if k[4] == 'set' else owner.surfaces
        if k[5] in repo.keys():
            del repo[k[5]]
    elif k[0] == 'job' and k[2] in mdb.jobs.keys():
        del mdb.jobs[k[2]]
    elif k[0] == 'output' and k[2] in m.steps.keys():  # back to Abaqus' default requests in that step
        import step
        if 'F-Output-1' in m.fieldOutputRequests.keys():
            try:
                m.fieldOutputRequests['F-Output-1'].setValuesInStep(stepName=k[2], variables=PRESELECT, frequency=1)
            except Exception:
                pass
        for name, repo in ((k[2] + '-Field', m.fieldOutputRequests), (k[2] + '-History', m.historyOutputRequests)):
            if name in repo.keys():
                del repo[name]


def _build(objects, keep, broken, doc='', take=True):
    """objects: [(key, fingerprint, [keys it depends on], builder), ...] in build order. Builds only what changed
    and deletes what the graph no longer has, except keep: what nodes with an error (broken) made last time.

    doc: the editor tab this graph is open in. Each tab deletes only what it built; the same object built the same
    way by two tabs (one model open twice) is shared, not rebuilt. An object another tab built differently (two
    models with one name) is rebuilt for this tab only when take (Sync pressed in it): automatic updates (Live edits,
    a running job) leave it to the other tab, so two tabs don't rebuild a model over each other again and again."""
    led = _ledger(doc)
    want = dict((o[0], o[1]) for o in objects)
    removed = 0
    if broken:
        _say('kept as they were until fixed (error in the editor): ' + ', '.join(broken))
    for key, fp in list(reversed(led)):
        if want.get(key) == fp:
            continue
        if key in want or key not in keep:
            led.remove([key, fp])
            if _held(key, doc):  # another tab's now: it stays
                continue
            try:
                _delete(key)
                removed += 1
            except Exception as exc:
                _say('could not delete %s: %s' % (key.replace('|', ' '), exc))
    rebuilt, failed, waiting, taken, kept = [], [], [], [], 0
    for key, fp, deps, builder in objects:
        if [d for d in deps if d in failed]:
            failed.append(key)
            continue
        if [d for d in deps if d in waiting]:
            waiting.append(key)
            continue
        fresh = not [d for d in deps if d in rebuilt]
        if [key, fp] in led and _exists(key) and fresh:
            kept += 1
            continue
        others = _held(key, doc)
        if others and fresh and _exists(key) and all(f == fp for _, f in others):  # built the same: shared
            if [key, fp] not in led:
                led.append([key, fp])
            kept += 1
            continue
        if [o for o in others if o[1] != fp]:
            if not take:
                waiting.append(key)
                continue
            for other, f in others:
                if f != fp:
                    _ledgers().docs[other] = [e for e in _ledger(other) if e[0] != key]
            taken.append(key)
        if [key, fp] in led:
            led.remove([key, fp])
        try:
            if _exists(key):  # made by hand, or left over from a failed update
                _delete(key)
            builder()
            led.append([key, fp])
            rebuilt.append(key)
        except Exception as exc:
            failed.append(key)
            _say('%s failed: %s' % (key.replace('|', ' '), exc))
    if taken:
        _say('another editor tab had built %s differently: rebuilt for this tab (give the models different names to '
             'keep both)' % ', '.join(k.replace('|', ' ') for k in taken[:5]) + (' and %d more' % (len(taken) - 5)
                                                                                  if len(taken) > 5 else ''))
    if waiting:
        _left.extend(waiting)
        _say('left as another editor tab built them: %s (same model name in two tabs: press Sync in this tab to '
             'rebuild them here, or rename the model)' % ', '.join(k.replace('|', ' ') for k in waiting[:5])
             + (' and %d more' % (len(waiting) - 5) if len(waiting) > 5 else ''))
    _say('model: %d built, %d unchanged, %d deleted%s' % (len(rebuilt), kept, removed,
                                                        ', %d failed' % len(failed) if failed else ''))
    return rebuilt
'''

ASSEMBLY, PART, SKETCH = "Assembly", "Part", "Sketch"
ORDER = {Model: 0, Material: 1, Section: 2, Sketch: 3, Part: 4, MergedPart: 8, DatumPlane: 5, PartOperation: 6,
         Instance: 7, Region: 8, AnalysisStep: 9, Condition: 10, Interaction: 11, OutputRequests: 12, Job: 13}


def is_model_node(node: dict) -> bool:
    return str(node.get("type", "")).startswith("model.")


def fingerprint(value) -> str:
    return hashlib.sha1(repr(value).encode()).hexdigest()[:16]


def _n(v: float) -> str:
    return repr(float(f"{float(v):.10g}"))


def _pt(p) -> str:
    return "(" + ", ".join(_n(c) for c in p) + ")"


def _core(p: Part) -> Part:
    """A part without what is done to it (sections, meshes, Part Features) and which list item that was for: what
    makes it that part."""
    return replace(p, features=(), sections=(), operations=(), item=())


def _item_of(part: Part, items: list) -> tuple:
    """The list item a stage resolves to: its own, followed down to the first item of each list run on it later
    (a bare Part meshed over sizes 10, 5 is the first of those meshes, as before)."""
    t = part.item
    while True:
        deeper = [c[:len(t) + 1] for c in items if len(c) > len(t) and c[:len(t)] == t]
        if not deeper:
            return t
        t = min(deeper)


def final_part(part: Part, parts: list) -> Part:
    """The part as built in CAE: everything done to it anywhere in the graph (Material & Section, Mesh, Part Feature,
    in a chain or on side-by-side branches) applies to that one part, in graph order. A Region, Instance or Measure on
    any stage of it is on this part. Separate parts are made on purpose: edits run over a list (one part per item, a
    study) or another part name."""
    if not isinstance(part, Part):
        return part
    core = _core(part)
    family = [q for q in parts if type(q) is type(part) and _core(q) == core]
    if not family or family == [part]:
        return part
    target = _item_of(part, [q.item for q in family])
    # the stages of this item: the shared ones before the list (a prefix of it) and its own
    family = [q for q in family + [part] if q.item == target[:len(q.item)]]
    # one that already has all the others' edits is the part as built (as collected objects hold it)
    for q in family:
        if all(set(o.features) <= set(q.features) and set(o.sections) <= set(q.sections)
               and set(o.operations) <= set(q.operations) for o in family):
            return q
    merged = {"features": [], "sections": [], "operations": []}
    # shorter stages first (a chain then keeps its order), side-by-side branches in graph order
    for q in sorted(family, key=lambda q: len(q.features) + len(q.sections) + len(q.operations)):
        for field, items in merged.items():
            items += [x for x in getattr(q, field) if x not in items]
    return replace(part, item=target, **{field: tuple(items) for field, items in merged.items()})


def _flat(v) -> list:
    return [x for item in v for x in _flat(item)] if isinstance(v, list) else ([] if v is None else [v])


def built(v, objects: list[dict]) -> dict:
    """The collected object that builds v (for an earlier stage of a part, the part it ends up as)."""
    o = next((o for o in objects if o["value"] == v), None)
    if o is None and isinstance(v, Part):
        v = final_part(v, [o["value"] for o in objects])
        o = next((o for o in objects if o["value"] == v), None)
    if o is None and isinstance(v, Part):  # a stage before datums/partitions: the same CAE part
        bare = replace(v, operations=())
        o = next((o for o in objects if isinstance(o["value"], Part)
                  and replace(o["value"], operations=()) == bare), None)
    if o is None:
        raise LookupError(f"{type(v).__name__} {getattr(v, 'name', '')} is not built")
    return o
