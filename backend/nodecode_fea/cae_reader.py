"""Read an Abaqus/CAE database (.cae) or input file (.inp) into plain JSON, for ``cae_import`` to turn into an editor
graph.

``read_cae`` runs ``READER`` in ``abaqus cae noGUI`` (the launcher from ABAQUS_CMD, else the usual places): it opens
the .cae with openMdb and writes what the modelling nodes can rebuild (materials, sections and their assignments,
parts and how they were made, sets and surfaces as points on their faces/edges/vertices/cells, instances and where
they are, steps, output requests, loads and boundary conditions, interactions and constraints, mesh settings, jobs)
plus what it found but can't describe, so the mapper can say what it skipped. It only reads: the .cae isn't saved.
An input file is imported with ModelFromInputFile (as File > Import > Model does); its parts are only meshes, and
sets and surfaces that hold no geometry are written as their node, element and element-face labels.
Parts whose shape it can't describe as a sketch are written next to the dump as ACIS (.sat) files, and parts that
are only a mesh as an input file (.inp), so the graph imports them with Part instead of guessing.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .sources.abaqus import abaqus_command

# Runs inside Abaqus/CAE (Python 2 or 3): reads request.json, writes dump.json in its working directory.
READER = r'''
from __future__ import print_function
import json, math, os, sys, traceback
from abaqus import *
from abaqusConstants import *
from caeModules import *  # (shadows builtins such as sum) output requests, interactions... only exist once their modules are loaded

request = json.load(open('request.json'))
dump = {'file': request['cae'], 'models': [], 'notes': []}
INPUT_FILE = request['cae'] if request['cae'].lower().endswith('.inp') else ''


def text(v):
    return None if v is None else str(v)


def safe(obj, name):
    """getattr that gives None for whatever CAE refuses to tell (some states raise for the Initial step)."""
    try:
        return getattr(obj, name)
    except Exception:
        return None


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def points_of(seq, instance=False):
    """[[instance name or '', [x, y, z]], ...]: a point on each entity, to find it again (findAt) after a rebuild."""
    found = []
    for item in seq:
        try:
            p = item.pointOn[0]
        except Exception:
            continue
        found.append([getattr(item, 'instanceName', '') or '' if instance else '', [float(c) for c in p]])
    return found


def region_items(region, instance=False):
    """The geometry a set or surface holds: {kind: [[instance, point], ...]}, plus what it has that isn't geometry."""
    out = {}
    for kind, repo in (('cell', 'cells'), ('face', 'faces'), ('edge', 'edges'), ('vertex', 'vertices')):
        try:
            seq = getattr(region, repo)
        except Exception:
            continue
        if seq is not None and len(seq):
            out[kind] = points_of(seq, instance)
    extra, counts, rps, mesh = [], {}, [], {}
    for repo in ('elements', 'nodes', 'referencePoints'):
        try:
            seq = getattr(region, repo)
            if seq is not None and len(seq):
                counts[repo] = len(seq)
                if not out and repo != 'referencePoints':  # only a mesh: its labels (by instance on the assembly)
                    mesh[repo] = [[getattr(x, 'instanceName', '') or '' if instance else '', x.label] for x in seq]
                elif not out:
                    extra.append(repo)
                if repo == 'referencePoints':
                    for rp in seq:
                        try:
                            rps.append([float(c) for c in rp.pointOn[0]])
                        except Exception:
                            pass
        except Exception:
            pass
    sides = []
    try:
        sides = [str(s) for s in region.sides]
    except Exception:
        pass
    if mesh.get('elements') and len(sides) == len(mesh['elements']):  # an element surface: each element's face
        mesh['faces'] = [e + [side] for e, side in zip(mesh['elements'], sides)]
        sides = sorted(set(sides))
    return {'items': out, 'other': extra, 'sides': sides, 'counts': counts, 'reference_points': rps, 'mesh': mesh}


def find_region(owner, name):
    """A set or surface of a part or the assembly by name, internal ones included: (repository kind, object)."""
    for kind, repos in (('set', ('sets', 'allInternalSets')), ('surface', ('surfaces', 'allInternalSurfaces'))):
        for repo in repos:
            try:
                bag = getattr(owner, repo)
                if name in bag.keys():
                    return kind, bag[name], repo.startswith('allInternal')
            except Exception:
                pass
    return None, None, False


