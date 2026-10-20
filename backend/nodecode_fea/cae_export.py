"""Generate an Abaqus/CAE script that recreates a graph's outputs inside a CAE session.

The editor stays the source of truth: curves and values are computed here and
embedded in the script as data. Contour plots are rebuilt with CAE's own
Visualization commands (same ODB, step, frame, variable and invariant), so the
user gets a native, fully interactive CAE viewport.

The generated script avoids f-strings and other Python 3 only syntax, so it also
runs on older (Python 2) Abaqus versions.
"""

from __future__ import annotations

import datetime
import os
import math
import re

import numpy as np

from . import ops
from . import cae_mesh, cae_model, cae_queries
from .nodes import EDGES, REGISTRY, _abaqus_name, _contour_display, _odb_files, _odb_path, entity_name, group_members, summarize
from .mesh import surface_for
from .values import EntitySet, Field, Instance, Number, Odb, Part, PartOperation, PendingMeasure, Region, Series

HEADER = '''# -*- coding: utf-8 -*-
"""NodeCode FEA results, generated {when}.

Run inside Abaqus/CAE: File > Run Script... and pick this file
(or start CAE with:  abaqus cae script={filename}).

What it does:
{contents}
Curves and values were computed by the editor and are stored in this file;
contour plots are rebuilt from the ODB with CAE's own commands.
"""
from __future__ import print_function

import os
import sys

from abaqus import highlight, mdb, session, unhighlight
from abaqusConstants import *
import displayGroupOdbToolset as dgo

_opened = {{}}
_used_viewports = []
# Outputs set to Viewport = Current draw into the viewport that was current when the script started.
_home = session.currentViewportName
_CURRENT_USED = {current_used}


def _say(text):
    print('NodeCode FEA: ' + text)


def _geometry(items, description='Region'):
    """Give an actionable error instead of letting CAE silently create or use an empty geometry region."""
    if not len(items):
        raise ValueError('%s found no geometry; check the type and Find at points or pick again' % description)
    return items


def _region_positions(items, positions, expected, description='Region'):
    """Apply an If node's boolean-mask positions to geometry, preserving mesh-hole groups when present."""
    items = _geometry(items, description)
    count = (len(items.groups) if isinstance(items, _MeshPick) and items.fits and items.fits[0] is not None
             else len(items))
    if count != expected:
        raise ValueError('%s has %d members but the Yes/No mask has %d' % (description, count, expected))
    try:
        if isinstance(items, _MeshPick) and items.fits and items.fits[0] is not None:
            picked = _MeshPick(items.owner, [items.groups[i] for i in positions],
                               [items.fits[i] for i in positions])
        elif isinstance(items, _MeshPick):  # flat faces or labels: still a mesh pick (Set, Surface take it so)
            picked = _MeshPick(items.owner, [[items[i] for i in positions]], [None], items.what)
        else:
            picked = tuple(items[i] for i in positions)
    except IndexError:
        raise ValueError('%s has fewer members than the Yes/No mask' % description)
    return _geometry(picked, description)


def _cone(face, radius, owner):
    """A cone also answers getRadius (its mid radius): a cylinder's circular edges all have the face's radius."""
    if owner is None:
        return False
    try:
        for index in face.getEdges():
            try:
                edge_radius = owner.edges[index].getRadius()
            except Exception:  # a straight edge (a partial cylinder's side)
                continue
            if abs(edge_radius - radius) > 1e-6 * max(radius, 1.0):
                return True
    except Exception:
        return False
    return False


class _MeshPick(object):
    """Element faces By shape found on a part or instance that is only a mesh, one group per hole (fits: radius,
    center, axis of each) or one group of flat faces (fits: None); or what Mesh labels picked (what: 'nodes',
    'elements' or 'faces', in faces whatever they are)."""

    def __init__(self, owner, groups, fits, what='faces'):
        self.owner, self.groups, self.fits, self.what = owner, groups, fits, what
        self.faces = [f for g in groups for f in g]

    def __len__(self):
        return len(self.faces)

    def __iter__(self):
        return iter(self.faces)

    def __getitem__(self, i):
        return self.faces[i]


def _fit_cylinder(points, normals):
    """(radius, center on the axis, axis) of mesh faces whose normals are all perpendicular to one axis (a cylinder;
    a cone or a sphere is not, a flat patch has no axis), with every node on the fitted circle, else None."""
    import numpy
    n = numpy.array(normals, dtype=float)
    w, v = numpy.linalg.eigh(numpy.dot(n.T, n) / len(n))
    if w[0] > 0.01 or w[1] < 0.02:
        return None
    axis, u, t = v[:, 0], v[:, 1], v[:, 2]
    p = numpy.array(points, dtype=float)
    x, y = numpy.dot(p, u), numpy.dot(p, t)
    a = numpy.column_stack((x, y, numpy.ones(len(x))))
    sol = numpy.linalg.lstsq(a, -(x * x + y * y), rcond=-1)[0]
    cx, cy = -sol[0] / 2.0, -sol[1] / 2.0
    r2 = cx * cx + cy * cy - sol[2]
    if r2 <= 0:
        return None
    r = r2 ** 0.5
    if numpy.sqrt(numpy.mean((numpy.hypot(x - cx, y - cy) - r) ** 2)) > 0.02 * r:
        return None
    center = cx * u + cy * t + numpy.mean(numpy.dot(p, axis)) * axis
    return float(r), [float(c) for c in center], [float(c) for c in axis]


def _mesh_patches(faces, angle=30.0):
    """The faces in smooth patches: neighbours (sharing an element edge) whose normals differ by less than angle."""
    import math
    limit = math.cos(math.radians(angle))
    normals = [f.getNormal() for f in faces]
    nodes = [f.getNodes() for f in faces]
    labels = [[n.label for n in ns] for ns in nodes]
    coords = dict((n.label, n.coordinates) for ns in nodes for n in ns)
    by_node = dict()
    for i, ls in enumerate(labels):
        for label in ls:
            by_node.setdefault(label, []).append(i)
    seen = [False] * len(faces)
    groups = []
    for start in range(len(faces)):
        if seen[start]:
            continue
        seen[start] = True
        stack, group = [start], []
        while stack:
            i = stack.pop()
            group.append(i)
            shared = dict()
            for label in labels[i]:
                for j in by_node[label]:
                    if j != i and not seen[j]:
                        shared[j] = shared.get(j, 0) + 1
            for j, count in shared.items():
                if count >= 2 and sum(a * b for a, b in zip(normals[i], normals[j])) >= limit:
                    seen[j] = True
                    stack.append(j)
        groups.append(group)
    return groups, normals, labels, coords


def _mesh_by_shape(owner, spec, description='Region'):
    """By shape on a mesh without geometry (an imported or orphan mesh): the outer element faces in smooth
    patches; a patch is a hole (or pin) when it fits a cylinder, of the Radius within Tolerance (0: any)."""
    what = spec[1]
    if what in ('circle', 'small'):
        raise ValueError('%s: %s need geometry; on a mesh-only part use Cylindrical or Flat faces' %
                         (description, 'circular edges' if what == 'circle' else 'small faces'))
    free = [f for f in owner.elementFaces if len(f.getElements()) == 1]
    if what == 'plane':
        want = spec[2:5]
        size = sum(c * c for c in want) ** 0.5
        flat = []
        for f in free:
            normal = f.getNormal()
            length = sum(c * c for c in normal) ** 0.5
            if length and sum(a * b for a, b in zip(normal, want)) / (length * size) >= 0.99985:
                flat.append(f)
        pick = _MeshPick(owner, [flat], [None])
        _geometry(pick.faces, description)
        _say('%s matched %d element faces of the mesh by shape' % (description, len(pick.faces)))
        return pick
    groups, normals, labels, coords = _mesh_patches(free)
    kept, fits = [], []
    for g in groups:
        if len(g) < 3:
            continue
        points = dict((label, coords[label]) for i in g for label in labels[i]).values()
        fit = _fit_cylinder(list(points), [normals[i] for i in g])
        if fit is None or not _radius_fits(fit[0], (spec[0], spec[1], spec[2], max(spec[3], 1e-3 * spec[2]))
                                           + tuple(spec[4:])):
            continue
        kept.append([free[i] for i in g])
        fits.append(fit)
    pick = _MeshPick(owner, kept, fits)
    _geometry(pick.faces, description)
    _say('%s matched %d cylinder%s (%d element faces) in the mesh by shape' %
         (description, len(fits), '' if len(fits) == 1 else 's', len(pick.faces)))
    return pick


class _RPPick(object):
    """A part's reference points, by feature id: an instance keeps its part's under the same ids."""

    def __init__(self, ids):
        self.ids = ids

    def __len__(self):
        return len(self.ids)


def _rp_pick(owner, name):
    """The reference point a Part Feature named name made, on a part or (through its part) an instance."""
    part = getattr(owner, 'part', None) or owner
    if name not in part.features.keys():
        raise ValueError('Reference point %s is not on part %s (its Part Feature was deleted or deactivated?)' %
                         (name, part.name))
    return _RPPick([part.features[name].id])


def _region_kw(source, repo, picked, arg):
    """Keyword arguments for regionToolset.Region / Set / Surface from what a Region selected on source: CAE's own
    sequence of the geometry (a tuple: 'Feature creation failed.'), or for a mesh, its element faces (a surface)
    or their nodes."""
    if isinstance(picked, _RPPick):
        return dict(referencePoints=tuple(source.referencePoints[i] for i in picked.ids))
    if isinstance(picked, _MeshPick) and picked.what == 'nodes':
        return dict(nodes=source.nodes.sequenceFromLabels(sorted(set(n.label for n in picked.faces))))
    if isinstance(picked, _MeshPick) and picked.what == 'elements':
        elements = source.elements.sequenceFromLabels(sorted(set(e.label for e in picked.faces)))
        if arg.startswith('side'):  # a surface on shell elements: side1Faces -> side1Elements
            return dict([(arg[:5] + 'Elements', elements)])
        return dict(elements=elements)
    if isinstance(picked, _MeshPick):
        if arg.startswith('side'):
            by_side = dict()
            for f in picked.faces:
                by_side.setdefault(str(f.face)[-1], []).append(f.getElements()[0].label)
            return dict(('face%sElements' % k, source.elements.sequenceFromLabels(sorted(set(v))))
                        for k, v in by_side.items())
        labels = sorted(set(n.label for f in picked.faces for n in f.getNodes()))
        return dict(nodes=source.nodes.sequenceFromLabels(labels))
    seq = getattr(source, repo)
    targets = seq[0:0]
    for entity in picked:
        targets += seq[entity.index:entity.index + 1]
    return dict([(arg, targets)])


def _on_instances(model, part_name, repo, picked, arg, description='Region'):
    """A Region made on a part, used where CAE wants the assembly (loads, BCs, couplings, contact): the same
    entities on every instance of that part, as CAE shows a part's sets on each of its instances."""
    a = mdb.models[model].rootAssembly
    instances = [i for _, i in sorted(a.instances.items()) if i.partName == part_name]
    if not instances:
        raise ValueError('%s is on part %s, which has no instance in the assembly: place it with Instance first' %
                         (description, part_name))
    kw = dict()
    for instance in instances:
        for k, v in _region_kw(instance, repo, picked, arg).items():
            kw[k] = kw[k] + v if k in kw else v
    if len(instances) > 1:
        _say('%s (on part %s) applied on its %d instances' % (description, part_name, len(instances)))
    return kw


def _radius_fits(radius, spec):
    """A By shape radius test: spec[2] (0: any), within Tolerance spec[3]; spec[4] 'Up to' / 'At least'."""
    if spec[2] <= 0:
        return True
    match = spec[4] if len(spec) > 4 else 'Equal to'
    slack = max(spec[3], 1e-6 * spec[2])
    if match == 'Up to':
        return radius <= spec[2] + slack
    if match == 'At least':
        return radius >= spec[2] - slack
    return abs(radius - spec[2]) <= slack


def _no_radius(what, spec, radii, description):
    """No round geometry of the Radius: say which radii there are (a diameter typed as Radius is a common slip)."""
    names = dict(cylinder='cylindrical faces', circle='circular edges', disk='circular faces')
    if not radii:
        raise ValueError('%s found no %s at all on this geometry' % (description, names[what]))
    match = (spec[4] if len(spec) > 4 else 'Equal to').lower()
    there = sorted(set(round(r, 6) for r in radii))
    shown = ', '.join('%g' % r for r in there[:8]) + (', ...' if len(there) > 8 else '')
    raise ValueError('%s found no %s of radius %s %g (tolerance %g); the radii here are %s' %
                     (description, names[what], match, spec[2], spec[3], shown))


def _round_center(item, what, owner):
    """The centre of a circular edge, or the mean centre of a face's circular edges (a hole's axis midpoint): the
    point Measure Center gives; None when it has no circular edge."""
    edges = [item] if what == 'circle' else [owner.edges[i] for i in item.getEdges()]
    centers = []
    for e in edges:
        try:
            r = e.getRadius()
            k = e.getCurvature(parameter=0.0)
        except Exception:  # a straight edge
            continue
        centers.append([p + c * r * r for p, c in zip(k['evaluationPoint'], k['curvature'])])
    if not centers:
        return None
    mean = [0.0, 0.0, 0.0]  # no sum(): `from abaqus import *` replaces it with the field-output one
    for c in centers:
        for i in range(3):
            mean[i] += c[i] / len(centers)
    return mean


def _nearest(centers, at, description):
    """The position of the centre nearest the point (x, y, z), or an error saying where the matches are."""
    best, best_d = None, None
    for i, c in enumerate(centers):
        if c is None:
            continue
        d = ((c[0] - at[0]) ** 2 + (c[1] - at[1]) ** 2 + (c[2] - at[2]) ** 2) ** 0.5
        if best_d is None or d < best_d:
            best, best_d = i, d
    if best is None:
        raise ValueError('%s found no match near %s' % (description, ', '.join('%g' % c for c in at)))
    return best


def _find_by_shape(repository, spec, description='Region', owner=None):
    """Every cylindrical face / circular edge / flat circular face (of a radius, within a tolerance; radius 0: any;
    cylinders exclude cones), or every flat face facing a direction (within 1 degree). A part or instance that is
    only a mesh is searched through its element faces. A spec ending in '@at', x, y, z keeps the one match whose
    centre is nearest that point (Region's One per geometry: a hole's centre is in empty space, no findAt there)."""
    at = None
    if '@at' in spec:
        cut = list(spec).index('@at')
        spec, at = tuple(spec[:cut]), tuple(spec[cut + 1:cut + 4])
    what = spec[1]
    if owner is not None and not len(repository) and len(getattr(owner, 'elementFaces', ())):
        pick = _mesh_by_shape(owner, spec, description)
        if at is not None and pick.fits and pick.fits[0] is not None:
            best = _nearest([f[1] for f in pick.fits], at, description)
            pick = _MeshPick(owner, [pick.groups[best]], [pick.fits[best]])
        return pick
    found, radii = [], []  # radii: of the round geometry the Radius test turned away, to say what is there
    for item in repository:
        try:
            radius = item.getRadius()
        except Exception:  # not cylindrical / circular
            radius = None
        if what == 'small':
            try:
                size = item.getSize(printResults=False)
            except Exception:
                continue
            if size > spec[2]:
                continue
        elif what in ('cylinder', 'circle'):
            if radius is None or what == 'cylinder' and _cone(item, radius, owner):
                continue
            if not _radius_fits(radius, spec):
                radii.append(radius)
                continue
        elif what == 'disk':
            # A circular planar patch has one boundary edge and that edge has a radius.  This excludes an outer
            # face containing circular holes (several straight/circular boundaries) and annular faces.
            try:
                edge_ids = item.getEdges()
                edge_radius = owner.edges[edge_ids[0]].getRadius() if len(edge_ids) == 1 else None
            except Exception:
                edge_radius = None
            if edge_radius is None:
                continue
            if not _radius_fits(edge_radius, spec):
                radii.append(edge_radius)
                continue
        elif what == 'plane':
            if radius is not None:
                continue
            try:
                normal = item.getNormal()
            except Exception:
                continue
            want = spec[2:5]
            size = (sum(c * c for c in normal) * sum(c * c for c in want)) ** 0.5
            if size == 0 or sum(a * b for a, b in zip(normal, want)) / size < 0.99985:  # cos 1 degree
                continue
        found.append(item.index)
    if not found and what in ('cylinder', 'circle', 'disk'):
        _no_radius(what, spec, radii, description)
    if at is not None:
        found = [found[_nearest([_round_center(repository[i], what, owner) for i in found], at, description)]]
    _geometry(found, description)
    _say('%s matched %d geometr%s by shape' % (description, len(found), 'y' if len(found) == 1 else 'ies'))
    picked = repository[0:0]  # a native sequence, as Set, Surface and features want
    for index in found:
        picked += repository[index:index + 1]
    return picked


def _find_operand(repository, spec, description, owner):
    """One side of a boolean Region: (indices, points, tolerance, positions, expected count)."""
    indices, points, tolerance, positions, expected = spec
    if indices:
        items = _geometry(tuple(repository[i] for i in indices), description)
    else:
        items = _find_geometry(repository, points, tolerance, description, owner)
    if positions is not None:
        items = _region_positions(items, positions, expected, description)
    if isinstance(items, _MeshPick):
        raise ValueError('%s: Union / Intersection / Difference need geometry; this part is only a mesh' %
                         description)
    return items


def _find_boolean(repository, spec, description='Region', owner=None):
    """A Set Operation of two Regions on one part or instance: their geometry combined by entity index."""
    op = spec[1]
    first = [g.index for g in _find_operand(repository, spec[2], description + ' (a)', owner)]
    second = [g.index for g in _find_operand(repository, spec[3], description + ' (b)', owner)]
    if op == 'Union':
        keep = first + second
    elif op == 'Intersection':
        keep = [i for i in first if i in second]
    else:
        keep = [i for i in first if i not in second]
    unique = []
    for i in keep:
        if i not in unique:
            unique.append(i)
    if not unique:
        raise ValueError('%s: the %s of a and b is empty' % (description, op.lower()))
    _say('%s: %s kept %d geometr%s' % (description, op.lower(), len(unique), 'y' if len(unique) == 1 else 'ies'))
    picked = repository[0:0]  # a native sequence, as Set, Surface and features want
    for index in unique:
        picked += repository[index:index + 1]
    return picked


def _find_labels(owner, spec, description='Region'):
    """Mesh labels: nodes, elements or element faces (by face, S1 to S6) of a part's or instance's mesh by label
    ranges, as an input file's sets and surfaces name them."""
    what = spec[1]
    if owner is None or not len(owner.nodes):
        raise ValueError('%s: Mesh labels needs a meshed part or instance (this one has no mesh)' % description)
    repo = owner.nodes if what == 'nodes' else owner.elements
    noun = 'node' if what == 'nodes' else 'element'
    items = []
    for face, ranges in spec[2]:
        labels = []
        for first, last in ranges:
            labels.extend(range(first, last + 1))
        try:
            found = repo.sequenceFromLabels(labels)
        except Exception:
            found = ()
        if len(found) != len(labels):
            known = set(x.label for x in repo)
            missing = [str(k) for k in labels if k not in known]
            raise ValueError('%s: the mesh has no %s %s' % (description, noun, ', '.join(missing[:10]) +
                                                            (' ...' if len(missing) > 10 else '')))
        if what == 'faces':
            want = 'FACE' + face[1:]
            for element in found:
                match = [f for f in element.getElemFaces() if str(f.face) == want]
                if not match:
                    raise ValueError('%s: element %d has no face %s' % (description, element.label, face))
                items.extend(match)
        else:
            items.extend(found)
    pick = _MeshPick(owner, [items], [None], what)
    _geometry(pick.faces, description)
    _say('%s selected %d mesh %s%s by label' % (description, len(items), 'element face' if what == 'faces' else noun,
                                               '' if len(items) == 1 else 's'))
    return pick


def _find_geometry(repository, points, tolerance=0.0, description='Region', owner=None):
    """Resolve every requested point, optionally to the nearest entity, and report the real match count. owner:
    the part or instance the repository belongs to (By shape uses it to tell cones apart and to search a mesh)."""
    if points and points[0] and points[0][0] == '@shape':  # a By shape Region
        return _find_by_shape(repository, points[0], description, owner)
    if points and points[0] and points[0][0] == '@bool':  # a Set Operation of two Regions
        return _find_boolean(repository, points[0], description, owner)
    if points and points[0] and points[0][0] == '@labels':  # mesh nodes, elements or element faces by label
        return _find_labels(owner, points[0], description)
    if tolerance > 0.0:
        found = repository.getClosest(coordinates=tuple(points), searchTolerance=tolerance)
        missing = [points[i] for i in range(len(points)) if i not in found]
        if missing:
            raise ValueError('%s found no geometry within tolerance %g at %s' %
                             (description, tolerance, ', '.join(str(p) for p in missing)))
        items = tuple(found[i][0] for i in range(len(points)))
    else:
        items, missing = [], []
        for point in points:
            item = repository.findAt((point,))
            # findAt of a point list gives a sequence (its .index is a method), empty when nothing is there
            if item is not None and not isinstance(getattr(item, 'index', None), int):
                item = item[0] if len(item) else None
            if item is None:
                missing.append(point)
            else:
                items.append(item)
        if missing:
            raise ValueError('%s found no geometry at %s; check the points or set a Find at tolerance' %
                             (description, ', '.join(str(p) for p in missing)))
    unique = []
    seen = set()
    for item in items:
        index = item.index
        if index not in seen:
            seen.add(index)
            unique.append(item)
    _geometry(unique, description)
    _say('%s matched %d unique geometr%s from %d point%s' %
         (description, len(unique), 'y' if len(unique) == 1 else 'ies', len(points), '' if len(points) == 1 else 's'))
    return tuple(unique)


def _odb(path):
    """Open an ODB once (or reuse it if it's already open in this session)."""
    if path not in _opened:
        real = _this_release(path)
        # CAE keys its open ODBs by the file it opened (slashes its own way), not by the name given
        known = dict((_same_file(k), k) for k in session.odbs.keys())
        key = known.get(_same_file(real)) or known.get(_same_file(path))
        if key is not None:
            _opened[path] = session.odbs[key]
            if os.path.exists(os.path.splitext(path)[0] + '.lck'):
                try:  # a running job's ODB: add the steps and frames it wrote since it was opened
                    _opened[path].update()
                except Exception as exc:
                    _say('could not read the newest results of %s: %s' % (path, exc))
        else:
            _opened[path] = session.openOdb(name=path, path=real, readOnly=True)
    return _opened[path]


def _same_file(path):
    return os.path.normcase(os.path.abspath(path))


def _this_release(path):
    """An ODB written by an older Abaqus (e.g. solved with 2025, shown in CAE 2026) can't be opened as it is: show an
    upgraded copy (the file itself stays as it is), kept in one temporary folder per file and made again only when
    the file is newer, so live updates reuse it."""
    from odbAccess import isUpgradeRequiredForOdb, upgradeOdb
    if not os.path.exists(path) or not isUpgradeRequiredForOdb(upgradeRequiredOdbPath=path):
        return path
    import tempfile
    import zlib
    folder = os.path.join(tempfile.gettempdir(), 'nocode_upgraded',
                          '%08x' % (zlib.crc32(_same_file(path).encode('utf-8')) & 0xffffffff))
    target = os.path.join(folder, os.path.basename(path))
    if os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(path):
        return target
    for key in list(session.odbs.keys()):  # an older upgrade of it, still open
        if _same_file(key) == _same_file(target):
            session.odbs[key].close()
    if not os.path.isdir(folder):
        os.makedirs(folder)
    if os.path.exists(target):
        os.remove(target)
    upgradeOdb(existingOdbPath=path, upgradedOdbPath=target)
    _say('%s is from an older Abaqus release: showing an upgraded copy (%s)' % (os.path.basename(path), target))
    return target


def _current_viewport():
    """CAE's current viewport ('Viewport: 1' unless the user picked another one)."""
    name = _home if _home in session.viewports.keys() else session.currentViewportName
    vp = session.viewports[name]
    vp.makeCurrent()
    return vp


def _viewport(name):
    """The output's own viewport, found by name so that updates reuse it."""
    # Abaqus/CAE 2026 often crashes (GUI segfault) when an ODB is shown in a newly
    # created viewport, so the first output goes to CAE's own 'Viewport: 1' and only
    # further outputs get (cascaded) viewports of their own. 'Viewport: 1' is left
    # alone when outputs set to Viewport = Current already draw into it.
    first = not _used_viewports
    _used_viewports.append(name)
    spare = 'Viewport: 1' in session.viewports.keys() and not (_CURRENT_USED and _home == 'Viewport: 1')
    if name in session.viewports.keys():
        vp = session.viewports[name]
    elif first and spare:
        vp = session.viewports['Viewport: 1']
    else:
        cur = session.viewports[session.currentViewportName]
        shift = 5.0 * len(session.viewports.keys())
        vp = session.Viewport(name=name, origin=(cur.origin[0] + shift, max(0.0, cur.origin[1] - shift)),
                              width=cur.width, height=cur.height)
    vp.makeCurrent()
    return vp


def _forget_annotations(prefix):
    """Annotations live in mdb.annotations (made with mdb.Text / mdb.Arrow); drop ours from an earlier update."""
    try:
        for name in list(mdb.annotations.keys()):
            if name.startswith(prefix):
                del mdb.annotations[name]
    except Exception:
        pass


def _marks(vp, prefix, marks):
    """Labelled arrows pointing at model points: [(text, (x, y, z)), ...]. Never stops the plot if CAE refuses."""
    _forget_annotations(prefix)
    for i, (text, xyz) in enumerate(marks):
        try:
            arrow = mdb.Arrow(name='%s %d arrow' % (prefix, i), startAnchor=xyz, startPoint=(8.0, 8.0),
                              endAnchor=xyz, endPoint=(0.0, 0.0))
            label = mdb.Text(name='%s %d' % (prefix, i), text=text, anchor=xyz, offset=(9.0, 9.0))
            vp.plotAnnotation(annotation=arrow)
            vp.plotAnnotation(annotation=label)
        except Exception as exc:
            _say('could not mark %s in the viewport (%s); it is listed in the note' % (text, exc))
            return


def _group(name, kind, members):
    """A display group of nodes or elements: {{instance name: [labels]}}. Shown via View > Display Group."""
    leaf = None
    for inst, labels in sorted(members.items()):
        if not inst:
            continue  # assembly-level members have no part instance to name them by
        labels = tuple(str(l) for l in labels)
        part = (dgo.LeafFromNodeLabels(partInstanceName=inst, nodeLabels=labels) if kind == 'node'
                else dgo.LeafFromElementLabels(partInstanceName=inst, elementLabels=labels))
        if leaf is None:
            if name in session.displayGroups.keys():
                del session.displayGroups[name]
            leaf = session.DisplayGroup(name=name, leaf=part)
        else:
            leaf.add(leaf=part)
    return leaf


def _session_field(odb, name, description, position, rows):
    """A field the editor computed, as a session field output (CAE's 'Session Step', like Create Field Output):
    rows = {{instance name: [(label, value), ...]}}. Returns the name it got (a new one per update)."""
    try:
        sodb = session.scratchOdbs[odb.path]
    except Exception:
        sodb = session.ScratchOdb(odb=odb)
    if 'Session Step' in sodb.steps.keys():
        sstep = sodb.steps['Session Step']
    else:
        sstep = sodb.Step(name='Session Step', description='Step for Viewer non-persistent fields',
                          domain=TIME, timePeriod=1.0)
    if len(sstep.frames):
        sframe = sstep.frames[0]
    else:
        sframe = sstep.Frame(frameId=0, frameValue=0.0, description='Session Frame')
    label, n = name, 1
    while label in sframe.fieldOutputs.keys():  # session fields can't be replaced: number the new one
        n += 1
        label = '%s-%d' % (name, n)
    fo = sframe.FieldOutput(name=label, description=description, type=SCALAR)
    for inst, pairs in rows.items():
        fo.addData(position=position, instance=odb.rootAssembly.instances[inst],
                   labels=tuple(p[0] for p in pairs), data=tuple((p[1],) for p in pairs))
    return label


def _note(vp, name, lines):
    """A text box in the corner of a viewport listing results."""
    if not lines:
        return
    try:
        _forget_annotations(name)
        text = mdb.Text(name=name, text='\\n'.join(lines), offset=(4.0, 4.0), anchor=BOTTOM_LEFT)
        vp.plotAnnotation(annotation=text)
    except Exception as exc:
        _say('could not add the results note (%s); the results are printed above' % exc)
    for line in lines:
        _say(line)
'''

