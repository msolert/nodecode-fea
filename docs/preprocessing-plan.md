# Plan: preprocessing (parts, assembly, instances)

Goal: build Abaqus models from the node graph the same way results are post-processed today: create parts,
place them in the assembly as instances, move and rotate them, and repeat a part many times, live in CAE.
Later phases go on to mesh, materials, steps, loads, jobs, and close the loop back to the results nodes.

The node rules in `CLAUDE.md` apply in full: few general nodes, every setting an input, lists flow (a list of
positions gives a list of instances), nothing breaks saved graphs.

## How it fits the current design

| | Post-processing (today) | Preprocessing (new) |
| --- | --- | --- |
| Data flows as | values computed in the editor (fields, curves) | a **model description**: what to build, computed in the editor |
| In CAE | the script shows results | the script builds the model in `mdb` |
| Without Abaqus | everything works on the demo ODB | the editor previews the parts and assembly in 3D |
| Program | Abaqus/Viewer or CAE | Abaqus/CAE (Auto launch already picks CAE for graphs with **Model** nodes) |

So the engine stays as it is: model nodes return plain Python descriptions (`Part`, `Instance`, `Model`
values), the editor previews them, and `cae_export` turns them into kernel commands. Nothing needs Abaqus to
evaluate, and every node is testable against the fake `mdb`.

**Rebuilding in CAE (decided).** Only what changed is rebuilt. Each Model / Part / Instance value is one CAE
object whose fingerprint is a hash of the value, and a value holds everything upstream of it (a part its model
and sketch, an instance its part), so a change upstream changes every fingerprint downstream and nothing else.
The generated script keeps the fingerprints in the CAE session (`cae_model/`), deletes and recreates objects
whose fingerprint changed or that went missing, and deletes objects whose node was removed. Delete-and-recreate
rather than editing features in place keeps geometry-based regions (sets, partitions, seeds) correct: they are
downstream, so they are rebuilt too.

## New port types

- **model** (the root everything else hangs off: parts now, materials, steps... later),
- **sketch** (a flat profile: rectangles, circles, polylines; loops inside loops are holes),
- **part** (geometry definition: the model, the profile, body and method),
- **instance** (a part placed in the assembly: translation, rotation).
- **region** (faces, edges, vertices or cells on a part/instance; optionally a named CAE Set or Surface).

They get colours like the existing types. Lists flow as usual: a list of instances is many instances.

## Nodes (category **Model**)

The model is the root, in Abaqus's own order: **Model → Sketch → Part → Instance** (done), then Transform.

1. **Model** (done): *start a model*. Its output goes into Part (and later Material, Step...); objects built on a
   part get the model from the part, so it isn't wired everywhere. A Part without a Model goes into `NoCode-Model`.
2. **Sketch** (done): *draw a flat profile*: rectangle, circle, arc, spline or polyline through points (typed, matrix, curve);
   *Add* combines sketches, a loop inside a closed loop is a hole. Previewed in the editor; shown in CAE's Sketch
   module when clicked. Later: sketch fillets and offsets.