def region_ref(value, model):
    """What a load, BC, assignment or interaction acts on: CAE gives (set or surface name, owner, ...)."""
    try:
        name, owner = value[0], value[1]
    except Exception:
        return None
    if not name:
        return None
    a = model.rootAssembly
    if owner == 'Assembly' or owner is None:
        obj, where = a, 'assembly'
    elif owner in model.parts.keys():
        obj, where = model.parts[owner], 'part'
    else:
        return {'name': name, 'owner': str(owner), 'missing': True}
    kind, region, internal = find_region(obj, name)
    if region is None:
        return {'name': name, 'owner': where, 'missing': True}
    out = region_items(region, instance=(where == 'assembly'))
    out.update({'name': name, 'owner': where, 'part': owner if where == 'part' else '', 'kind': kind,
                'internal': bool(internal or name.startswith('_'))})
    return out


def sketch_curves(feature, model):
    """The base feature's sketch as curves (in the sketch plane), or None when it can't be read."""
    name = '__nocode_read__'
    try:
        s = model.ConstrainedSketch(name=name, objectToCopy=feature.sketch)
    except Exception:
        return None
    curves = []
    try:
        for g in s.geometry.values():
            entry = {'type': str(g.curveType), 'construction': str(g.type) == 'CONSTRUCTION'}
            try:
                entry['points'] = [[float(c) for c in v.coords] for v in g.getVertices()]
            except Exception:
                entry['points'] = []
            entry['on'] = [float(c) for c in g.pointOn]
            curves.append(entry)
    finally:
        del model.sketches[name]
    return curves


BASES = {'Solid extrude': ('Solid', 'Extrude'), 'Solid revolve': ('Solid', 'Revolve'),
         'Shell planar': ('Shell', 'Planar'), 'Shell extrude': ('Shell', 'Extrude'),
         'Shell revolve': ('Shell', 'Revolve'), 'Wire planar': ('Wire', 'Planar')}


def read_part(p, model, folder):
    entry = {'name': p.name, 'space': str(p.space), 'type': str(p.type), 'features': list(p.features.keys()),
             'cells': len(p.cells), 'faces': len(p.faces), 'edges': len(p.edges),
             'vertices': [[float(c) for c in v.pointOn[0]] for v in p.vertices], 'elements': len(p.elements),
             'sets': [], 'surfaces': [], 'sections': [], 'beam_n1': [], 'orientations': 0, 'mesh': None,
             'reference_points': len(p.referencePoints)}
    entry['body'] = 'Solid' if len(p.cells) else 'Shell' if len(p.faces) else 'Wire' if len(p.edges) else ''
    keys = list(p.features.keys())
    if keys:
        first = p.features[keys[0]]
        kind = keys[0].rsplit('-', 1)[0]
        if kind in BASES:
            base = {'feature': keys[0], 'body': BASES[kind][0], 'method': BASES[kind][1],
                    'depth': num(getattr(first, 'depth', None)), 'angle': num(getattr(first, 'angle', None)),
                    'flip': text(getattr(first, 'flipRevolveDirection', None)),
                    'curves': sketch_curves(first, model)}
            entry['base'] = base
    if not len(p.faces) and not len(p.edges) and len(p.elements) and INPUT_FILE:  # a part of the input file read
        entry['body'] = ''
        entry['inp'] = INPUT_FILE
        entry['element_types'] = sorted(set(str(e.type) for e in p.elements))
    elif not len(p.faces) and not len(p.edges) and len(p.elements):  # only a mesh: written as an input file
        entry['body'] = ''
        try:
            scratch = '__nocode_mesh__'
            m2 = mdb.Model(name=scratch)
            m2.Part(name=p.name, objectToCopy=p)
            job = mdb.Job(name='part_' + str(len(os.listdir(folder))), model=scratch)
            here = os.getcwd()
            os.chdir(folder)
            try:
                job.writeInput(consistencyChecking=OFF)
            finally:
                os.chdir(here)
            entry['inp'] = os.path.join(folder, job.name + '.inp')
            del mdb.jobs[job.name]
            del mdb.models[scratch]
            entry['element_types'] = sorted(set(str(e.type) for e in p.elements))
        except Exception as exc:
            entry['inp_error'] = str(exc)
    elif len(p.faces) or len(p.edges):
        path = os.path.join(folder, '%s-%s.sat' % (model.name, p.name))
        try:
            p.writeAcisFile(fileName=path)
            entry['sat'] = path
        except Exception as exc:
            entry['sat_error'] = str(exc)
    for name in p.sets.keys():
        r = region_items(p.sets[name])
        r['name'] = name
        entry['sets'].append(r)
    for name in p.surfaces.keys():
        r = region_items(p.surfaces[name])
        r['name'] = name
        entry['surfaces'].append(r)
    for sa in p.sectionAssignments:
        ref = region_ref(sa.region, model)
        if ref is not None:
            ref['whole'] = (len(ref['items'].get('cell', [])) == len(p.cells) if len(p.cells) else
                            len(ref['items'].get('face', [])) == len(p.faces) if len(p.faces) else
                            len(ref['items'].get('edge', [])) == len(p.edges))
        entry['sections'].append({'section': sa.sectionName, 'region': ref,
                                  'offset': text(getattr(sa, 'offsetType', None))})
    for o in p.beamSectionOrientations:
        entry['beam_n1'].append({'n1': [float(c) for c in o.n1], 'region': region_ref(o.region, model)})
    entry['orientations'] = len(p.materialOrientations)
    mesh = {}
    for attr, key in ((SIZE, 'size'), (DEVIATION_FACTOR, 'deviation'), (MIN_SIZE_FACTOR, 'minimum')):
        try:
            mesh[key] = num(p.getPartSeeds(attribute=attr))
        except Exception:
            pass
    controls = p.cells if len(p.cells) else p.faces if len(p.faces) else []
    for attr, key in ((TECHNIQUE, 'technique'), (ELEM_SHAPE, 'shape')):
        seen = set()
        for c in controls:
            try:
                seen.add(str(p.getMeshControl(region=c, attribute=attr)))
            except Exception:
                pass
        mesh[key] = sorted(seen)
    mesh['element_types'] = sorted(set(str(e.type) for e in p.elements)) if len(p.elements) else []
    if any(v for v in mesh.values()):
        entry['mesh'] = mesh
    return entry