FOOTER = '''

def main():
    for step in ({steps}):
        try:
            step()
        except Exception as exc:
            _say('%s failed: %s' % (step.__doc__, exc))
    if _CURRENT_USED and _home in session.viewports.keys():
        session.viewports[_home].makeCurrent()  # so the next update's "current" is the same viewport
    _say('done.')


main()
'''


def _name(prefix: str, text: str, used: set[str]) -> str:
    """A valid, unique Abaqus repository key (letters, digits, - and _; max 38 characters)."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "x"
    base = f"{prefix}_{slug}"[:38]
    name, n = base, 2
    while name in used:
        tail = f"_{n}"
        name = base[: 38 - len(tail)] + tail
        n += 1
    used.add(name)
    return name


def _num(v: float) -> str:
    return repr(float(f"{v:.10g}"))


def _fmt(v: float) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.4g}"


def _xy_literal(s: Series) -> str:
    pts = [(x, y) for x, y in zip(s.x.tolist(), s.y.tolist()) if math.isfinite(x) and math.isfinite(y)]
    return "(" + ", ".join(f"({_num(x)}, {_num(y)})" for x, y in pts) + ("," if len(pts) == 1 else "") + ")"


def _number_lines(numbers: list[Number]) -> list[str]:
    return [f"{n.label}: {_fmt(n.value)}" + (f" ({n.where})" if n.where else "") for n in numbers]


def _flat(v) -> list:
    return [x for item in (v if isinstance(v, list) else [v]) for x in (item if isinstance(item, list) else [item])
            if x is not None]


class _Writer:
    def __init__(self):
        self.funcs: list[str] = []
        self.blocks: list[str] = []
        self.contents: list[str] = []
        self.used: set[str] = set()
        self.node_id = ""
        self.node_name = ""  # the user's name for the node, if any
        self.module = "Visualization"  # the CAE module to show the result in
        self.sketch = None  # (model, name) the link plug-in opens from the Sketch module's Sketch list
        self.current_used = False
        self.drawn = False  # an output draws in a viewport (only then does it need the Visualization module)

    def target(self, kind: str, title: str, params: dict) -> tuple[str, str]:
        """(code that picks the viewport, how to describe it) for an output's Viewport setting."""
        self.drawn = True
        if params.get("viewport", "Current") == "New":
            name = _name("NoCode", f"{kind} {self.node_id}", self.used)
            return f"vp = _viewport({name!r})", f"its own viewport '{name}'"
        self.current_used = True
        return "vp = _current_viewport()", "the current viewport"

    def add(self, doc: str, body: list[str], summary: str):
        fname = f"output_{len(self.funcs) + 1}"
        self.funcs.append(fname)
        self.contents.append(f"  - {summary}")
        lines = [f"def {fname}():", f"    {doc!r}"] + [("    " + ln) if ln else "" for ln in body]
        self.blocks.append("\n".join(lines))

    # ---------------------------------------------------------- contours
    def contour(self, index: int, inputs: dict, params: dict):
        f: Field = inputs["field"]
        origin = f.origin or {}
        title = self.node_name or f"Contour {index}: {f.name}"
        pick_vp, where = self.target("Contour", title, params)
        body = [f"path = {f.odb.path!r}"]
        # Computed in the editor (Formula, combined frames, Field from Sets): CAE gets the values themselves.
        derived = bool(origin.get("math") or origin.get("custom") or origin.get("combine"))
        if f.odb.path.startswith("mock://"):
            body += ["_say('skipping %s: it uses the built-in demo model, not an ODB file' % "
                     f"{title!r})", "return"]
            self.add(title, body, f"{title} (skipped: demo model)")
            return
        step_names = [s["name"] for s in f.odb.info["steps"]]
        body += [
            "if not os.path.exists(path):",
            "    _say('ODB not found: ' + path)",
            "    return",
            "odb = _odb(path)",
            pick_vp,
            "vp.setValues(displayedObject=odb)",
            "od = vp.odbDisplay",
        ]
        if derived:
            if f.values.shape[1] != 1:
                raise ValueError(f"'{f.name}' has {f.values.shape[1]} components: pick one (Quantity) to show it in CAE")
            rows: dict[str, list] = {}
            for inst, lab, v in zip(f.instances, f.labels, f.values[:, 0]):
                if str(inst) and np.isfinite(v):  # assembly-level rows have no instance to put them on
                    rows.setdefault(str(inst), []).append((int(lab), round(float(v), 9)))
            name = _abaqus_name(f.name, "NoCodeField")[:38]
            pos = "NODAL" if f.position == "NODAL" else "CENTROID"
            body += [f"# {f.name}: computed by the editor, shown as a session field output (like Create Field Output)",
                     f"label = _session_field(odb, {name!r}, {f.name!r}, {pos}, {rows!r})",
                     "od.setFrame(step='Session Step', frame=0)",
                     # CAE shows centroid data only as ELEMENT_CENTROID (CENTROID there is 'invalid position')
                     f"od.setPrimaryVariable(variableLabel=label, outputPosition="
                     f"{'NODAL' if pos == 'NODAL' else 'ELEMENT_CENTROID'})"]
            if inputs.get("deform") is not None:
                body.append("_say('note: computed fields are shown on the undeformed shape')")
            inputs = {**inputs, "deform": None}
        else:
            body.append(f"od.setFrame(step={step_names.index(f.step)}, frame={f.frame})"
                        f"  # {f.step}, t = {_fmt(f.frame_value)}")
        requested_position = origin.get("requested_position", "Automatic")
        position = {"Nodal": "NODAL", "Centroid": "CENTROID", "Integration point": "INTEGRATION_POINT",
                    "Element nodal": "ELEMENT_NODAL"}.get(
                        requested_position, "NODAL" if f.position == "NODAL" else "INTEGRATION_POINT")
        refinement = origin.get("refinement")
        variable = origin.get("variable", f.name)
        if refinement and refinement[0] == "COMPONENT" and origin.get("type") == "SCALAR":
            refinement = None  # a scalar (e.g. PEEQ) has no components in CAE: show it as is
        if refinement and refinement[0] == "INVARIANT":
            allowed = ops.available_invariants(origin.get("type", "TENSOR"), f.invariants)
            if refinement[1] not in allowed:  # the editor reports this too; never hand CAE a refinement it rejects
                raise ValueError(f"'{refinement[1]}' isn't available for {variable} in this ODB")
        if derived:
            pass  # shown above, from its own values
        elif refinement:
            body.append(f"od.setPrimaryVariable(variableLabel={variable!r}, outputPosition={position}, "
                        f"refinement=({refinement[0]}, {refinement[1]!r}))")
        else:
            body.append(f"od.setPrimaryVariable(variableLabel={variable!r}, outputPosition={position})")
        # What the editor shows (deformation scale, limits, marker positions on the deformed shape): CAE matches it.
        shown = _contour_display(inputs, params)
        d: Field | None = inputs.get("deform")
        if d is not None:
            body += [f"od.setDeformedVariable({d.origin.get('variable', d.name)!r})",
                     "od.display.setValues(plotState=(CONTOURS_ON_DEF,))",
                     # the editor's factor (also when automatic), so arrows land on the deformed surface
                     f"od.commonOptions.setValues(deformationScaling=UNIFORM, "
                     f"uniformScaleFactor={_num(float(shown['deformScale']))})"]
        else:
            body.append("od.display.setValues(plotState=(CONTOURS_ON_UNDEF,))")
        opts = [f"numIntervals={int(params.get('bands') or 12)}", f"spectrum={params.get('spectrum', 'Rainbow')!r}"]
        lo_set, hi_set = shown["limits"]
        opts += [f"minAutoCompute=OFF, minValue={_num(shown['min'])}"] if lo_set else ["minAutoCompute=ON"]
        opts += [f"maxAutoCompute=OFF, maxValue={_num(shown['max'])}"] if hi_set else ["maxAutoCompute=ON"]
        on = "ON" if params.get("minmax") == "On" else "OFF"
        opts += [f"showMinLocation={on}, showMaxLocation={on}"]
        body += ["try:",
                 f"    od.contourOptions.setValues({', '.join(opts)})",
                 "except Exception as exc:",
                 "    _say('some contour options were not accepted (%s)' % exc)",
                 "    od.contourOptions.setValues(" + opts[0] + ")",
                 "try:",
                 f"    od.commonOptions.setValues(visibleEdges={EDGES.get(params.get('edges', 'All edges'), 'ALL')})",
                 "except Exception:",
                 "    pass"]
        view = params.get("view", "Iso")
        if view != "Keep":
            body += ["try:",
                     f"    vp.view.setValues(session.views[{view!r}])",
                     "    vp.view.fitView()",
                     "except Exception as exc:",
                     f"    _say('could not set the {view} view (%s)' % exc)"]
        sets = origin.get("sets") or []
        if sets:
            dg_name = _name("NoCode", f"Contour {self.node_id} only", self.used)
            odb_sets = {s["name"]: s.get("kind") for s in f.odb.info.get("sets", [])}
            if all(odb_sets.get(name) == f.entity for name in sets):
                leaf = "LeafFromNodeSets(nodeSets" if f.position == "NODAL" else "LeafFromElementSets(elementSets"
                body += [f"# Only show the set(s) the field was filtered by: {', '.join(sets)}",
                         f"dg = session.DisplayGroup(name={dg_name!r}, leaf=dgo.{leaf}={tuple(sets)!r}))",
                         "od.setValues(visibleDisplayGroups=(dg,))"]
            else:  # restricted by values, picks or a group: show exactly the field's nodes/elements
                members: dict[str, list[int]] = {}
                for inst, lab in zip(f.instances, f.labels):
                    members.setdefault(str(inst), []).append(int(lab))
                body += [f"# Only show the {len(f.labels)} {f.entity}s the field was restricted to ({', '.join(sets)})",
                         f"dg = _group({dg_name!r}, {f.entity!r}, {members!r})",
                         "if dg is not None:",
                         "    od.setValues(visibleDisplayGroups=(dg,))"]
        items = _flat(inputs.get("markers"))
        notes = _number_lines([x for x in items if isinstance(x, Number)])
        marks, groups = self._results_in_cae(f, items, shown["markers"])
        prefix = _name("NoCode", f"Contour {self.node_id} mark", self.used)
        body.append(f"_marks(vp, {prefix!r}, {marks!r})")
        for gname, kind, members in groups:
            body.append(f"_group({gname!r}, {kind!r}, {members!r})")
            notes.append(f"display group '{gname}': {sum(len(v) for v in members.values())} {kind}s")
        body.append(f"_note(vp, {_name('NoCode', f'Contour {self.node_id} notes', self.used)!r}, {notes!r})")
        extra = [f"{len(marks)} result(s) marked"] if marks else []
        extra += [f"{len(groups)} display group(s)"] if groups else []
        self.add(title, body, f"{title} in {where}" + (", " + ", ".join(extra) if extra else ""))

    def _results_in_cae(self, f: Field, items: list, markers: list) -> tuple[list, list]:
        """The contour's Results as CAE sees them: arrows at located values (and at a few members of fields/sets),
        at the editor's marker positions (on the deformed shape when deformed), and display groups for fields/sets.
        Min/max markers are left to CAE's own min/max locations."""
        if not items:
            return [], []
        marks = [(m["text"], tuple(round(float(c), 6) for c in m["position"])) for m in markers
                 if m["text"] and not m.get("extreme")]
        groups = []
        for item in items:
            if isinstance(item, (Field, EntitySet)):
                kind = item.entity if isinstance(item, Field) else item.kind
                members: dict[str, list[int]] = {}
                for inst, lab in zip(item.instances, item.labels):
                    members.setdefault(str(inst), []).append(int(lab))
                groups.append((_name("NoCode", f"{self.node_name or 'Contour ' + self.node_id} {item.name}", self.used),
                               kind, members))
        return marks, groups

    # ------------------------------------------------------------ charts
    def chart(self, index: int, inputs: dict, params: dict):
        series: list[Series] = _flat(inputs.get("series"))
        title = params.get("title") or self.node_name or f"Chart {index}"
        plot_name = _name("NoCode", title, self.used)
        pick_vp, where = self.target("Chart", title, params)
        body = ["curves = []"]
        for s in series:
            key = _name("NC", s.name, self.used)
            body += [
                f"if {key!r} in session.xyDataObjects.keys():",
                f"    del session.xyDataObjects[{key!r}]",
                f"xy = session.XYData(name={key!r}, data={_xy_literal(s)},",
                f"                    legendLabel={s.name!r}, xValuesLabel={(params.get('xlabel') or s.xlabel)!r},",
                f"                    yValuesLabel={(params.get('ylabel') or s.ylabel)!r})",
                "curves.append(session.Curve(xyData=xy))",
            ]
        body += [
            f"if {plot_name!r} in session.xyPlots.keys():",
            f"    del session.xyPlots[{plot_name!r}]",
            f"plot = session.XYPlot(name={plot_name!r})",
            "chart = list(plot.charts.values())[0]",
            "chart.setValues(curvesToPlot=curves)",
            "try:",
            f"    plot.title.setValues(text={title!r}, useDefault=OFF)",
            "except Exception:",
            "    pass",
            pick_vp,
            "vp.setValues(displayedObject=plot)",
        ]
        self.add(title, body, f"{title}: {len(series)} curve(s) as XY data + an XY plot in {where}")

    # ------------------------------------------------------ the model itself
    def model(self, path: str):
        """The ODB's undeformed mesh in the current viewport (when no output draws there)."""
        title = f"Model: {os.path.basename(path)}"
        pick_vp, where = self.target("Model", title, {"viewport": "Current"})
        body = [f"path = {path!r}",
                "if not os.path.exists(path):",
                "    _say('ODB not found: ' + path)",
                "    return",
                "odb = _odb(path)",
                pick_vp,
                "vp.setValues(displayedObject=odb)",
                "vp.odbDisplay.display.setValues(plotState=(UNDEFORMED,))",
                "try:",
                "    vp.view.fitView()",
                "except Exception:",
                "    pass"]
        self.add(title, body, f"{title}: the undeformed mesh in {where}")

    # ------------------------------------------------------ display groups
    def group(self, index: int, inputs: dict, params: dict):
        kind, sets = group_members(inputs["members"])
        members: dict[str, list[int]] = {}
        for s in sets:
            for inst, lab in zip(s.instances, s.labels):
                labs = members.setdefault(str(inst), [])
                if int(lab) not in labs:
                    labs.append(int(lab))
        name = params.get("name") or self.node_name or f"NoCode group {index}"
        n = sum(len(v) for v in members.values())
        body = [f"dg = _group({name!r}, {kind!r}, {members!r})",
                "if dg is None:",
                f"    _say('display group %s: no part-instance members to group' % {name!r})",
                "    return"]
        show = params.get("show", "Create only")
        if show != "Create only":
            self.drawn = True
            body += ["vp = session.viewports[session.currentViewportName]",
                     "od = vp.odbDisplay"]
            body.append("od.setValues(visibleDisplayGroups=(dg,))" if show.startswith("Show only") else
                        "od.setValues(visibleDisplayGroups=tuple(od.visibleDisplayGroups) + (dg,))")
        body.append(f"_say('display group %s: {n} {kind}s' % {name!r})")
        self.add(f"Display group {name}", body, f"Display group '{name}': {n} {kind}s"
                 + ("" if show == "Create only" else f" ({show.lower()} in the current viewport)"))

    # ------------------------------------------------------ values/table
    def values(self, index: int, inputs: dict, params: dict):
        v = inputs.get("value")
        if isinstance(v, Field):
            return self.table(index, {"field": v}, params)
        if not all(isinstance(x, Number) for x in _flat(v)):  # texts, Yes/No, sets…: print what the editor shows
            title = self.node_name or params.get("label") or f"Value {index}"
            lines = [f"{title}: {summarize(v)}"]
            self.add(title, [f"for line in {lines!r}:", "    _say(line)"], f"{title}: printed to the message area")
            return
        numbers = _flat(inputs.get("value"))
        label = params.get("label") or self.node_name
        if label and len(numbers) == 1:
            numbers = [Number(numbers[0].value, label, numbers[0].where)]
        unit = f" {params['unit']}" if params.get("unit") else ""
        lines = [ln + unit for ln in _number_lines(numbers)]
        self.add(f"Value {index}", [f"for line in {lines!r}:", "    _say(line)"],
                 f"Value {index}: printed to the message area")

    def table(self, index: int, inputs: dict, params: dict):
        f: Field = inputs["field"]
        order = (-abs(f.values[:, 0])).argsort()[:10] if f.values.shape[1] == 1 else range(min(10, len(f.labels)))
        rows = [f"{entity_name(str(f.instances[i]), f.labels[i])}: " + ", ".join(_fmt(float(v)) for v in f.values[i]) for i in order]
        lines = [f"{f.name} (first {len(rows)} of {len(f.labels)} rows, {', '.join(f.components)})"] + rows
        title = self.node_name or f"Table {index}"
        self.add(title, [f"for line in {lines!r}:", "    _say(line)"], f"{title}: top rows printed to the message area")