3. **Part** (done): *make a part from a profile*: Shell / Wire / Solid by Planar / Extrude / Revolve; 3D points
   make a wire frame. Done since: *Modelling space* (2D planar, axisymmetric), *Type* (discrete and analytical
   rigid), Import CAD (STEP / IGES / SAT, an input file's mesh).
4. **Instance** (done: one instance per part, rotate about an axis through the origin, then move). Later:
   *Where* with a list of positions or a matrix (one instance per row), *Repeat* (linear / circular patterns),
   dependent / independent.
5. **Transform**: *move existing instances*. Translate, rotate, or place by an assembly constraint:
   face to face, edge to edge, coaxial, coincident point, chosen by picking in CAE.
   - It applies to instances from anywhere: Instance nodes, a list, or instances picked in CAE (extending Set's
     Pick in CAE to instances).
   - It is a separate node because "move what already exists" is a different verb from "place a new copy".
   - CAE: `translate`, `rotate`, `FaceToFace`, `EdgeToEdge`, `CoaxialAlign`, `CoincidentPoint`.

Picking in CAE grows to faces, edges and cells of instances. It is then used to define surfaces and sets for
contacts, loads and boundary conditions (phase 3).

## Phases

1. **Foundations** (no visible nodes yet):
   - new value types and port types;
   - the Model category, and `cae_model/` generating kernel code;
   - the fake `mdb` extended with `Model`, `ConstrainedSketch`, parts, `rootAssembly` and patterns, recording
     calls like the post-processing fake does;
   - the 3D model preview in the editor.
2. **Part + Model**:
   - Box, Cylinder, Tube, Plate, Extrude and Revolve from points, From file;
   - preview, and build in CAE (live);
   - write `.cae`.
3. **Instance + Transform**:
   - instances, several copies (repeat, or a list of positions), translate and rotate;
   - patterns;
   - picked instances, and constraint-based placing.
   - This covers "orient, move instances and instantiate parts several times".
4. **Regions** (done): picking/finding faces, edges, vertices and cells in CAE; reusable named part- or
   assembly-level Sets and side-aware Surfaces are options on the same Region node.
5. **Rest of the model** (each phase a few general nodes, by the same rules):
   - Material and Section (done: one node, solid/shell/beam, assigned to whole parts or regions);
   - Mesh (done: global/local seed, controls, element type and shape, generate, per part or region);
   - Step (done: static, implicit dynamic and frequency, with chain validation);
   - Load / Boundary condition (done: one node with fixed/displacement, force, pressure and gravity kinds);
   - Interaction (done: one node for tie, frictionless contact and frictional contact);
   - Job (write `.inp`, submit, monitor).
6. **Closing the loop**: the Job node outputs the ODB path into Open ODB. A For loop over a dimension then runs
   the model, the job and the results, giving parametric studies in one graph.

## Testing

- Each node is checked by evaluating the graph, by preview geometry checks (bounding boxes, instance counts,
  transforms), and by running the generated CAE script against the extended fake `mdb`.
- As for the link today, CAE errors reported from CAE 2026 get reproduced in the fake first, then fixed.

## Decisions from the owner

- Model comes first and feeds Part; parts appear live in CAE as they are added.
- Sketch is its own node; Part takes its profile from a Sketch (no built-in shapes on Part).
- Parts are never placed in the assembly automatically: that is the Instance node.
- Changes rebuild only what is downstream of them, not the whole model.
- Old Part / Model graphs (before Sketch and Instance) are not migrated.

## Remaining implementation roadmap

Instance patterns are deferred: lists wired into Instance settings already provide the important repetition and
parametric cases without another operation. The remaining work is ordered by modelling value and dependency:

1. **Boolean assembly operations**: extend Merge Instances with Merge and Cut. Both accept lists, preserve their
   source instances, and return a Part that can be placed explicitly with Instance.
2. **Generic Datum**: extend the existing node with principal/offset planes, three-point planes, face-offset
   planes, axes and points. Geometry and points are inputs; all settings remain wireable.
3. **Part Feature** (in progress): sketch extrude/cut and fillet/chamfer are done. Next are holes and shell/remove
   face. Every operation remains an independently deletable CAE feature.
4. **Geometry queries** (in progress): tolerance-aware nearest lookup and actual unique match-count feedback are done.
   Next are ambiguity errors and visible markers for failed locations. Region remains the single selection node.
5. **Assembly placement constraints**: coincident point, face-to-face, edge-to-edge and coaxial alignment. Initial
   placement stays on Instance; operations on existing instances use one generic assembly transform operation.
6. **CAD import**: extend Part for STEP, IGES, SAT/ACIS and orphan meshes, including lists of files and names.
7. **Job** (done): create/write input, submit, optionally wait for completion, and return the resulting ODB path
   to Open ODB. A future live status control can add kill/restart without adding another node.
8. **Parametric execution**: map dimensions/materials into Jobs and collect the resulting ODB values as normal
   lists, tables and curves; no separate parameter-study node unless orchestration cannot express it.
9. **Exact CAE tree synchronization**: focus and expand the precise model object represented by the selected node,
   and surface suppressed/deleted/failed CAE state in the editor.

Each numbered item is a focused commit and must pass the complete backend suite and frontend build. New variants
extend these generic nodes rather than increasing the palette.