def read_material(mat):
    entry = {'name': mat.name, 'behaviours': []}
    for attr in dir(mat):
        if attr[:1].islower() and attr not in ('name', 'description', 'materialIdentifier', 'setValues'):
            try:
                if getattr(mat, attr) is not None:
                    entry['behaviours'].append(attr)
            except Exception:
                pass
    def table_of(b):
        return [[float(c) for c in row] for row in b.table]
    if 'elastic' in entry['behaviours']:
        e = mat.elastic
        entry['elastic'] = {'type': str(e.type), 'table': table_of(e), 'temperature': str(e.temperatureDependency) == 'ON',
                            'moduli': text(getattr(e, 'moduli', None)), 'noCompression': str(getattr(e, 'noCompression', OFF)) == 'ON'}
    if 'plastic' in entry['behaviours']:
        b = mat.plastic
        entry['plastic'] = {'hardening': str(b.hardening), 'table': table_of(b),
                            'temperature': str(b.temperatureDependency) == 'ON',
                            'rate': str(getattr(b, 'rate', OFF)) == 'ON'}
    if 'density' in entry['behaviours']:
        entry['density'] = {'table': table_of(mat.density), 'temperature': str(mat.density.temperatureDependency) == 'ON'}
    if 'damping' in entry['behaviours']:
        d = mat.damping
        entry['damping'] = {'alpha': num(d.alpha), 'beta': num(d.beta), 'composite': num(getattr(d, 'composite', 0)),
                            'structural': num(getattr(d, 'structural', 0))}
    if 'hyperelastic' in entry['behaviours']:
        h = mat.hyperelastic
        entry['hyperelastic'] = {'type': str(h.type), 'testData': str(h.testData) == 'ON', 'table': table_of(h),
                                 'temperature': str(h.temperatureDependency) == 'ON',
                                 'materialType': str(h.materialType)}
    return entry


def read_section(sec):
    entry = {'name': sec.name, 'type': type(sec).__name__, 'material': getattr(sec, 'material', None)}
    for attr in ('thickness', 'thicknessType', 'idealization', 'symmetric', 'poissonRatio', 'integration',
                 'nodalThicknessField', 'thicknessField', 'useDensity', 'temperatureVar', 'preIntegrate',
                 'profile', 'poisson'):
        try:
            v = getattr(sec, attr)
        except Exception:
            continue
        entry[attr] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else text(v)
    try:
        entry['layup'] = [{'thickness': float(ply.thickness), 'angle': float(ply.orientAngle),
                           'material': ply.material, 'orientation': text(ply.orientationType)}
                          for ply in sec.layup]
    except Exception:
        pass
    return entry