HANDLERS = {"view.contour": "contour", "view.chart": "chart", "view.value": "values", "view.table": "table",
            "view.group": "group"}


def _params(node: dict) -> dict:
    return {p.name: p.default for p in REGISTRY[node["type"]].params} | (node.get("params") or {})


def _in_current(node: dict) -> bool:
    """Draws into CAE's current viewport (a Contour Plot or Chart whose Viewport is Current)."""
    return node.get("type") in ("view.contour", "view.chart") and _params(node).get("viewport", "Current") != "New"


def _graph_odb(graph: dict, results: dict, node_inputs: dict) -> str | None:
    """The first real ODB file the graph reads (not the built-in demo model)."""
    def real(p: str) -> bool:
        return bool(p) and not p.startswith("mock://")
    for node in graph.get("nodes", []):
        for v in (node_inputs.get(node["id"]) or {}).values():
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, Odb) and real(item.path):
                    return item.path
    for node in graph.get("nodes", []):  # an Open ODB node with nothing after it yet
        if node.get("type") == "odb.open" and results.get(node["id"], {}).get("status") == "ok":
            inputs = node_inputs.get(node["id"]) or {}
            path = _odb_path(_params(node), inputs)
            files = _odb_files(path) if real(path) else None
            path = files[0] if files else path
            if real(path):
                return path
    return None


