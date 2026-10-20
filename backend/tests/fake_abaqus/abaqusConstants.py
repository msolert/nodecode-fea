"""Stand-in for Abaqus' abaqusConstants module (tests only)."""


class SymbolicConstant:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name

    __repr__ = __str__

    def __eq__(self, other):
        return str(other) == self.name

    def __hash__(self):
        return hash(self.name)


NODAL = SymbolicConstant("NODAL")
CENTROID = SymbolicConstant("CENTROID")
INTEGRATION_POINT = SymbolicConstant("INTEGRATION_POINT")
WHOLE_ELEMENT = SymbolicConstant("WHOLE_ELEMENT")

# Visualization constants used by generated CAE scripts.
for _n in ("INVARIANT", "COMPONENT", "CONTOURS_ON_DEF", "CONTOURS_ON_UNDEF", "UNDEFORMED", "ALL", "FEATURE", "EXTERIOR", "NONE",
           "THREE_D", "DEFORMABLE_BODY", "IMPRINT", "CARTESIAN", "XYPLANE", "YZPLANE", "XZPLANE", "ELEMENT_NODAL", "ELEMENT_CENTROID",
           "SUPPRESS", "DELETE", "GEOMETRY", "RIGHT", "COPLANAR_EDGES", "CLOCKWISE", "COUNTERCLOCKWISE",
           "XAXIS", "YAXIS", "ZAXIS", "SIDE1", "SIDE2",
           "TIME", "SCALAR", "CENTROID", "UNIFORM", "BOTTOM_LEFT",
           "ON", "OFF", "TRUE", "FALSE", "FINER", "FREE", "STRUCTURED", "SWEEP",
           "HEX", "TET", "QUAD", "TRI", "STANDARD", "B31", "S4R", "S3", "C3D8R", "C3D4", "C3D10", "C3D8H", "C3D10H", "B32", "S8R", "C3D8I", "C3D20R", "HEX_DOMINATED", "QUAD_DOMINATED", "APPLY_FORCE", "FIX_LENGTH",
           "NEO_HOOKE", "MOONEY_RIVLIN", "VOLUMETRIC_DATA",
           "DURING_ANALYSIS", "LINEAR", "N1_COSINES", "COMPUTED", "HARD", "DEFAULT", "FRICTIONLESS",
           "PENALTY", "STEP", "SOLVER_DEFAULT", "FIELD", "POINT", "XYZ", "WHOLE_SURFACE", "DISTRIBUTING",
           "KINEMATIC", "STANDALONE", "SINGLE", "GLOBAL", "SEMI_AUTOMATIC", "MODEL", "AT_BEGINNING", "BELOW_MIN", "SELF", "BEAM", "JOIN", "HINGE", "AXIAL", "BUSHING", "DOUBLE", "ASPECT_RATIO", "ANALYSIS_CHECKS", "ISOTROPIC", "FINITE", "SMALL", "OVERCLOSED", "FRACTION", "PERCENTAGE", "UNSET", "SET", "FREED", "CONSTANT_THROUGH_THICKNESS", "MAGNITUDE",
           "LAMINA", "ENGINEERING_CONSTANTS", "NO_IDEALIZATION", "GRADIENT", "SIMPSON", "SYSTEM", "AXIS_3",
           "ROTATION_NONE", "STACK_3", "CLOSEST_POINT_FRACTION", "TOTAL",
           "TWO_D_PLANAR", "AXISYMMETRIC", "DISCRETE_RIGID_SURFACE", "ANALYTIC_RIGID_SURFACE",
           "C3D10M", "CPS4R", "CPS4", "CPS8R", "CPS3", "CPE4R", "CPE4", "CPE8R", "CPE3", "CPE4H", "CAX4R", "CAX4",
           "CAX8R", "CAX3", "CAX4H", "B21", "B22", "SAX1", "R3D4", "R3D3", "R2D2", "RAX2",
           "CYLINDRICAL", "SPHERICAL", "STEADY_STATE", "TRANSIENT", "EMBEDDED_COEFF", "KINEMATIC", "COMBINED",
           "HALF_CYCLE", "JOHNSON_COOK", "DISPLACEMENT", "DC3D8", "DC3D4", "DC3D10", "DS4", "DC2D4", "DC2D3",
           "DCAX4", "DCAX3", "C3D8T", "C3D8RT", "C3D4T", "C3D10MT", "CPE4T", "CPS4T", "CAX4T", "MASS_PROPORTIONAL"):
    globals()[_n] = SymbolicConstant(_n)
