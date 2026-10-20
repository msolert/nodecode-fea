"""Materials, sections and their assignment to parts."""

from __future__ import annotations

from ..values import Material, Part, Section
from .core import _n, _pt


def material_code(value: Material, name: str) -> list[str]:
    lines = [f"m = mdb.models[{value.model.name!r}]", f"material = m.Material(name={name!r})"]
    if value.hyper:
        kind, *table = value.hyper
        lines.append(f"material.Hyperelastic(materialType=ISOTROPIC, testData=OFF, type={kind}, "
                     f"volumetricResponse=VOLUMETRIC_DATA, table=(({', '.join(_n(c) for c in table)}),))")
    elif value.elastic:
        kind, *table = value.elastic
        lines.append(f"material.Elastic(type={kind}, table=(({', '.join(_n(c) for c in table)}),))")
    else:
        lines.append(f"material.Elastic(table=(({_n(value.young)}, {_n(value.poisson)}),))")
    if value.damping:
        lines.append(f"material.Damping(alpha={_n(value.damping[0])}, beta={_n(value.damping[1])})")
    if value.density > 0:
        lines.append(f"material.Density(table=(({_n(value.density)},),))")
    if value.hardening and value.hardening[0] == "JOHNSON_COOK":
        lines.append(f"material.Plastic(hardening=JOHNSON_COOK, table=(({', '.join(_n(c) for c in value.hardening[1:])}),))")
    elif value.plastic:
        rows = ", ".join(f"({_n(s)}, {_n(e)})" for s, e in value.plastic)
        hardening = f"hardening={value.hardening[0]}, " if value.hardening else ""
        half = "dataType=HALF_CYCLE, " if value.hardening and value.hardening[0] == "COMBINED" else ""
        lines.append(f"material.Plastic({hardening}{half}table=({rows},))")
    if value.damage:
        strain, displacement = value.damage
        lines += [f"material.DuctileDamageInitiation(table=(({_n(strain)}, 0.0, 0.0),))",
                  f"material.ductileDamageInitiation.DamageEvolution(type=DISPLACEMENT, table=(({_n(displacement)},),))"]
    for prop, v in (("Conductivity", value.conductivity), ("SpecificHeat", value.specific_heat),
                    ("Expansion", value.expansion)):
        if v:
            lines.append(f"material.{prop}(table=(({_n(v)},),))")
    return lines


def copy_code(value, model: str) -> list[str]:
    """A material or section copied from another model (a material library) by name; a section brings its material
    when the model doesn't have one of that name yet. The source model is only read."""
    lines = [f"m = mdb.models[{model!r}]", f"_source = mdb.models[{value.copy_from!r}]"]
    if isinstance(value, Material):
        return lines + [f"m.copyMaterials(sourceModel=_source, names=({value.name!r},))"]
    return lines + [f"m.copySections(sourceModel=_source, names=({value.name!r},))",
                    f"_material = getattr(_source.sections[{value.name!r}], 'material', None)",
                    "if _material and _material not in m.materials.keys():",
                    "    m.copyMaterials(sourceModel=_source, names=(_material,))"]


def section_code(value: Section, name: str, material_name: str) -> list[str]:
    lines = [f"m = mdb.models[{value.material.model.name!r}]"]
    if value.kind == "Solid":
        thickness = _n(value.thickness) if value.thickness else "None"  # a 2D planar part's out-of-plane thickness
        lines.append(f"m.HomogeneousSolidSection(name={name!r}, material={material_name!r}, thickness={thickness})")
    elif value.kind == "Shell":
        lines.append(f"m.HomogeneousShellSection(name={name!r}, material={material_name!r}, "
                     f"thickness={_n(value.thickness)})")
    elif value.kind == "Composite shell":
        lines.append("import section")
        layers = ", ".join(f"section.SectionLayer(material={material_name!r}, thickness={_n(t)}, "
                           f"orientAngle={_n(a)}, numIntPts=3, plyName={f'Ply-{k}'!r})"
                           for k, (a, t) in enumerate(value.plies, 1))
        lines.append(f"m.CompositeShellSection(name={name!r}, preIntegrate=OFF, idealization=NO_IDEALIZATION, "
                     f"symmetric={value.symmetric}, thicknessType=UNIFORM, poissonDefinition=DEFAULT, "
                     f"temperature=GRADIENT, integrationRule=SIMPSON, layup=({layers},))")
    else:
        profile_name = name + "-Profile"
        if value.profile == "Circle":
            lines.append(f"m.CircularProfile(name={profile_name!r}, r={_n(value.radius)})")
        else:
            lines.append(f"m.RectangularProfile(name={profile_name!r}, a={_n(value.width)}, b={_n(value.height)})")
        lines.append(f"m.BeamSection(name={name!r}, integration=DURING_ANALYSIS, poissonRatio="
                     f"{_n(value.material.poisson)}, profile={profile_name!r}, material={material_name!r}, "
                     "temperatureVar=LINEAR, consistentMassMatrix=False)")
    return lines