def build_script(graph: dict, results: dict, node_inputs: dict, filename: str = "nocode_cae.py",
                 previous: dict | None = None, previous_keys: dict | None = None) -> dict:
    """The CAE script for the graph's outputs, and its modelling (see cae_model: only what changed is rebuilt).

    Only one output can own the current viewport, otherwise each one would overwrite the one before. That is
    ``graph["focus"]`` (the output last clicked in the web app) when it draws there, else the last Contour Plot
    (or the last Chart when there is no contour). For modelling it is the modelling node clicked last, else the
    one that changed since ``previous`` (each modelling node's fingerprint at the last update). A modelling node
    with an error keeps the CAE objects it made at the last update (``previous_keys``) until it's fixed.
    """
    w = _Writer()
    skipped = []
    counters: dict[str, int] = {}
    ready = {n["id"] for n in graph.get("nodes", [])
             if results.get(n["id"], {}).get("status") == "ok" and n["id"] in node_inputs}
    current = [n["id"] for n in graph.get("nodes", []) if _in_current(n) and n["id"] in ready]
    contours = [i for i in current if any(n["id"] == i and n["type"] == "view.contour" for n in graph["nodes"])]
    fallback = (contours or current or [None])[-1]
    # one graph holds the model and its results: a modelling node clicked last owns the current viewport (the
    # results' outputs there wait), a results output clicked last keeps it (modelling is built, not shown)
    model_focus = any(n["id"] == graph.get("focus") and cae_model.is_model_node(n) for n in graph.get("nodes", []))
    shown = graph.get("focus") if graph.get("focus") in current else (None if model_focus else fallback)
    for node in graph.get("nodes", []):
        kind = HANDLERS.get(node.get("type"))
        if not kind:
            continue
        counters[kind] = counters.get(kind, 0) + 1
        title = REGISTRY[node["type"]].title
        if results.get(node["id"], {}).get("status") == "disabled":
            skipped.append(f"{title} {counters[kind]} (deactivated)")
            continue
        if node["id"] not in ready:
            skipped.append(f"{title} {counters[kind]} (not ready: {results.get(node['id'], {}).get('message') or 'error'})")
            continue
        if _in_current(node) and node["id"] != shown:
            skipped.append(f"{title} {counters[kind]} (the current viewport shows " +
                           ("the model node clicked last" if model_focus else "another output") +
                           ": click this one in the graph, or set its Viewport to New)")
            continue
        params = _params(node)
        w.node_id = node["id"]
        w.node_name = str(node.get("name") or "")
        getattr(w, kind)(counters[kind], node_inputs[node["id"]], params)

    modelled = _modelling(w, graph, results, node_inputs, previous, previous_keys or {})
    fingerprints, node_keys, objects = modelled if modelled else (None, {}, [])
    _measurements(w, node_inputs, objects)
    meshes = cae_mesh.queries(node_inputs, objects)
    if meshes:
        w.node_id, w.node_name = "mesh", ""
        w.add("Mesh preview", meshes, "Mesh preview: the meshes the editor shows")
    draws = [n for n in graph.get("nodes", []) if n.get("type") in ("view.contour", "view.chart") and n["id"] in ready]
    draws += [n for n in graph.get("nodes", []) if cae_model.is_model_node(n)]
    if not draws:  # no contour or chart for CAE yet: show the graph's ODB there, undeformed
        path = _graph_odb(graph, results, node_inputs)
        if path:
            w.node_id, w.node_name = "model", ""
            body_count = len(w.funcs)
            w.model(path)
            # the model goes first, so outputs in their own viewports come after it
            w.funcs.insert(0, w.funcs.pop(body_count))
            w.blocks.insert(0, w.blocks.pop(body_count))
            w.contents.insert(0, w.contents.pop(body_count))
    if not w.funcs:
        raise ValueError("Nothing to export: add a Contour Plot, Chart, Value or Table node that runs without errors.")
    contents = "\n".join(w.contents)
    script = (HEADER.format(when=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), filename=filename,
                            contents=contents, current_used=w.current_used)
              + (cae_model.HELPERS if fingerprints is not None else "")
              + "\n\n" + "\n\n\n".join(w.blocks) + "\n\n" + FOOTER.format(steps=", ".join(w.funcs) + ","))
    return {"script": script, "filename": filename, "module": w.module, "sketch": w.sketch,
            "contents": [c.strip("- ").strip() for c in w.contents],
            "skipped": skipped, "fingerprints": fingerprints or {}, "node_keys": node_keys}


