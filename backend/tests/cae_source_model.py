"""A small model made in Abaqus/CAE itself and saved as a .cae: what "Open CAE model" is tested on.

``SOURCE`` runs in ``abaqus cae noGUI`` and writes ``source.cae`` in its working directory. ``data/cae_dump.json`` is
``cae_reader``'s dump of it (re-record it with ``python -m tests.cae_source_model <launcher>`` after changing either).
It has what the mapper rebuilds (sketched extrude/revolve/planar parts, materials, sections, a mesh, instances turned
about a skew axis, steps, output requests, boundary conditions, loads with an amplitude, a tie) and what it can't (a
filleted part: imported from its geometry; a material with thermal expansion: used from the .cae).
"""

SOURCE = r'''
from abaqus import *
from abaqusConstants import *
from caeModules import *
import regionToolset, mesh

Mdb()
m = mdb.Model(name='Bracket')
del mdb.models['Model-1']
s = m.ConstrainedSketch(name='s', sheetSize=200)
s.rectangle(point1=(0, 0), point2=(100, 50))
s.CircleByCenterPerimeter(center=(50, 25), point1=(60, 25))
plate = m.Part(name='Plate', dimensionality=THREE_D, type=DEFORMABLE_BODY)
plate.BaseSolidExtrude(sketch=s, depth=20)
s = m.ConstrainedSketch(name='s2', sheetSize=200)
s.ConstructionLine(point1=(0, -100), point2=(0, 100))
s.rectangle(point1=(5, 0), point2=(10, 30))
ring = m.Part(name='Ring', dimensionality=THREE_D, type=DEFORMABLE_BODY)
ring.BaseSolidRevolve(sketch=s, angle=270, flipRevolveDirection=OFF)
s = m.ConstrainedSketch(name='s3', sheetSize=200)
s.Line(point1=(0, 0), point2=(40, 0)); s.Line(point1=(40, 0), point2=(40, 30)); s.Line(point1=(40, 30), point2=(0, 0))
tri = m.Part(name='Tri', dimensionality=THREE_D, type=DEFORMABLE_BODY)
tri.BaseShell(sketch=s)
s = m.ConstrainedSketch(name='s4', sheetSize=200)
s.rectangle(point1=(0, 0), point2=(30, 30))
block = m.Part(name='Block', dimensionality=THREE_D, type=DEFORMABLE_BODY)
block.BaseSolidExtrude(sketch=s, depth=30)
block.Round(radius=5.0, edgeList=(block.edges.findAt((30.0, 30.0, 15.0)),))

m.Material(name='Steel')
m.materials['Steel'].Elastic(table=((210000.0, 0.3),))
m.materials['Steel'].Plastic(table=((250.0, 0.0), (300.0, 0.1)))
m.materials['Steel'].Density(table=((7.85e-9,),))
m.Material(name='Alu')
m.materials['Alu'].Elastic(table=((70000.0, 0.33),))
m.Material(name='Hot')
m.materials['Hot'].Elastic(table=((200000.0, 0.29),))
m.materials['Hot'].Expansion(table=((1.2e-5,),))
m.HomogeneousSolidSection(name='SteelSolid', material='Steel', thickness=None)
m.HomogeneousSolidSection(name='HotSolid', material='Hot', thickness=None)
m.HomogeneousShellSection(name='Skin', material='Alu', thickness=2.0)
for p, sec in ((plate, 'SteelSolid'), (ring, 'HotSolid'), (block, 'SteelSolid')):
    p.Set(name='All', cells=p.cells)
    p.SectionAssignment(region=p.sets['All'], sectionName=sec)
tri.SectionAssignment(region=regionToolset.Region(faces=tri.faces), sectionName='Skin')
plate.seedPart(size=5.0, deviationFactor=0.1, minSizeFactor=0.1)
plate.setElementType(regions=(plate.cells,), elemTypes=(mesh.ElemType(elemCode=C3D8R, elemLibrary=STANDARD),))
plate.generateMesh()
plate.Surface(name='Hole', side1Faces=plate.faces.findAt(((40.0, 25.0, 10.0),)))

a = m.rootAssembly
a.Instance(name='Plate-1', part=plate, dependent=ON)
a.Instance(name='Plate-2', part=plate, dependent=ON)
a.rotate(instanceList=('Plate-2',), axisPoint=(0, 0, 0), axisDirection=(0, 0, 1), angle=30)
a.translate(instanceList=('Plate-2',), vector=(200, 10, 0))
a.Instance(name='Ring-1', part=ring, dependent=ON)
a.translate(instanceList=('Ring-1',), vector=(0, 0, 100))
a.rotate(instanceList=('Ring-1',), axisPoint=(10, 0, 100), axisDirection=(1, 1, 0), angle=40)
a.Instance(name='Tri-1', part=tri, dependent=ON)
a.translate(instanceList=('Tri-1',), vector=(0, 100, 0))
a.Instance(name='Block-1', part=block, dependent=ON)
a.translate(instanceList=('Block-1',), vector=(-100, 0, 0))

m.StaticStep(name='Load', previous='Initial', timePeriod=2.0, nlgeom=ON, initialInc=0.2, maxInc=0.5, minInc=1e-6,
             maxNumInc=500)
m.fieldOutputRequests['F-Output-1'].setValuesInStep(stepName='Load', variables=('S', 'U', 'RF'))
m.FrequencyStep(name='Modes', previous='Load', numEigen=5)
i1, i2 = a.instances['Plate-1'], a.instances['Plate-2']
a.Set(name='Fix', faces=i1.faces.findAt(((0.0, 10.0, 10.0),)))
m.EncastreBC(name='Clamp', createStepName='Initial', region=a.sets['Fix'])
m.DisplacementBC(name='Pull', createStepName='Load', region=regionToolset.Region(faces=i2.faces.findAt(
    ((i2.faces[0].pointOn[0]),))), u1=0.5, u2=UNSET, ur3=0.0)
m.TabularAmplitude(name='Ramp2', timeSpan=STEP, smooth=SOLVER_DEFAULT, data=((0.0, 0.0), (1.0, 1.0), (2.0, 0.5)))
m.Pressure(name='Push', createStepName='Load', region=regionToolset.Region(side1Faces=i1.faces.findAt(
    ((100.0, 10.0, 10.0),))), magnitude=5.0, amplitude='Ramp2')
t1 = a.instances['Tri-1']
m.ConcentratedForce(name='Tip', createStepName='Load', region=regionToolset.Region(vertices=t1.vertices.findAt(
    ((40.0, 130.0, 0.0),))), cf2=-10.0)
m.Gravity(name='Weight', createStepName='Load', comp3=-9810.0)
a.Surface(name='Under', side1Faces=a.instances['Block-1'].faces.findAt(((-85.0, 15.0, 0.0),)))
a.Surface(name='PlateBack', side1Faces=i1.faces.findAt(((50.0, 5.0, 0.0),)))
m.Tie(name='Glue', main=a.surfaces['PlateBack'], secondary=a.surfaces['Under'], positionToleranceMethod=COMPUTED,
      adjust=OFF, tieRotations=ON, thickness=ON)
mdb.Job(name='BracketJob', model='Bracket', numCpus=1, memory=80)
mdb.saveAs(pathName='source.cae')
'''

if __name__ == "__main__":  # re-record data/cae_dump.json from real Abaqus
    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from nodecode_fea.cae_reader import import_folder, read_cae

    launcher = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "source.py").write_text(SOURCE, encoding="ascii")
        subprocess.run([launcher, "cae", "noGUI=source.py"], cwd=tmp, check=True, capture_output=True)
        dump = read_cae(str(Path(tmp) / "source.cae"), launcher=launcher)
    # no machine's paths in the fixture: the tests put the part files where IMPORT_DIR says
    text = json.dumps(dump, indent=1).replace(json.dumps(str(Path(tmp) / "source.cae"))[1:-1], "source.cae")
    folder = json.dumps(str(import_folder(str(Path(tmp) / "source.cae"), "model")))[1:-1]
    out = Path(__file__).parent / "data" / "cae_dump.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text.replace(folder, "IMPORT_DIR"))
    print("wrote", out)
