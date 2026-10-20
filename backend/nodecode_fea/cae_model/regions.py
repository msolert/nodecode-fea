"""Regions in CAE: sets, surfaces, reference points and selections on parts and the assembly."""

from __future__ import annotations

from dataclasses import dataclass

from ..values import Instance, Interaction, Part, Region
from .core import built, _n, ORDER


# where CAE keeps each kind of Region's entities (a reference point: _rp_pick resolves it, by its feature's id)
REPOS = {"face": "faces", "edge": "edges", "vertex": "vertices", "cell": "cells", "reference point": "referencePoints"}


def rp_feature_name(region: Region, objects: list[dict]) -> str:
    """The CAE feature name of the Part Feature that made a reference point Region (as built, after renames)."""
    return next((o["name"] for o in objects if o["value"] == region.feature), region.feature.name)


def coupling_point(coupling: Interaction, objects: list[dict]) -> str:
    """The CAE region of a coupling's control point (or a connector's wire): the assembly set its code makes."""
    name = next(o["name"] for o in objects if o["value"] == coupling)
    owned = name + ("-Wire" if coupling.kind == "Connector" else "-RP")
    return f"mdb.models[{coupling.model.name!r}].rootAssembly.sets[{owned!r}]"


@dataclass(frozen=True)
class CombinedRegion:
    """One Set/Surface made from every mapped Region output by one Region node."""
    name: str
    regions: tuple[Region, ...]


ORDER[CombinedRegion] = 8


def object_code(v, objects: list[dict]) -> str:
    """Generated-code expression for a Part or Instance, including any collision rename."""
    o = built(v, objects)
    bits = o["key"].split("|")
    if isinstance(v, Part):
        return f"mdb.models[{bits[1]!r}].parts[{o['name']!r}]"
    return f"mdb.models[{bits[1]!r}].rootAssembly.instances[{o['name']!r}]"


def region_kw_code(region: Region, objects: list[dict], repo: str, arg: str) -> str:
    """Code for the keyword arguments of an assembly-level regionToolset.Region / Surface / Set: on the Region's
    instance, or, for a Region made on a part, on every instance of that part."""
    selected, _ = selection_code(region, objects, "")
    source = object_code(region.source, objects)
    if isinstance(region.source, Part):
        part = built(region.source, objects)
        return (f"_on_instances({part['key'].split('|')[1]!r}, {part['name']!r}, {repo!r}, {selected}, {arg!r}, "
                f"{(region.kind.title() + ' Region')!r})")
    return f"_region_kw({source}, {repo!r}, {selected}, {arg!r})"


def selection_code(v, objects: list[dict], quantity: str) -> tuple[str, str]:
    """(entity sequence expression, owning query object expression) for one measurable value."""
    source = v.source if isinstance(v, Region) else v
    obj = object_code(source, objects)
    owner = (f"mdb.models[{source.part.model.name!r}].rootAssembly"
             if isinstance(source, Instance) else obj)
    if isinstance(v, Region) and v.kind == "reference point":
        return f"_rp_pick({obj}, {rp_feature_name(v, objects)!r})", owner
    if isinstance(v, Region):
        repo = REPOS[v.kind]
        if v.indices:
            seq = f"tuple({obj}.{repo}[i] for i in {tuple(v.indices)!r})"
        else:
            seq = (f"_find_geometry({obj}.{repo}, {tuple(v.points)!r}, {_n(v.tolerance)}, "
                   f"{(v.kind.title() + ' Region')!r}, owner={obj})")
    else:
        repo = {"Area": "faces", "Length": "edges", "Volume": "cells"}.get(quantity, "vertices")
        seq = f"{obj}.{repo}[:]"
    if isinstance(v, Region):
        description = v.kind.title() + " Region"
        seq = f"_geometry({seq}, {description!r})"
        if v.positions is not None:
            seq = (f"_region_positions({seq}, {tuple(v.positions)!r}, {v.position_count!r}, "
                   f"{description!r})")
    return seq, owner


def region_code(v: Region, name: str, source_name: str) -> list[str]:
    """Create a named CAE Set or Surface from a Region after its Part/Instance exists."""
    source = v.source
    if isinstance(source, Part):
        lines = [f"m = mdb.models[{source.model.name!r}]", f"owner = m.parts[{source_name!r}]", "selected = owner"]
    else:
        lines = [f"m = mdb.models[{source.part.model.name!r}]", "owner = m.rootAssembly",
                 f"selected = owner.instances[{source_name!r}]"]
    repo = {"face": "faces", "edge": "edges", "vertex": "vertices", "cell": "cells"}[v.kind]
    if v.indices:
        lines.append(f"targets = tuple(selected.{repo}[i] for i in {tuple(v.indices)!r})")
    else:
        lines.append(f"targets = _find_geometry(selected.{repo}, {tuple(v.points)!r}, {_n(v.tolerance)}, "
                     f"{(v.kind.title() + ' Region ' + name)!r}, owner=selected)")
    lines.append(f"targets = _geometry(targets, {(v.kind.title() + ' Region ' + name)!r})")
    if v.positions is not None:
        lines.append(f"targets = _region_positions(targets, {tuple(v.positions)!r}, {v.position_count!r}, "
                     f"{(v.kind.title() + ' Region ' + name)!r})")
    # a mesh-only source: its nodes (Set) or element faces (Surface)
    repo_check = f"(_created_region.nodes if isinstance(targets, _MeshPick) else _created_region.{repo})"
    if v.form == "Set":
        lines += [f"_created_region = owner.Set(name={name!r}, **_region_kw(selected, {repo!r}, targets, {repo!r}))",
                  "try:", f"    _geometry({repo_check}, {(v.kind.title() + ' Set ' + name)!r})",
                  "except Exception:", f"    del owner.sets[{name!r}]", "    raise"]
    else:
        side = "side1" if v.side == "Side 1" else "side2"
        keyword = side + ("Faces" if v.kind == "face" else "Edges")
        lines += [f"_created_region = owner.Surface(name={name!r}, **_region_kw(selected, {repo!r}, targets, "
                  f"{keyword!r}))",
                  "try:", f"    _geometry({repo_check}, {(v.kind.title() + ' Surface ' + name)!r})",
                  "except Exception:", f"    del owner.surfaces[{name!r}]", "    raise"]
    return lines


def combined_region_code(v: CombinedRegion, name: str, objects: list[dict]) -> list[str]:
    """Create one owner-level Set/Surface from several selections on one Part or in one assembly."""
    first = v.regions[0]
    model = first.source.model if isinstance(first.source, Part) else first.source.part.model
    repo = REPOS[first.kind]
    if first.form == "Set":
        arg = repo
    else:
        side = "side1" if first.side == "Side 1" else "side2"
        arg = side + ("Faces" if first.kind == "face" else "Edges")
    owner = object_code(first.source, objects) if isinstance(first.source, Part) else "m.rootAssembly"
    lines = [f"m = mdb.models[{model.name!r}]", f"owner = {owner}", "_combined = dict()"]
    for region in v.regions:
        selected = object_code(region.source, objects)
        targets, _ = selection_code(region, objects, "")
        lines += [f"selected = {selected}", f"targets = {targets}",
                  f"for _key, _values in _region_kw(selected, {repo!r}, targets, {arg!r}).items():",
                  "    _combined[_key] = _combined[_key] + _values if _key in _combined else _values"]
    method = "Set" if first.form == "Set" else "Surface"
    lines.append(f"owner.{method}(name={name!r}, **_combined)")
    return lines