def _modelling(w: _Writer, graph: dict, results: dict, node_inputs: dict, previous: dict | None,
               previous_keys: dict) -> tuple[dict, dict, list] | None:
    """Add the modelling steps (build what changed, then show one node's result); each modelling node's fingerprint
    and the CAE objects it made, or None when the graph has no modelling (a results-only graph: models are left alone)."""
    if (not any(cae_model.is_model_node(n) for n in graph.get("nodes", [])) and graph.get("tab") != "model"
            and not cae_model.picked_value(graph, node_inputs)):
        return None
    stages: dict = {}
    objects, values, broken = cae_model.collect(graph, results, node_inputs, stages)
    keep = sorted({k for nid in broken for k in previous_keys.get(nid, [])})
    titles = {n["id"]: (n.get("name") or REGISTRY[n["type"]].title) for n in graph.get("nodes", [])}
    broken_names = [titles[nid] for nid in broken if previous_keys.get(nid)]
    entries = []
    built = [o for o in objects if not o.get("external") and not o.get("transient")]
    for i, o in enumerate(built, 1):
        w.blocks.append("\n".join([f"def _make_{i}():", f"    {o['label']!r}"] + ["    " + ln for ln in o["code"]]))
        entries.append(f"    ({o['key']!r}, {o['fp']!r}, {o['deps']!r}, _make_{i}),")
    w.node_id, w.node_name = "model", ""
    counts = {k: sum(o["key"].startswith(k + "|") for o in built)
              for k in ("model", "material", "section", "sketch", "part", "datum", "feature", "instance", "region", "step", "condition", "interaction")}
    what = ", ".join(f"{n} {k}{'es' if k == 'sketch' and n != 1 else 's' if n != 1 else ''}"
                     for k, n in counts.items() if n) or "nothing yet"
    renamed = [f"{o['value'].name} -> {o['name']} ({o['key'].split('|')[1]})" for o in objects if o["renamed"]]
    notes = [f"_say({('two different objects asked for one name in a model; built as: ' + ', '.join(renamed))!r})"]
    w.add("Model", [*(notes if renamed else []), "_build([", *entries, f"], {keep!r}, {broken_names!r}, {str(graph.get('doc') or '')!r}, {graph.get('take', True) is not False!r})"],
          f"Model: {what}, built in CAE (only what changed is rebuilt)")
    results_focus = any(n["id"] == graph.get("focus") and _in_current(n) for n in graph.get("nodes", []))
    shown = None if results_focus else cae_model.shown_node(graph, values, previous, stages)
    picked = cae_model.picked_value(graph, node_inputs)
    passed = None if picked else cae_model.passed_on(graph, node_inputs, objects)
    if picked:  # an Input picked in CAE: shown as it is there
        w.add("Show Input", ["vp = _current_viewport()", *cae_model.show_code("input.value", [picked[1]], objects)],
              "Input shown in the current viewport")
        w.current_used = True
        w.module = cae_model.module_for("input.value", picked[1], objects)
        w.sketch = cae_model.shown_sketch(picked[1], objects)
    elif passed:  # a general node (Merge, Pick from List…) on modelling values: all of them, as their maker shows them
        node_type, vals = passed
        title = titles[graph["focus"]]
        w.add(f"Show {title}", ["vp = _current_viewport()", *cae_model.show_code(node_type, vals, objects)],
              f"{title} shown in the current viewport")
        w.current_used = True
        w.module = cae_model.module_for(node_type, vals[0], objects, vals)
        w.sketch = cae_model.shown_sketch(vals[0], objects)
    elif shown:
        node_type = next(n["type"] for n in graph["nodes"] if n["id"] == shown)
        vals = values.get(shown) or stages[shown]
        w.add(f"Show {REGISTRY[node_type].title}", ["vp = _current_viewport()",
              *cae_model.show_code(node_type, vals, objects)],
              f"{REGISTRY[node_type].title} shown in the current viewport")
        w.current_used = True
        first = vals[0]
        w.module = cae_model.module_for(node_type, first, objects, vals)
        w.sketch = cae_model.shown_sketch(first, objects)
    elif not w.drawn:  # nothing new to show (printed Values, Measure…): CAE stays in the module it's in
        w.module = ""
    types = {n["id"]: n.get("type") for n in graph.get("nodes", [])}
    # a Part Feature followed by more part nodes is only a stage of the part, but still makes its feature
    made = {**{nid: v for nid, v in stages.items() if types.get(nid) == "model.feature"}, **values}
    keys = {**{nid: ks for nid, ks in previous_keys.items() if nid in broken},
            **cae_model.node_keys(made, objects, types)}
    return {nid: cae_model.fingerprint(v) for nid, v in values.items()}, keys, objects


