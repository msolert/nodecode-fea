"""Material & Section: materials, sections and their assignment."""

from __future__ import annotations

import numpy as np
from dataclasses import replace

from ...values import Instance, Material, Model, Part, Region, Section, Text
from ..core import Param, Port, register
from ..common import _finite
from ..field import parse_points
from .geometry import _abaqus_name, _item, _model_value_display, _numbered, _profile_points


MESH_TECHNIQUES = ["Automatic", "Free", "Structured", "Sweep"]
MESH_SEEDS = ["By size", "By number", "Biased"]
MESH_BIAS = ["Fine at start", "Fine at end", "Fine at both ends", "Fine in the middle"]
MESH_SHAPES = ["Automatic", "Hex", "Hex-dominated", "Tet", "Quad", "Quad-dominated", "Tri"]
ELEMENT_TYPES = ["Automatic", "B31", "B32", "S4R", "S8R", "S3", "C3D8R", "C3D8I", "C3D20R", "C3D4", "C3D10", "C3D8H",
                 "C3D10H", "C3D10M", "CPS4R", "CPS4", "CPS8R", "CPS3", "CPE4R", "CPE4", "CPE8R", "CPE3", "CPE4H",
                 "CAX4R", "CAX4", "CAX8R", "CAX3", "CAX4H", "B21", "B22", "SAX1", "R3D4", "R3D3", "R2D2", "RAX2",
                 # heat transfer (temperatures only) and coupled temperature-displacement elements
                 "DC3D8", "DC3D4", "DC3D10", "DS4", "DC2D4", "DC2D3", "DCAX4", "DCAX3",
                 "C3D8T", "C3D8RT", "C3D4T", "C3D10MT", "CPE4T", "CPS4T", "CAX4T"]
HEAT_ELEMENTS = ("DC", "DS")  # heat transfer element codes start so; coupled ones end in T
# which element shape each solid/shell element type is: a hex code on a tet mesh (or the reverse) is ignored by CAE
ELEMENT_SHAPES = {"C3D8R": "Hex", "C3D8I": "Hex", "C3D20R": "Hex", "C3D8H": "Hex", "C3D4": "Tet", "C3D10": "Tet",
                  "C3D10H": "Tet", "C3D10M": "Tet", "S4R": "Quad", "S8R": "Quad", "S3": "Tri", "R3D4": "Quad",
                  "R3D3": "Tri", "DC3D8": "Hex", "DC3D4": "Tet", "DC3D10": "Tet", "C3D8T": "Hex", "C3D8RT": "Hex",
                  "C3D4T": "Tet", "C3D10MT": "Tet", "DS4": "Quad",
                  **{c: "Tri" if c.endswith("3") else "Quad" for c in (
                      "CPS4R", "CPS4", "CPS8R", "CPS3", "CPE4R", "CPE4", "CPE8R", "CPE3", "CPE4H",
                      "CAX4R", "CAX4", "CAX8R", "CAX3", "CAX4H", "DC2D4", "DC2D3", "DCAX4", "DCAX3",
                      "CPE4T", "CPS4T", "CAX4T")}}
# the element families that fit each kind of part: (space, body, rigid) → (code prefixes, automatic, automatic tri)
ELEMENT_FAMILIES = {("3D", "Wire", ""): (("B3",), "B31", "B31"), ("3D", "Shell", ""): (("S3", "S4", "S8", "DS"), "S4R", "S3"),
                    ("3D", "Solid", ""): (("C3D", "DC3D"), "C3D8R", "C3D4"),
                    ("2D planar", "Shell", ""): (("CPS", "CPE", "DC2D"), "CPS4R", "CPS3"),
                    ("2D planar", "Wire", ""): (("B2",), "B21", "B21"),
                    ("Axisymmetric", "Shell", ""): (("CAX", "DCAX"), "CAX4R", "CAX3"),
                    ("Axisymmetric", "Wire", ""): (("SAX",), "SAX1", "SAX1"),
                    ("3D", "Shell", "Discrete"): (("R3D",), "R3D4", "R3D3"),
                    ("2D planar", "Wire", "Discrete"): (("R2D",), "R2D2", "R2D2"),
                    ("Axisymmetric", "Wire", "Discrete"): (("RAX",), "RAX2", "RAX2")}