def read_profile(prof):
    entry = {'name': prof.name, 'type': type(prof).__name__}
    for attr in ('a', 'b', 'r'):
        v = getattr(prof, attr, None)
        if v is not None:
            entry[attr] = float(v)
    return entry


def instance_pose(inst, part):
    """Rotation matrix and translation taking the part's coordinates to the instance's: x' = R x + t."""
    point, axis, angle = inst.getRotation()
    n = math.sqrt(axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2) or 1.0
    ux, uy, uz = [c / n for c in axis]
    a = math.radians(angle)
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    R = [[c + ux * ux * C, ux * uy * C - uz * s, ux * uz * C + uy * s],
         [uy * ux * C + uz * s, c + uy * uy * C, uy * uz * C - ux * s],
         [uz * ux * C - uy * s, uz * uy * C + ux * s, c + uz * uz * C]]
    try:  # exact: where a vertex of the part ended up
        v0, v1 = part.vertices[0].pointOn[0], inst.vertices[0].pointOn[0]
        t = [v1[i] - (R[i][0] * v0[0] + R[i][1] * v0[1] + R[i][2] * v0[2]) for i in range(3)]
    except Exception:  # no vertices: CAE's translation, then the rotation about its axis point
        tr = inst.getTranslation()
        d = [tr[i] - point[i] for i in range(3)]
        t = [R[i][0] * d[0] + R[i][1] * d[1] + R[i][2] * d[2] + point[i] for i in range(3)]
    return R, t


def variables(state):
    v = safe(state, 'variables')
    return text(v) if v is None or isinstance(v, str) or not hasattr(v, '__iter__') else [str(x) for x in v]


def state_of(step, repo, name):
    try:
        return getattr(step, repo)[name]
    except Exception:
        return None


def state_values(state, keys):
    out = {}
    for k in keys:
        if not hasattr(type(state), k) and k not in dir(state):
            continue
        v = safe(state, k)
        flag = safe(state, k + 'State')
        if flag is not None and str(flag) == 'UNSET':
            out[k] = None
        elif v is None or isinstance(v, (int, float)):
            out[k] = v
        else:
            out[k] = text(v)
    return out


def read_condition(obj, model, repo, keys):
    entry = {'name': obj.name, 'type': type(obj).__name__, 'states': []}
    try:
        entry['region'] = region_ref(obj.region, model)
    except Exception:
        entry['region'] = None
    for attr in ('distributionType', 'localCsys', 'fixed', 'category'):
        try:
            entry[attr] = text(getattr(obj, attr))
        except Exception:
            pass
    for sname in model.steps.keys():
        st = state_of(model.steps[sname], repo, obj.name)
        if st is None:
            continue
        values = state_values(st, keys + ['amplitude', 'boltMethod'])
        entry['states'].append({'step': sname, 'status': text(safe(st, 'status')), 'values': values})
    return entry