MESH_MEASURES = ("Nodes", "Elements", "Worst aspect ratio", "Mean aspect ratio", "Failed elements")


def _mesh_measure(q, objects: list[dict]) -> list[str]:
    """Node/element counts and mesh quality (verifyMeshQuality) of a Part, an Instance or a Region's geometry."""
    region = q.first if isinstance(q.first, Region) else None
    source = region.source if region is not None else q.first
    obj = cae_model.object_code(source, objects)
    owner = obj if not isinstance(source, Instance) else obj.split(".rootAssembly")[0] + ".rootAssembly"
    lines = []
    if region is not None:
        first, _ = cae_model.selection_code(region, objects, q.quantity)
        lines.append(f"first = {first}")
        regions = ", regions=tuple(first)"
    elif isinstance(source, Instance):
        lines.append(f"_i = {obj}")
        regions = ", regions=(_i.cells[:] if len(_i.cells) else _i.faces[:] if len(_i.faces) else _i.edges[:])"
    else:
        regions = ""
    if q.quantity in ("Nodes", "Elements"):
        method, repo = ("getNodes", "nodes") if q.quantity == "Nodes" else ("getElements", "elements")
        count = (f"len(set(_x.label for _e in first for _x in _e.{method}()))" if region is not None else
                 f"len({obj}.{repo})")
        return lines + [f"answers[{q.key!r}] = float({count})"]
    if q.quantity == "Failed elements":
        return lines + [f"_q = {owner}.verifyMeshQuality(criterion=ANALYSIS_CHECKS{regions})",
                        f"answers[{q.key!r}] = float(len(_q['failedElements']))"]
    field = "worst" if q.quantity == "Worst aspect ratio" else "average"
    return lines + [f"_q = {owner}.verifyMeshQuality(criterion=ASPECT_RATIO, threshold=10.0{regions})",
                    f"answers[{q.key!r}] = float(_q.get({field!r}, 0.0))"]