# the section each kind of deformable part takes: 2D areas are solids (with an out-of-plane thickness), an
# axisymmetric wire is a shell (SAX elements)
SECTION_FOR = {("3D", "Wire"): "Beam", ("3D", "Shell"): "Shell", ("3D", "Solid"): "Solid",
               ("2D planar", "Shell"): "Solid", ("2D planar", "Wire"): "Beam",
               ("Axisymmetric", "Shell"): "Solid", ("Axisymmetric", "Wire"): "Shell"}
BEHAVIOURS = ["Elastic", "Neo-Hooke", "Mooney-Rivlin", "Lamina", "Engineering constants"]
HARDENINGS = ["Isotropic", "Kinematic", "Combined", "Johnson-Cook"]
ORTHOTROPIC = ["Lamina", "Engineering constants"]


def _plies(wired, text: str) -> tuple:
    """((angle°, thickness), ...) of a composite layup from text "0, 0.125; 45, 0.125" or a matrix."""
    pts = parse_points(wired.value if isinstance(wired, Text) else text, 1) if wired is None or isinstance(
        wired, Text) else _profile_points(wired)
    if pts.shape[1] < 2 or not np.isfinite(pts[:, :2]).all():
        raise ValueError("Plies are rows of angle (°), thickness, e.g. 0, 0.125; 45, 0.125; -45, 0.125; 90, 0.125")
    if np.any(pts[:, 1] <= 0):
        raise ValueError("Every ply needs a positive thickness")
    return tuple((float(a), float(t)) for a, t in pts[:, :2])


def _plastic_table(wired, text: str) -> tuple:
    """((yield stress, plastic strain), ...) from plastic strain, stress pairs (text, a matrix or a curve)."""
    if wired is None and not text.strip():
        return ()
    # one row is perfect plasticity (yield stress only)
    pts = parse_points(wired.value if isinstance(wired, Text) else text, 1) if wired is None or isinstance(
        wired, Text) else _profile_points(wired)
    if not np.isfinite(pts[:, :2]).all():
        raise ValueError("Plastic has values that are infinite or NaN")
    strain, stress = pts[:, 0], pts[:, 1]
    if strain[0] != 0:
        raise ValueError("Plastic starts at 0 plastic strain (the initial yield stress), e.g. 0, 250; 0.1, 300")
    if np.any(np.diff(strain) <= 0):
        raise ValueError("Plastic strains must increase from row to row")
    if np.any(stress <= 0):
        raise ValueError("Plastic yield stresses must be positive")
    return tuple((float(s), float(e)) for e, s in zip(strain, stress))


def _hardening(params: dict, plastic: tuple) -> tuple[tuple, tuple]:
    """(hardening, damage) of an elastic-plastic material: () for isotropic, as Material keeps them."""
    kind = str(params.get("hardening") or "Isotropic")
    if kind not in HARDENINGS:
        raise ValueError(f"Hardening is one of {', '.join(HARDENINGS)}")
    hardening = ()
    if kind == "Johnson-Cook":
        names = {"jc_a": "Johnson-Cook A", "jc_b": "Johnson-Cook B", "jc_n": "Johnson-Cook n",
                 "jc_m": "Johnson-Cook m", "jc_melt": "Melting temperature", "jc_transition": "Transition temperature"}
        _finite(params, names)
        values = [float(params[k]) for k in names]
        if values[0] <= 0 or values[1] < 0 or values[2] < 0 or values[3] <= 0:
            raise ValueError("Johnson-Cook needs A > 0, B ≥ 0, n ≥ 0 and m > 0")
        if values[4] <= values[5]:
            raise ValueError("The melting temperature is above the transition temperature")
        hardening = ("JOHNSON_COOK", *values)
    elif kind != "Isotropic":
        if not plastic:
            raise ValueError(f"{kind} hardening needs Plastic: plastic strain, yield stress rows")
        if kind == "Kinematic" and len(plastic) != 2:
            raise ValueError("Kinematic hardening is linear: Plastic is two rows (0, yield; strain, stress)")
        hardening = (kind.upper(),)
    strain = float(params.get("fracture_strain") or 0.0)
    if strain < 0:
        raise ValueError("Fracture strain is 0 (no damage) or positive")
    damage = ()
    if strain > 0:
        if not plastic and not hardening:
            raise ValueError("Ductile damage starts from plastic strain: give the material Plastic (or Johnson-Cook)")
        displacement = float(params.get("failure_displacement") or 0.0)
        if displacement <= 0:
            raise ValueError("Displacement at failure must be positive")
        damage = (strain, displacement)
    return hardening, damage