def _selector_parts(selector: tuple) -> tuple:
    """Read current four-item selectors and older three-item values from saved/runtime objects."""
    return (*selector[:3], selector[3] if len(selector) > 3 else 0.0)


def native_sequence_lines(name: str, repo: str, indices: tuple, points: tuple,
                          tolerance: float = 0.0, description: str = "Region") -> list[str]:
    """Generated code selecting geometry while preserving Abaqus's native GeomSequence type."""
    if indices:
        picked = f"tuple({repo}[i] for i in {tuple(indices)!r})"
    else:
        picked = (f"_find_geometry({repo}, {tuple(points)!r}, {_n(tolerance)}, {description!r}, "
                  f"owner={repo.rsplit('.', 1)[0]})")
    return [f"_picked = {picked}", f"{name} = {repo}[0:0]", "for _entity in _picked:",
            f"    {name} += {repo}[_entity.index:_entity.index + 1]"]


def section_assignment_code(p: Part, names: dict) -> list[str]:
    lines = []
    for i, (section, selector) in enumerate(p.sections, 1):
        repo = {"Solid": "cells", "Shell": "faces", "Wire": "edges"}[p.effective_body]
        lines.append("import regionToolset")
        if selector and selector[2] and selector[2][0] and selector[2][0][0] == "@labels":
            # an element set of the mesh (Region by Mesh labels)
            lines.append(f"section_region = regionToolset.Region(**_region_kw(part, {repo!r}, _find_geometry("
                         f"part.{repo}, {tuple(selector[2])!r}, 0.0, 'Section Region', owner=part), {repo!r}))")
        elif selector:
            kind, indices, points, tolerance = _selector_parts(selector)
            lines += native_sequence_lines("section_targets", f"part.{repo}", indices, points, tolerance,
                                           "Section Region")
            lines.append(f"section_region = regionToolset.Region({repo}=section_targets)")
        else:  # the whole part; a mesh without geometry (imported from an input file): its elements
            lines += [f"section_targets = part.{repo}[:]", "if len(section_targets) or not len(part.elements):",
                      f"    section_region = regionToolset.Region({repo}=section_targets)", "else:",
                      "    section_region = regionToolset.Region(elements=part.elements)"]
        lines.append(f"part.SectionAssignment(region=section_region, "
                     f"sectionName={names.get(section, section.name)!r})")
        if p.body == "Wire" and section.kind in ("Beam", ""):  # not an axisymmetric wire's shell section
            lines.append("part.assignBeamSectionOrientation(region=section_region, method=N1_COSINES, "
                         f"n1={tuple(section.n1)!r})")
        if section.orientation:  # the material's 1-direction: a datum csys with its x axis along it
            x = section.orientation
            y = (-x[1], x[0], 0.0) if abs(x[0]) + abs(x[1]) > 1e-9 else (0.0, 1.0, 0.0)  # z × x, or y
            csys = f"{names.get(section, section.name)}-Csys-{i}"
            lines += [f"if {csys!r} in part.features.keys():", f"    del part.features[{csys!r}]",
                      f"_csys = part.DatumCsysByThreePoints(name={csys!r}, coordSysType=CARTESIAN, "
                      f"origin=(0.0, 0.0, 0.0), point1={_pt(x)}, point2={_pt(y)})",
                      "part.MaterialOrientation(region=section_region, orientationType=SYSTEM, axis=AXIS_3, "
                      "localCsys=part.datums[_csys.id], additionalRotationType=ROTATION_NONE, "
                      "stackDirection=STACK_3)"]
    return lines