def _per_item(q, objects: list[dict]) -> list[str]:
    """How many faces/edges… a Region found, or each one's radius (None: not round) or center: a cylindrical face's
    centroid (on its axis), a circular edge's center of curvature, a cell's centroid, else a point on it."""
    first, owner = cae_model.selection_code(q.first, objects, q.quantity)
    obj = cae_model.object_code(q.first.source, objects)
    repo = {"face": "faces", "edge": "edges", "vertex": "vertices", "cell": "cells"}[q.first.kind]
    lines = [f"_first = {first}", "_items = list(_first)", "_fits = getattr(_first, 'fits', [None])"]
    lines += ["_holes = _fits if _fits and _fits[0] is not None else None  # a mesh: one fit per hole"]
    if q.quantity == "Count":
        return lines + [f"answers[{q.key!r}] = float(len(_holes) if _holes is not None else len(_items))"]
    lines += ["def _radius(e):", "    try:", "        return float(e.getRadius())", "    except Exception:",
              "        return None"]
    if q.quantity == "Radius":
        return lines + [f"answers[{q.key!r}] = ([f[0] for f in _holes] if _holes is not None else "
                        "[_radius(e) for e in _items])"]
    lines += ["def _center(e):", "    if hasattr(e, 'face'):  # an element face: the mean of its nodes",
              "        _c = [n.coordinates for n in e.getNodes()]",
              "        return [float(sum(p[i] for p in _c) / len(_c)) for i in range(3)]"]
    if repo in ("faces", "cells"):
        lines += [f"    return [float(c) for c in {owner}.getCentroid({repo}={obj}.{repo}[e.index:e.index + 1])]"]
    elif repo == "edges":
        lines += ["    r = _radius(e)", "    if r:", "        k = e.getCurvature(parameter=0.0)",
                  "        return [float(p + c * r * r) for p, c in zip(k['evaluationPoint'], k['curvature'])]",
                  "    return [float(c) for c in e.pointOn[0]]"]
    else:
        lines += ["    return [float(c) for c in e.pointOn[0]]"]
    return lines + [f"answers[{q.key!r}] = ([f[1] for f in _holes] if _holes is not None else "
                    "[_center(e) for e in _items])"]