def _cae_material(raw: str) -> Material | None:
    """A material chosen from CAE's list on Material & Section ("model|name"), as an Input's Material in CAE gives it;
    None when nothing is chosen (this node defines its own)."""
    model_name, _, name = str(raw or "").partition("|")
    if not name.strip():
        return None
    return Material(name.strip(), Model(model_name.strip(), existing=True), 0.0, 0.0, existing=True)


OWN_MATERIAL = {"cae_material": [""]}  # the material settings, shown while no CAE material is chosen


@register("model.section", "Material & Section", "Model",
          "Define a material (elastic, optionally plastic: yield stress against plastic strain, hardening; or "
          "hyperelastic rubber: Neo-Hooke, Mooney-Rivlin; orthotropic: Lamina for composite plies (E1, E2, ν12, "
          "G12, G13, G23) or Engineering constants; Rayleigh damping; isotropic, kinematic, combined or "
          "Johnson-Cook hardening; ductile damage and failure; thermal conductivity, specific heat and expansion), "
          "make a "
          "solid, shell, composite shell (layup of plies: angle and thickness, symmetric or not; carbon/glass "
          "fibre laminates) or beam section, and assign it to a Part or Region in one operation, with a material "
          "orientation (fibre direction) when given. Regions restrict the "
          "assignment; lists of targets or properties create several assigned parts. Picked CAE parts are copied "
          "before assignment. A material already defined in CAE can be chosen from a list on the node (material "
          "library, reuse); an existing material or section from an Input is used as it is too: a material gets "
          "this node's section, a section is assigned directly.",
          inputs=[Port("target", "any", "Part or region", depth=0),
                  Port("existing", "any", "Existing material or section", optional=True, depth=0,
                       help="A material or section already in CAE (Input: Material in CAE / Section in CAE). From "
                            "another model (a material library) it is copied into the part's model. A material "
                            "replaces this node's material settings; a section replaces both (only the beam n1 and "
                            "fibre direction still apply)."),
                  Port("plastic", "any", "Plastic", optional=True, depth=0, table=True,
                       when={**OWN_MATERIAL, "behavior": ["Elastic"]},
                       help="Plastic strain, yield stress pairs: a matrix, a curve (x = plastic strain, y = stress, "
                            "e.g. test data) or text. Overrides the typed table."),
                  Port("plies", "any", "Plies", optional=True, depth=0, table=True,
                       when={"kind": ["Composite shell"]},
                       help="The layup: rows of angle (°), thickness, from the bottom (a matrix or text). Overrides "
                            "the typed table.")],
          outputs=[Port("part", "part", "Part")],
          params=[Param("name", "text", "", "Copy name",
                        help="For a picked CAE part, the editable copy. Empty: original-name-copy."),
                  Param("cae_material", "pick", "", "", when={"unwired:existing": []},
                        help="A material already defined in CAE (any model: from a library model it is copied in), "
                             "used as it is instead of the settings below (⟳ lists them again)"),
                  Param("material", "text", "Material-1", "Material name", when=OWN_MATERIAL),
                  Param("section", "text", "Section-1", "Section name"),
                  Param("behavior", "select", "Elastic", "Behaviour", options=BEHAVIOURS,
                        help="Elastic: Young's modulus and Poisson's ratio (metals, plastics). Neo-Hooke / "
                             "Mooney-Rivlin: hyperelastic rubber for large strains, from C10 (and C01); "
                             "the small-strain shear modulus is 2 (C10 + C01).", when=OWN_MATERIAL),
                  Param("young", "number", 210000.0, "Young's modulus", when={**OWN_MATERIAL, "behavior": ["Elastic"]}),
                  Param("poisson", "number", 0.3, "Poisson's ratio", when={**OWN_MATERIAL, "behavior": ["Elastic"]}),
                  Param("e1", "number", 135000.0, "E1 (fibre)", when={**OWN_MATERIAL, "behavior": ORTHOTROPIC},
                        help="Young's modulus along the fibres (material direction 1)"),
                  Param("e2", "number", 10000.0, "E2", when={**OWN_MATERIAL, "behavior": ORTHOTROPIC}),
                  Param("e3", "number", 10000.0, "E3", when={**OWN_MATERIAL, "behavior": ["Engineering constants"]}),
                  Param("nu12", "number", 0.3, "ν12", when={**OWN_MATERIAL, "behavior": ORTHOTROPIC}),
                  Param("nu13", "number", 0.3, "ν13", when={**OWN_MATERIAL, "behavior": ["Engineering constants"]}),
                  Param("nu23", "number", 0.4, "ν23", when={**OWN_MATERIAL, "behavior": ["Engineering constants"]}),
                  Param("g12", "number", 5000.0, "G12", when={**OWN_MATERIAL, "behavior": ORTHOTROPIC}),
                  Param("g13", "number", 5000.0, "G13", when={**OWN_MATERIAL, "behavior": ORTHOTROPIC}),
                  Param("g23", "number", 3500.0, "G23", when={**OWN_MATERIAL, "behavior": ORTHOTROPIC}),
                  Param("c10", "number", 0.5, "C10",
                        when={**OWN_MATERIAL, "behavior": ["Neo-Hooke", "Mooney-Rivlin"]}),
                  Param("c01", "number", 0.0, "C01", when={**OWN_MATERIAL, "behavior": ["Mooney-Rivlin"]}),
                  Param("d1", "number", 0.0, "D1", when={**OWN_MATERIAL, "behavior": ["Neo-Hooke", "Mooney-Rivlin"]},
                        help="Compressibility: 2 / bulk modulus. 0 is incompressible, which needs hybrid elements "
                             "(C3D8H, C3D10H on Mesh)."),
                  Param("alpha", "number", 0.0, "Damping α (mass)",
                        help="Rayleigh damping: α × mass + β × stiffness. 0 and 0: no damping. For a damping ratio "
                             "ζ at a frequency ω (rad/s): β = 2ζ/ω (stiffness only).", when=OWN_MATERIAL),
                  Param("beta", "number", 0.0, "Damping β (stiffness)", when=OWN_MATERIAL),
                  Param("density", "number", 0.0, "Density", help="0 leaves density undefined", when=OWN_MATERIAL),
                  Param("plastic", "text", "", "Plastic",
                        help="Plastic strain, yield stress pairs from 0 plastic strain, e.g. 0, 250; 0.1, 300. "
                             "Empty: elastic only.",
                        when={**OWN_MATERIAL, "behavior": ["Elastic"]}),
                  Param("hardening", "select", "Isotropic", "Hardening", options=HARDENINGS,
                        when={**OWN_MATERIAL, "behavior": ["Elastic"]},
                        help="How the yield surface follows plastic strain: Isotropic grows it, Kinematic moves it "
                             "(cyclic loading, Bauschinger effect: Plastic is two rows, a straight line), Combined "
                             "does both (Plastic from a half cycle), Johnson-Cook is A + B εᵖⁿ (high rates, "
                             "Explicit; Plastic not used)."),
                  Param("jc_a", "number", 350.0, "Johnson-Cook A", when={**OWN_MATERIAL, "hardening": ["Johnson-Cook"]},
                        help="Yield stress at zero plastic strain"),
                  Param("jc_b", "number", 275.0, "Johnson-Cook B", when={**OWN_MATERIAL, "hardening": ["Johnson-Cook"]}),
                  Param("jc_n", "number", 0.36, "Johnson-Cook n", when={**OWN_MATERIAL, "hardening": ["Johnson-Cook"]}),
                  Param("jc_m", "number", 1.0, "Johnson-Cook m", when={**OWN_MATERIAL, "hardening": ["Johnson-Cook"]},
                        help="Thermal softening exponent"),
                  Param("jc_melt", "number", 1800.0, "Melting temperature",
                        when={**OWN_MATERIAL, "hardening": ["Johnson-Cook"]}),
                  Param("jc_transition", "number", 20.0, "Transition temperature",
                        when={**OWN_MATERIAL, "hardening": ["Johnson-Cook"]},
                        help="Below it there is no thermal softening"),
                  Param("fracture_strain", "number", 0.0, "Fracture strain", when={**OWN_MATERIAL, "behavior": ["Elastic"]},
                        help="Ductile damage (failure, fracture, element deletion): damage starts at this equivalent "
                             "plastic strain. 0: no damage."),
                  Param("failure_displacement", "number", 0.1, "Displacement at failure",
                        when={**OWN_MATERIAL, "behavior": ["Elastic"]},
                        help="After damage starts, the stiffness is gone once the element has opened this much "
                             "(length units; about the element size × the strain still to fail)."),
                  Param("conductivity", "number", 0.0, "Conductivity", when=OWN_MATERIAL,
                        help="Thermal conductivity, for heat transfer (steel: 0.045 W/(mm·K) in mm, tonne, s, mJ… "
                             "units; 45 W/(m·K) in SI). 0: undefined."),
                  Param("specific_heat", "number", 0.0, "Specific heat", when=OWN_MATERIAL,
                        help="For transient heat transfer, with Density (steel: 4.6e8 mJ/(tonne·K) with density "
                             "in tonne/mm³; 460 J/(kg·K) in SI). 0: undefined."),
                  Param("expansion", "number", 0.0, "Expansion coefficient", when=OWN_MATERIAL,
                        help="Thermal expansion per degree (steel: 1.2e-5): temperatures then strain the part "
                             "(thermal stress). 0: none."),
                  Param("kind", "select", "Automatic", "Section type",
                        options=["Automatic", "Solid", "Shell", "Composite shell", "Beam"]),
                  Param("thickness", "number", 1.0, "Shell thickness", when={"kind": ["Shell"]}),
                  Param("plane_thickness", "number", 1.0, "Out-of-plane thickness",
                        when={"kind": ["Automatic", "Solid"]},
                        help="A 2D planar part's thickness (plane stress / plane strain): forces and areas are per "
                             "this thickness. 3D and axisymmetric parts ignore it."),
                  Param("plies", "text", "0, 0.125; 45, 0.125; -45, 0.125; 90, 0.125", "Plies",
                        when={"kind": ["Composite shell"]},
                        help="Angle (° from the fibre direction about the shell normal), thickness of each ply, "
                             "from the bottom; separated by semicolons"),
                  Param("symmetric", "select", "No", "Symmetric layup", options=["No", "Yes"],
                        when={"kind": ["Composite shell"]},
                        help="Yes: the plies are mirrored about the mid-plane (type half the layup)"),
                  Param("orientation", "text", "", "Fibre direction",
                        help="x, y, z of the material's direction 1 (fibres; projected onto shells), e.g. 1, 0, 0. "
                             "Empty: Abaqus' default (global x projected)"),
                  Param("profile", "select", "Rectangle", "Beam profile", options=["Rectangle", "Circle"],
                        when={"kind": ["Beam"]}),
                  Param("width", "number", 10.0, "Width", when={"kind": ["Beam"]}),
                  Param("height", "number", 10.0, "Height", when={"kind": ["Beam"]}),
                  Param("radius", "number", 5.0, "Radius", when={"kind": ["Beam"]}),
                  Param("n1x", "number", 0.0, "Beam n1 x", when={"kind": ["Beam"]}),
                  Param("n1y", "number", 0.0, "Beam n1 y", when={"kind": ["Beam"]}),
                  Param("n1z", "number", -1.0, "Beam n1 z", when={"kind": ["Beam"]})],
          display=lambda o, i, p: _model_value_display(o["part"]))