def read_model(m, folder):
    out = {'name': m.name, 'parts': [], 'materials': [], 'sections': [], 'profiles': [], 'instances': [],
           'assembly': {'sets': [], 'surfaces': [], 'features': [], 'reference_points': 0},
           'steps': [], 'amplitudes': [], 'bcs': [], 'loads': [], 'interactions': [], 'constraints': [],
           'interaction_properties': [], 'field_outputs': [], 'history_outputs': [], 'other': {}}
    for name in m.parts.keys():
        try:
            out['parts'].append(read_part(m.parts[name], m, folder))
        except Exception:
            out['parts'].append({'name': name, 'error': traceback.format_exc()})
    for name in m.materials.keys():
        out['materials'].append(read_material(m.materials[name]))
    for name in m.sections.keys():
        out['sections'].append(read_section(m.sections[name]))
    for name in m.profiles.keys():
        out['profiles'].append(read_profile(m.profiles[name]))
    a = m.rootAssembly
    for name in a.instances.keys():
        inst = a.instances[name]
        entry = {'name': name, 'part': inst.partName, 'dependent': str(inst.dependent) in ('ON', '1', 'True'),
                 'elements': len(inst.elements) if not inst.dependent else 0}
        try:
            entry['rotation'], entry['translation'] = instance_pose(inst, m.parts[inst.partName])
        except Exception:
            entry['pose_error'] = traceback.format_exc()
        out['instances'].append(entry)
    for name in a.sets.keys():
        r = region_items(a.sets[name], instance=True)
        r['name'] = name
        out['assembly']['sets'].append(r)
    for name in a.surfaces.keys():
        r = region_items(a.surfaces[name], instance=True)
        r['name'] = name
        out['assembly']['surfaces'].append(r)
    out['assembly']['features'] = [k for k in a.features.keys() if k not in a.instances.keys()]
    out['assembly']['reference_points'] = len(a.referencePoints)
    names = list(m.steps.keys())
    for name in names:
        st = m.steps[name]
        entry = {'name': name, 'type': type(st).__name__, 'previous': getattr(st, 'previous', None)}
        for attr in ('timePeriod', 'nlgeom', 'initialInc', 'maxInc', 'minInc', 'maxNumInc', 'numEigen',
                     'timeIncrementationMethod', 'perturbation', 'eigensolver', 'stabilizationMethod',
                     'amplitude', 'alpha', 'application', 'scaleFactor'):
            try:
                v = getattr(st, attr)
            except Exception:
                continue
            entry[attr] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else text(v)
        try:  # Explicit mass scaling definitions
            entry['massScaling'] = [{'objective': text(ms.objective), 'type': text(ms.type), 'dt': ms.dt,
                                     'occurs': text(ms.occurs), 'region': text(ms.region)}
                                    for ms in st.massScaling]
        except Exception:
            pass
        out['steps'].append(entry)
    for name in m.amplitudes.keys():
        amp = m.amplitudes[name]
        entry = {'name': name, 'type': type(amp).__name__, 'timeSpan': text(getattr(amp, 'timeSpan', None))}
        try:
            entry['data'] = [[float(c) for c in row] if isinstance(row, (tuple, list)) else float(row)
                             for row in amp.data]
        except Exception:
            pass
        for attr in ('smooth', 'fixedInterval', 'begin', 'frequency', 'start', 'a_0', 'initial', 'magnitude',
                     'maximum', 'minimum', 'decayTime', 'frequency1', 'frequency2', 'numVariables'):
            try:
                v = getattr(amp, attr)
            except Exception:
                continue
            entry[attr] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else text(v)
        out['amplitudes'].append(entry)
    for name in m.boundaryConditions.keys():
        out['bcs'].append(read_condition(m.boundaryConditions[name], m, 'boundaryConditionStates',
                                         ['u1', 'u2', 'u3', 'ur1', 'ur2', 'ur3', 'typeName']))
    for name in m.loads.keys():
        out['loads'].append(read_condition(m.loads[name], m, 'loadStates',
                                           ['magnitude', 'cf1', 'cf2', 'cf3', 'comp1', 'comp2', 'comp3']))
    for name in m.interactionProperties.keys():
        prop = m.interactionProperties[name]
        entry = {'name': name, 'type': type(prop).__name__, 'behaviours': []}
        for attr in ('tangentialBehavior', 'normalBehavior', 'damping', 'cohesiveBehavior', 'damage',
                     'thermalConductance', 'heatGeneration', 'radiation', 'fractureCriterion', 'geometricProperties'):
            try:
                b = getattr(prop, attr)
            except Exception:
                continue
            if b is None:
                continue
            entry['behaviours'].append(attr)
            if attr == 'tangentialBehavior':
                entry['friction'] = {'formulation': text(b.formulation),
                                     'table': [[float(c) for c in row] for row in (b.table or ())]}
            if attr == 'normalBehavior':
                entry['normal'] = {'pressureOverclosure': text(getattr(b, 'pressureOverclosure', None)),
                                   'allowSeparation': text(getattr(b, 'allowSeparation', None))}
        out['interaction_properties'].append(entry)
    for name in m.interactions.keys():
        it = m.interactions[name]
        entry = {'name': name, 'type': type(it).__name__}
        for attr in ('createStepName', 'sliding', 'adjustMethod', 'interactionProperty', 'contactTracking',
                     'enforcement', 'thickness'):
            try:
                entry[attr] = text(getattr(it, attr))
            except Exception:
                pass
        for attr in ('main', 'secondary'):
            try:
                entry[attr] = region_ref(getattr(it, attr), m)
            except Exception:
                pass
        for sname in names:
            st = state_of(m.steps[sname], 'interactionStates', name)
            if st is not None and str(safe(st, 'status')) == 'CREATED':
                entry['step'] = sname
                entry['property'] = text(getattr(st, 'interactionProperty', None))
                for attr in ('sliding', 'adjustMethod'):
                    v = getattr(st, attr, None)
                    if v is not None:
                        entry[attr] = text(v)
        try:  # general contact: the property assigned to everything
            entry['global_property'] = text(it.contactPropertyAssignments[0][2])
        except Exception:
            pass
        try:
            entry['included'] = text(it.includedPairs.useAllstar)
        except Exception:
            pass
        out['interactions'].append(entry)
    for name in m.constraints.keys():
        c = m.constraints[name]
        entry = {'name': name, 'type': type(c).__name__}
        for attr in ('main', 'secondary', 'surface', 'controlPoint', 'refPoint'):
            try:
                v = getattr(c, attr)
            except Exception:
                continue
            ref = region_ref(v, m)
            if ref is not None:
                entry[attr] = ref
        for attr in ('adjust', 'tieRotations', 'positionToleranceMethod', 'couplingType', 'influenceRadius',
                     'u1', 'u2', 'u3', 'ur1', 'ur2', 'ur3', 'weightingMethod', 'thickness'):
            try:
                entry[attr] = text(getattr(c, attr))
            except Exception:
                pass
        out['constraints'].append(entry)
    for name in m.fieldOutputRequests.keys():
        fo = m.fieldOutputRequests[name]
        entry = {'name': name, 'region': text(getattr(fo, 'region', None)), 'states': []}
        for sname in names:
            st = state_of(m.steps[sname], 'fieldOutputRequestStates', name)
            if st is None:
                continue
            entry['states'].append({'step': sname, 'status': text(safe(st, 'status')),
                                    'variables': variables(st),
                                    'frequency': num(safe(st, 'frequency')),
                                    'numIntervals': num(safe(st, 'numIntervals')),
                                    'frequencyType': text(safe(st, 'frequencyType'))})
        out['field_outputs'].append(entry)
    for name in m.historyOutputRequests.keys():
        ho = m.historyOutputRequests[name]
        entry = {'name': name, 'region': text(getattr(ho, 'region', None)), 'states': []}
        for sname in names:
            st = state_of(m.steps[sname], 'historyOutputRequestStates', name)
            if st is None:
                continue
            entry['states'].append({'step': sname, 'status': text(safe(st, 'status')),
                                    'variables': variables(st),
                                    'frequency': num(safe(st, 'frequency'))})
        out['history_outputs'].append(entry)
    for repo in ('predefinedFields', 'remeshingRules', 'adaptiveMeshControls', 'filters', 'optimizationTasks',
                 'calibrations', 'contactControls', 'fieldOutputRequests', 'historyOutputRequests'):
        try:
            bag = getattr(m, repo)
            names_in = list(bag.keys())
        except Exception:
            continue
        if names_in and repo not in ('fieldOutputRequests', 'historyOutputRequests'):
            out['other'][repo] = names_in
    return out


