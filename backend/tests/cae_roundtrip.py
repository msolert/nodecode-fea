"""Compare a model rebuilt from "Open CAE model" with the one it was read from, inside Abaqus/CAE.

``COMPARE`` runs in ``abaqus cae noGUI`` next to the opened .cae: it runs the graph's build script (rebuild.py), then
writes, for both models, what must match (materials, sections, instances and where their vertices are, steps,
output requests, loads and boundary conditions, constraints, part volumes and mesh sizes) to compare.json.
"""

COMPARE = r'''
from __future__ import print_function
import json, sys, io, traceback
import visualization, part
from abaqus import *
from abaqusConstants import *
from caeModules import *

request = json.load(open('compare_request.json'))
openMdb(pathName=request['cae'])
out, old = io.StringIO() if sys.version_info[0] > 2 else None, sys.stdout
error = None
try:
    if out is not None:
        sys.stdout = out
    exec(compile(open('rebuild.py').read(), 'rebuild.py', 'exec'), {'__name__': '__main__'})
except Exception:
    error = traceback.format_exc()
finally:
    sys.stdout = old


def rounded(p):
    return [round(float(c), 3) for c in p]


def describe(m):
    a = m.rootAssembly
    d = {'materials': {}, 'sections': {}, 'instances': {}, 'steps': {}, 'bcs': {}, 'loads': {}, 'constraints': {},
         'parts': {}, 'field': {}}
    for k in m.materials.keys():
        mat = m.materials[k]
        d['materials'][k] = {'elastic': [list(r) for r in mat.elastic.table] if getattr(mat, 'elastic', None) else None,
                             'plastic': [list(r) for r in mat.plastic.table] if getattr(mat, 'plastic', None) else None,
                             'density': [list(r) for r in mat.density.table] if getattr(mat, 'density', None) else None,
                             'expansion': bool(getattr(mat, 'expansion', None))}
    for k in m.sections.keys():
        s = m.sections[k]
        d['sections'][k] = [type(s).__name__, s.material, getattr(s, 'thickness', None)]
    for k in a.instances.keys():
        i = a.instances[k]
        d['instances'][k] = [i.partName, sorted(rounded(v.pointOn[0]) for v in i.vertices)]
    for k in m.steps.keys():
        s = m.steps[k]
        d['steps'][k] = [type(s).__name__, getattr(s, 'timePeriod', None), str(getattr(s, 'nlgeom', None)),
                         getattr(s, 'numEigen', None)]
    for k in m.boundaryConditions.keys():
        b = m.boundaryConditions[k]
        d['bcs'][k] = [type(b).__name__, len(b.region[0]) and True]
    for k in m.loads.keys():
        l = m.loads[k]
        st = m.steps['Load'].loadStates[k]
        vals = [getattr(st, x, None) for x in ('magnitude', 'cf1', 'cf2', 'cf3', 'comp1', 'comp2', 'comp3')]
        amp = m.amplitudes[st.amplitude].data if st.amplitude in m.amplitudes.keys() else st.amplitude
        d['loads'][k] = [type(l).__name__, [v for v in vals if isinstance(v, float)], str(amp)]
    for k in m.constraints.keys():
        d['constraints'][k] = type(m.constraints[k]).__name__
    for k in m.parts.keys():
        p = m.parts[k]
        try:
            vol = round(p.getVolume(), 2) if len(p.cells) else round(p.getArea(p.faces), 2)
        except Exception:
            vol = None
        d['parts'][k] = [vol, len(p.elements), len(p.sectionAssignments)]
    try:
        d['field'] = {k: str(m.steps['Load'].fieldOutputRequestStates[k].variables)
                      for k in m.fieldOutputRequests.keys()
                      if 'Load' in m.steps.keys() and k in m.steps['Load'].fieldOutputRequestStates.keys()}
    except Exception:
        d['field'] = {'error': traceback.format_exc()}
    return d


report = {'error': error, 'output': out.getvalue() if out is not None else '',
          'models': {k: describe(mdb.models[k]) for k in request['models'] if k in mdb.models.keys()},
          'all_models': list(mdb.models.keys())}
json.dump(report, open('compare.json', 'w'), indent=1, default=str)
'''


def rebuild_and_compare(launcher: str, folder, cae: str, script: str, models: list) -> dict:
    import json
    import subprocess
    from pathlib import Path

    folder = Path(folder)
    (folder / "compare_request.json").write_text(json.dumps({"cae": cae, "models": models}))
    (folder / "rebuild.py").write_text(script, encoding="utf-8")
    (folder / "compare.py").write_text(COMPARE, encoding="ascii")
    proc = subprocess.run([launcher, "cae", "noGUI=compare.py"], cwd=folder, capture_output=True, text=True,
                          errors="replace", timeout=900)
    out = folder / "compare.json"
    assert out.exists(), proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(out.read_text())