def model_section(inputs, params):
    target = inputs["target"]
    region = target if isinstance(target, Region) else None
    part = region.source if region is not None else target
    if isinstance(part, Instance):
        raise ValueError("Material & Section works on Parts or Regions on a Part")
    if not isinstance(part, Part):
        raise ValueError("Material & Section needs a Part or Region")
    _finite(params, {"e1": "E1", "e2": "E2", "e3": "E3", "nu12": "ν12", "nu13": "ν13", "nu23": "ν23",
                     "g12": "G12", "g13": "G13", "g23": "G23"})
    _finite(params, {"young": "Young's modulus", "poisson": "Poisson's ratio", "density": "Density",
                     "c10": "C10", "c01": "C01", "d1": "D1", "alpha": "Damping α", "beta": "Damping β",
                     "thickness": "Shell thickness", "width": "Width", "height": "Height", "radius": "Radius",
                     "n1x": "Beam n1 x", "n1y": "Beam n1 y", "n1z": "Beam n1 z"})
    young, poisson, density = float(params["young"]), float(params["poisson"]), float(params["density"])
    _finite(params, {"conductivity": "Conductivity", "specific_heat": "Specific heat", "expansion": "Expansion",
                     "fracture_strain": "Fracture strain", "failure_displacement": "Displacement at failure"})
    thermal = tuple(float(params.get(k) or 0.0) for k in ("conductivity", "specific_heat", "expansion"))
    if thermal[0] < 0 or thermal[1] < 0:
        raise ValueError("Conductivity and specific heat are 0 (undefined) or positive")
    existing = inputs.get("existing")
    if existing is None:
        existing = _cae_material(params.get("cae_material"))
    # a chosen material's properties stay CAE's: the hidden behaviour settings don't apply
    behavior = "Elastic" if existing is not None else str(params.get("behavior") or "Elastic")
    if behavior not in BEHAVIOURS:
        raise ValueError(f"Behaviour is one of {', '.join(BEHAVIOURS)}")
    hyper = ()
    if behavior in ("Neo-Hooke", "Mooney-Rivlin"):
        c10, d1 = float(params.get("c10") or 0.0), float(params.get("d1") or 0.0)
        c01 = float(params.get("c01") or 0.0) if behavior == "Mooney-Rivlin" else 0.0
        if c10 + c01 <= 0 or d1 < 0:
            raise ValueError("Hyperelastic needs C10 + C01 > 0 (half the shear modulus) and D1 ≥ 0")
        hyper = ("NEO_HOOKE", c10, d1) if behavior == "Neo-Hooke" else ("MOONEY_RIVLIN", c10, c01, d1)
        # the small-strain equivalents, for the unit checks: shear modulus 2 (C10 + C01), nearly incompressible
        young, poisson = 6.0 * (c10 + c01), 0.49
    elastic = ()
    if behavior in ORTHOTROPIC:
        names = (["e1", "e2", "nu12", "g12", "g13", "g23"] if behavior == "Lamina" else
                 ["e1", "e2", "e3", "nu12", "nu13", "nu23", "g12", "g13", "g23"])
        values = {k: float(params.get(k) if params.get(k) is not None else 0.0) for k in names}
        if any(values[k] <= 0 for k in names if not k.startswith("nu")):
            raise ValueError("The moduli E and G of an orthotropic material must be positive")
        if any(abs(values[k]) >= 1 for k in names if k.startswith("nu")):
            raise ValueError("The Poisson's ratios ν of an orthotropic material are between -1 and 1")
        elastic = ("LAMINA" if behavior == "Lamina" else "ENGINEERING_CONSTANTS",
                   *(values[k] for k in names))
        young, poisson = values["e1"], min(values["nu12"], 0.49)  # for the unit checks
    if young <= 0 or not -1 < poisson < 0.5 or density < 0:
        raise ValueError("Young's modulus must be positive, Poisson's ratio between -1 and 0.5, and Density ≥ 0")
    alpha, beta = float(params.get("alpha") or 0.0), float(params.get("beta") or 0.0)
    if alpha < 0 or beta < 0:
        raise ValueError("Damping α and β are 0 or positive")
    if part.rigid:
        raise ValueError(f"{part.name} is a {part.rigid.lower()} rigid surface: it takes no material or section (give "
                         "it mass with Interaction → Point mass on its reference point)")
    space = getattr(part, "space", "3D")
    body_section = SECTION_FOR[(space, part.effective_body)]
    kind = body_section if params["kind"] == "Automatic" else str(params["kind"])
    if kind != body_section and not (kind == "Composite shell" and body_section == "Shell"):
        where = f"{space.lower()} " if space != "3D" else ""
        raise ValueError(f"A {where}{part.effective_body.lower()} part needs a {body_section} section")
    if behavior == "Lamina" and kind not in ("Shell", "Composite shell"):
        raise ValueError("Lamina is for shells (plane stress): use a Shell or Composite shell section, or "
                         "Engineering constants for a solid")
    plies = _plies(inputs.get("plies"), str(params.get("plies") or "")) if kind == "Composite shell" else ()
    orientation = ()
    if str(params.get("orientation") or "").strip():
        vec = parse_points(str(params["orientation"]), 1)
        if vec.shape[1] < 3 or not np.any(vec[0][:3]):
            raise ValueError("Fibre direction is x, y, z, e.g. 1, 0, 0")
        if part.effective_body == "Wire":
            raise ValueError("A beam's directions come from its n1: Fibre direction is for solids and shells")
        orientation = tuple(round(float(c), 9) for c in vec[0][:3])
    expected = {"Solid": "cell", "Shell": "face", "Wire": "edge"}[part.effective_body]
    if region is not None and region.kind != expected:
        raise ValueError(f"A {part.effective_body.lower()} section needs a {expected} Region")
    thickness = float(params["thickness"])
    width, height, radius = float(params["width"]), float(params["height"]), float(params["radius"])
    if kind == "Shell" and thickness <= 0:
        raise ValueError("Shell thickness must be positive")
    if kind == "Solid" and space == "2D planar":
        _finite(params, {"plane_thickness": "Out-of-plane thickness"})
        thickness = float(params.get("plane_thickness") if params.get("plane_thickness") is not None else 1.0)
        if thickness <= 0:
            raise ValueError("Out-of-plane thickness must be positive")
    if kind == "Beam" and ((params["profile"] == "Rectangle" and (width <= 0 or height <= 0)) or
                           (params["profile"] == "Circle" and radius <= 0)):
        raise ValueError("Beam profile dimensions must be positive")
    n1 = (float(params["n1x"]), float(params["n1y"]), float(params["n1z"]))
    if kind == "Beam" and not any(n1):
        raise ValueError("Beam n1 direction cannot be zero")
    if existing is not None:
        if not isinstance(existing, (Material, Section)) or not existing.existing:
            raise ValueError("Existing material or section takes an Input set to Material in CAE or Section in CAE, "
                             f"not {type(existing).__name__}")
        home = (existing.model if isinstance(existing, Material) else existing.material.model).name
        if home != part.model.name:  # from a library model: copied into the part's model by name
            existing = (replace(existing, model=part.model, existing=False, copy_from=home)
                        if isinstance(existing, Material) else
                        replace(existing, material=Material("", part.model, 0.0, 0.0, existing=True),
                                existing=False, copy_from=home))
        elif not part.model.existing:
            raise ValueError(f"The Model node rebuilds model {home}, which would delete {existing.name}: keep "
                             "materials and sections to reuse in another model (a library) and pick them from there; "
                             "they are copied in")
    section_name = _numbered(_abaqus_name(str(params["section"]), "Section-1"), params, "section", part.model)
    if isinstance(existing, Section):  # assigned as it is (its own material and type)
        section = replace(existing, n1=n1, orientation=orientation)
    else:
        if isinstance(existing, Material):
            material = existing
        else:
            material_name = _numbered(_abaqus_name(str(params["material"]), "Material-1"), params, "material",
                                      part.model)
            plastic = () if hyper else _plastic_table(inputs.get("plastic"), str(params.get("plastic") or ""))
            hardening, damage = _hardening(params, plastic) if behavior == "Elastic" else ((), ())
            material = Material(material_name, part.model, young, poisson, density,
                                () if hardening[:1] == ("JOHNSON_COOK",) else plastic,
                                hyper, (alpha, beta) if alpha or beta else (), elastic,
                                conductivity=thermal[0], specific_heat=thermal[1], expansion=thermal[2],
                                hardening=hardening, damage=damage)
        section = Section(section_name, material, kind,
                          thickness if kind == "Shell" or (kind == "Solid" and space == "2D planar") else 0.0,
                          str(params["profile"]), width, height, radius, n1,
                          plies, params.get("symmetric") == "Yes", orientation)
    selector = None if region is None else (region.kind, region.indices, region.points, region.tolerance)
    name, source, existing = part.name, part.source, part.existing
    if part.existing:  # a picked CAE part is copied, not changed: the copy gets a name (one per item of a list)
        name = _numbered(_abaqus_name(str(params.get("name") or f"{part.name}-copy"), f"{part.name}-copy"), params,
                         model=part.model)
        source, existing = part, False
    return {"part": replace(part, name=name, existing=existing, source=source,
                            sections=part.sections + ((section, selector),), item=_item(part, params))}