def model_name(path):
    """A CAE model name for an input file: its name in letters, digits, - and _, starting with a letter."""
    stem = os.path.splitext(os.path.basename(path))[0]
    name = ''.join(c if c.isalnum() or c in '-_' else '-' for c in stem).strip('-_')[:38]
    return name if name[:1].isalpha() else 'Model-' + name


try:
    try:
        if INPUT_FILE:  # as CAE imports one (File > Import > Model): a model named after the file
            request['model'] = request.get('model') or model_name(INPUT_FILE)
            mdb.ModelFromInputFile(name=request['model'], inputFileName=INPUT_FILE)
        else:
            openMdb(pathName=request['cae'])
    except Exception as exc:  # saved by an older release: read an upgraded copy (the file itself stays as it is)
        if INPUT_FILE:
            raise
        if 'incompatible release' not in str(exc) or 'expected' not in str(exc):
            raise
        import shutil
        shutil.copyfile(request['cae'], 'original.cae')
        try:  # a copy here, by plain names without .cae, by position (CAE 2026 refuses paths and keywords)
            upgradeMdb('original', 'upgraded')
        except Exception as why:
            raise Exception('%s: %s; upgrading it failed: %s (a file from a newer Abaqus can only be read by that '
                            'release or later)' % (os.path.basename(request['cae']), exc, why))
        openMdb(pathName='upgraded.cae')
        dump['notes'].append('Read an upgraded copy: %s' % str(exc).strip())
    dump['models'] = list(mdb.models.keys())
    wanted = request.get('model') or ''
    if not wanted:  # the only model, else the only one with something in it (a new file's Model-1 is empty)
        used = [k for k in dump['models'] if len(mdb.models[k].parts) or len(mdb.models[k].materials)]
        wanted = (dump['models'] if len(dump['models']) == 1 else used if len(used) == 1 else [''])[0]
    if wanted and wanted not in dump['models']:
        dump['error'] = 'The file has no model %s (it has: %s)' % (wanted, ', '.join(dump['models']))
    elif wanted:
        folder = request['folder']
        if not os.path.isdir(folder):
            os.makedirs(folder)
        dump['model'] = read_model(mdb.models[wanted], folder)
        dump['jobs'] = []
        for name in mdb.jobs.keys():
            j = mdb.jobs[name]
            if getattr(j, 'model', None) == wanted:
                dump['jobs'].append({'name': name, 'cpus': int(getattr(j, 'numCpus', 1) or 1),
                                     'memory': int(getattr(j, 'memory', 90) or 90),
                                     'memoryUnits': text(getattr(j, 'memoryUnits', None)),
                                     'type': text(getattr(j, 'type', None))})
        if INPUT_FILE and not dump['jobs']:  # an input file is one analysis: a job to run it again
            dump['jobs'].append({'name': wanted, 'cpus': 1, 'memory': 90, 'memoryUnits': 'PERCENTAGE',
                                 'type': 'ANALYSIS'})