STAGE = "__nocode_stage"


def _at_stage(q, objects: list[dict], lines: list[str]) -> list[str]:
    """Measure a Region made on a part before later Part Features (Remove faces, partitions…) on the part as it was
    then: those features are on the same CAE part, so the lines run on a copy with them suppressed (suppressing them
    on the part itself would delete its mesh)."""
    regions = [r for r in (q.first, q.second) if isinstance(r, Region) and isinstance(r.source, Part)]
    if not regions or q.quantity in MESH_MEASURES:
        return lines
    stage = regions[0].source
    part = cae_model.built(stage, objects)
    mine = f"feature|{part['key'].split('|')[1]}|{part['name']}|"
    later = [o["name"] for o in objects if o["key"].startswith(mine) and isinstance(o["value"], PartOperation)
             and o["value"] not in stage.operations]
    if not later:
        return lines
    obj = cae_model.object_code(stage, objects)
    model = obj.split(".parts[")[0]
    code = [f"_stage = {model}.Part(name={STAGE!r}, objectToCopy={obj})", "try:",
            f"    for _f in {list(reversed(later))!r}:", "        _stage.features[_f].suppress()"]
    code += ["    " + line.replace(obj, "_stage") for line in lines]
    return code + ["finally:", f"    del {model}.parts[{STAGE!r}]"]


def _measurements(w: _Writer, node_inputs: dict, objects: list[dict]) -> None:
    """Append unresolved live-CAE measurements and print one machine-readable result line."""
    pending = []
    for out in getattr(node_inputs, "outputs", {}).values():
        pending += [v for v in cae_model._flat(list(out.values())) if isinstance(v, PendingMeasure)]
    if not pending:
        return
    lines = ["answers = {}"]
    for q in dict((v.key, v) for v in pending).values():
        lines += _at_stage(q, objects, _measure(q, objects))
    # an answer is kept for good under this graph's key: none from geometry another tab built (see _build)
    lines += ["if globals().get('_left'):",
              "    _say('measured nothing: the model is as another editor tab built it (press Sync in this tab)')",
              "else:",
              f"    print({cae_queries.MARKER!r} + __import__('json').dumps(answers, sort_keys=True))"]
    w.add("Measure", lines, f"Measure: {len(pending)} CAE geometry quer{'y' if len(pending) == 1 else 'ies'}")


def _measure(q, objects: list[dict]) -> list[str]:
    """The lines that put one query's answer in ``answers``."""
    if q.quantity in MESH_MEASURES:
        return _mesh_measure(q, objects)
    if q.quantity in ("Count", "Radius", "Center"):
        return _per_item(q, objects)
    first, owner1 = cae_model.selection_code(q.first, objects, q.quantity)
    lines = [f"first = {first}"]
    if q.quantity in ("Area", "Length", "Volume"):
        method = {"Area": "getArea", "Length": "getLength", "Volume": "getVolume"}[q.quantity]
        arg = {"Area": "faces", "Length": "edges", "Volume": "cells"}[q.quantity]
        lines.append(f"answers[{q.key!r}] = float({owner1}.{method}({arg}=first))")
    else:
        second, owner2 = cae_model.selection_code(q.second, objects, q.quantity)
        lines.append(f"second = {second}")
        owner = owner1 if owner1 == owner2 else owner1.rsplit(".", 1)[0] + ".rootAssembly"
        if q.quantity == "Distance":
            lines.append(f"answers[{q.key!r}] = min(float({owner}.getDistance(entity1=a, entity2=b)) "
                         "for a in first for b in second)")
        else:
            arg1 = "line1" if q.first.kind == "edge" else "plane1"
            arg2 = "line2" if q.second.kind == "edge" else "plane2"
            lines.append(f"answers[{q.key!r}] = min(float({owner}.getAngle({arg1}=a, {arg2}=b)) "
                         "for a in first for b in second)")
    return lines