except Exception:
    dump['error'] = traceback.format_exc()
json.dump(dump, open('dump.json', 'w'), indent=1)
'''


class ReadError(Exception):
    pass


def import_folder(cae: str, model: str = "") -> Path:
    """Where a .cae's parts are written as geometry/mesh files: beside the app's jobs, one folder per file."""
    stem = Path(cae).stem or "model"
    return Path.home() / "nodecode-fea" / "cae-import" / stem / (model or "model")


def read_cae(cae: str, model: str = "", launcher: str | None = None, timeout: int = 600,
             folder: str | None = None) -> dict:
    """The dump of one model of a .cae (or, when it has several and none is named, just their names), or of an
    Abaqus input file (.inp) imported as CAE imports one. Parts written out as files go to ``folder`` (default:
    ``import_folder``); an input file's parts are imported from the file itself."""
    cae = os.path.abspath(os.path.expanduser(cae))
    if not cae.lower().endswith((".cae", ".inp")):
        raise ReadError("Choose an Abaqus/CAE model database (.cae) or an Abaqus input file (.inp)")
    if not os.path.isfile(cae):
        raise ReadError(f"File not found: {cae}")
    with tempfile.TemporaryDirectory(prefix="nocode_cae_read_") as tmp:
        (Path(tmp) / "request.json").write_text(json.dumps(
            {"cae": cae, "model": model, "folder": folder or str(import_folder(cae, model or "model"))}))
        (Path(tmp) / "reader.py").write_text(READER, encoding="ascii")
        try:
            proc = subprocess.run([launcher or abaqus_command(), "cae", "noGUI=reader.py"], cwd=tmp,
                                  capture_output=True, text=True, errors="replace", timeout=timeout)
        except FileNotFoundError as exc:
            raise ReadError("Abaqus wasn't found: set ABAQUS_CMD to the Abaqus launcher (e.g. "
                            "C:/SIMULIA/Commands/abaqus.bat)") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReadError(f"Abaqus/CAE took more than {timeout} s to read {os.path.basename(cae)}") from exc
        out = Path(tmp) / "dump.json"
        if not out.exists():
            raise ReadError("Abaqus/CAE couldn't read the file:\n" + (proc.stdout + proc.stderr)[-2000:])
        dump = json.loads(out.read_text())
    if dump.get("error"):
        raise ReadError(dump["error"])
    return dump
