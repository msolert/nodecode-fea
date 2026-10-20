import type { Edge, Node } from "@xyflow/react";
import type { OpNodeData, ParamValue } from "./types";
import solved from "./solvedExamples.json";

type Graph = { nodes: Node<OpNodeData>[]; edges: Edge[] };

const node = (id: string, type: string, x: number, y: number, params: Record<string, ParamValue> = {}): Node<OpNodeData> => ({
  id,
  type: "op",
  position: { x, y },
  // an Input's Name shows in its header, so each example's inputs say what they are
  data: { type, params, ...(type === "input.value" && params.label ? { name: String(params.label) } : {}) },
});

const edge = (source: string, sourceHandle: string, target: string, targetHandle: string): Edge => ({
  id: `${source}.${sourceHandle}-${target}.${targetHandle}`,
  source,
  sourceHandle,
  target,
  targetHandle,
});

/** A Find at Region: a typed point (an Input) wired into a Region on `source` (an instance unless `part`). */
function findAt(id: string, source: string, kind: string, point: string, x: number, y: number, part = false): Graph {
  return {
    nodes: [
      node(`${id}-at`, "input.value", x - 300, y, { type: "Text", value: point, label: `Point on ${id} (x, y, z)` }),
      node(id, "model.region", x, y, { method: "Find at", kind }),
    ],
    edges: [edge(source, part ? "part" : "instance", id, "source"), edge(`${id}-at`, "value", id, "points")],
  };
}

function join(...parts: Graph[]): Graph {
  return { nodes: parts.flatMap((p) => p.nodes), edges: parts.flatMap((p) => p.edges) };
}

const wires = (...edges: Edge[]): Graph => ({ nodes: [], edges });

/** A 100 x 10 x 10 meshed bar in `model` (ids prefixed by `p`), with one instance per x offset (`<p>i1`, …). */
function bar(p: string, model: string, x: number, y: number, offsets: number[]): Graph {
  return {
    nodes: [
      node(`${p}sketch`, "model.sketch", x, y, { name: "Bar", width: 100, height: 10 }),
      node(`${p}part`, "model.part", x + 300, y, { name: "Bar", body: "Solid", method: "Extrude", depth: 10 }),
      node(`${p}sec`, "model.section", x + 600, y, {}),
      node(`${p}mesh`, "model.mesh", x + 900, y, { size: 5, element: "C3D8R" }),
      ...offsets.map((dx, i) =>
        node(`${p}i${i + 1}`, "model.instance", x + 1200, y + i * 220, { name: `Bar-${i + 1}`, x: dx }),
      ),
    ],
    edges: [
      edge(model, "model", `${p}sketch`, "model"),
      edge(`${p}sketch`, "sketch", `${p}part`, "profile"),
      edge(`${p}part`, "part", `${p}sec`, "target"),
      edge(`${p}sec`, "part", `${p}mesh`, "target"),
      ...offsets.map((_, i) => edge(`${p}mesh`, "part", `${p}i${i + 1}`, "part")),
    ],
  };
}

/** Every Sketch, Part and first Step wired to the example's Model, so nothing is built in NoCode-Model beside it
 * (the sketches added into another one too) and the wire shows which model each goes into. */
function inModel(g: Graph): Graph {
  const model = g.nodes.find((n) => n.data.type === "model.model");
  if (!model) return g;
  const wired = new Set(g.edges.map((e) => `${e.target}|${e.targetHandle}`));
  const port: Record<string, string> = { "model.sketch": "model", "model.part": "model", "model.step": "after" };
  const added = g.nodes
    .filter((n) => port[n.data.type] && !wired.has(`${n.id}|${port[n.data.type]}`))
    .map((n) => edge(model.id, "model", n.id, port[n.data.type]));
  return { nodes: g.nodes, edges: [...g.edges, ...added] };
}

/** One physical telescope model. Loads carry their values across the three linked steps, so a single job writes
 * all load cases to one ODB. */
function telescopeModel(): Graph {
  const model = "mirror-model";
  const sketch = "mirror-hex";
  const part = "mirror-part";
  const back = "mirror-back";
  const padRest = "mirror-pad-rest";
  const padZero = "mirror-pad-zero";
  const partition = "mirror-partition";
  const section = "mirror-section";
  const mesh = "mirror-mesh";
  const instances = "mirror-instances";
  const nodes: Node<OpNodeData>[] = [
    node(model, "model.model", 2100, 0, { name: "Telescope-Mirror" }),
    node(sketch, "model.sketch", 2400, 0, { name: "Segment", shape: "Polygon", sides: 6 }),
    node(part, "model.part", 2700, 0, { name: "Segment", body: "Solid", method: "Extrude" }),
    node(back, "model.region", 3000, -300, { method: "Find at", kind: "Face" }),
    node(padRest, "model.sketch", 3000, 340, { name: "Support pads", shape: "Circle" }),
    node(padZero, "model.sketch", 3300, 340, { name: "Support pads", shape: "Circle" }),
    node(partition, "model.feature", 3300, 0, { operation: "Partition by a sketch on a face", name: "Support pads" }),
    node(section, "model.section", 3600, 0, {
      material: "Zerodur", section: "Zerodur solid", behavior: "Elastic", young: 90300, poisson: 0.24,
    }),
    node(mesh, "model.mesh", 3900, 0, { element: "C3D8R" }),
    node(instances, "model.instance", 4200, 0, { name: "Segment" }),
    node("mirror-support-regions", "model.region", 4500, 0, {
      method: "By shape", shape: "Circular faces", match: "Equal to", tolerance: 0.01,
      members: "One per geometry",
    }),
    node("mirror-couplings", "model.interaction", 4800, 0, {
      name: "Support", kind: "Distributing coupling", dofs: "U1, U2, U3",
      point_from: "Slave Region point",
    }),
    node("mirror-connectors", "model.interaction", 5100, 0, {
      name: "Ground", kind: "Connector", connector: "Beam", second: "Ground",
    }),
    node("zenith-step", "model.step", 4500, -760, {
      name: "Zenith", field: "U, S, CTF", intervals: 1,
    }),
    node("horizon-step", "model.step", 4800, -760, {
      name: "Horizon", field: "U, S, CTF", intervals: 1,
    }),
    node("wind-step", "model.step", 5100, -760, {
      name: "Wind", field: "U, S, CTF", intervals: 1,
    }),
    node("gravity-load", "model.condition", 5400, -1000, {
      name: "Gravity", kind: "Gravity", x: "0; Horizon: -9810", z: "-9810; Horizon: 0", active: "Zenith to Horizon",
    }),
    node("mirror-top", "model.region", 4500, -420, {
      method: "By shape", shape: "Flat faces", facing: "0, 0, 1",
    }),
    node("wind-load", "model.condition", 5700, -760, { name: "Trapezoidal wind", kind: "Pressure", active: "Wind" }),
    node("mirror-job", "model.job", 6000, -760, { name: "Telescope-Mirror" }),
  ];
  const edges: Edge[] = [
    edge(model, "model", sketch, "model"),
    edge("hex-radius", "value", sketch, "param:radius"),
    edge(sketch, "sketch", part, "profile"),
    edge(model, "model", part, "model"),
    edge("thickness", "value", part, "param:depth"),
    edge(part, "part", back, "source"),
    edge("origin", "value", back, "points"),
    edge(model, "model", padRest, "model"),
    edge("pad-local-x", "value", padRest, "param:x"),
    edge("pad-local-y", "value", padRest, "param:y"),
    edge("pad-radius", "value", padRest, "param:radius"),
    edge(model, "model", padZero, "model"),
    edge("support-radius", "value", padZero, "param:x"),
    edge("pad-radius", "value", padZero, "param:radius"),
    edge(padRest, "sketch", padZero, "add"),
    edge(part, "part", partition, "part"),
    edge(back, "region", partition, "region"),
    edge(padZero, "sketch", partition, "sketch"),
    edge(partition, "part", section, "target"),
    edge("density", "value", section, "param:density"),
    edge(section, "part", mesh, "target"),
    edge("mesh-size", "value", mesh, "param:size"),
    edge(mesh, "part", instances, "part"),
    edge("center-x", "value", instances, "param:x"),
    edge("center-y", "value", instances, "param:y"),
    edge(instances, "instance", "mirror-support-regions", "source"),
    edge("pad-radius", "value", "mirror-support-regions", "param:radius"),
    edge("mirror-support-regions", "region", "mirror-couplings", "slave"),
    edge("mirror-couplings", "interaction", "mirror-connectors", "master"),
    edge(model, "model", "zenith-step", "after"),
    edge("zenith-step", "step", "horizon-step", "after"),
    edge("horizon-step", "step", "wind-step", "after"),
    edge("zenith-step", "step", "gravity-load", "step"),
    edge(instances, "instance", "mirror-top", "source"),
    edge("gravity-load", "condition", "wind-load", "step"),
    edge("mirror-top", "region", "wind-load", "region"),
    edge("wind-pressure", "value", "wind-load", "param:magnitude"),
    edge("wind-profile", "text", "wind-load", "distribution"),
    edge("wind-step", "step", "mirror-job", "analysis"),
  ];

  return { nodes, edges };
}

/** Segmented circular mirror: vector → axial-coordinate matrix → exact XY packing, three supports per segment,
 * one three-step job and ready-wired result reductions. */
function telescopeMirror(): Graph {
  const common: Graph = {
    nodes: [
      node("mirror-diameter", "input.value", -900, -800, { type: "Number", value: 1000, label: "Mirror diameter (mm)" }),
      node("hex-diameter", "input.value", -900, -610, { type: "Number", value: 180, label: "Hex flat-to-flat (mm)" }),
      node("thickness", "input.value", -900, -420, { type: "Number", value: 25, label: "Segment thickness (mm)" }),
      node("support-diameter", "input.value", -900, -230, { type: "Number", value: 90, label: "Support circle diameter (mm)" }),
      node("pad-diameter", "input.value", -900, -40, { type: "Number", value: 18, label: "Support pad diameter (mm)" }),
      node("mesh-size", "input.value", -900, 150, { type: "Number", value: 25, label: "Mesh size (mm)" }),
      node("density", "input.value", -900, 340, { type: "Number", value: 2.53e-9, label: "Zerodur density (tonne/mm³)" }),
      node("wind-pressure", "input.value", -900, 530, { type: "Number", value: 0.001, label: "Peak wind pressure (MPa)" }),
      node("span", "math.formula", -600, -800, {
        expr: "np.arange(-ceil(a / b) - 1, ceil(a / b) + 2)", label: "Axial coordinate vector",
      }),
      node("q-vector", "math.formula", -300, -900, { expr: "np.tile(a, len(a))", label: "q vector" }),
      node("r-vector", "math.formula", -300, -700, { expr: "np.repeat(a, len(a))", label: "r vector" }),
      node("qr-matrix", "math.formula", 0, -800, { expr: "np.stack([a, b], axis=1)", label: "q-r matrix" }),
      node("candidate-x", "math.formula", 300, -900, {
        expr: "b * (a[:, 0] + a[:, 1] / 2)", label: "Candidate x",
      }),
      node("candidate-y", "math.formula", 300, -700, {
        expr: "sqrt(3) * b * a[:, 1] / 2", label: "Candidate y",
      }),
      node("inside", "math.formula", 600, -800, {
        expr: "np.max(sqrt((a + np.outer(cos((90 + 60 * np.arange(6)) * pi / 180), c / sqrt(3))) ** 2 + (b + np.outer(sin((90 + 60 * np.arange(6)) * pi / 180), c / sqrt(3))) ** 2), axis=0) <= d / 2",
        label: "All six vertices inside aperture",
      }),
      node("center-x", "math.formula", 900, -900, { expr: "a[np.where(b)[0]]", label: "Segment x" }),
      node("center-y", "math.formula", 900, -700, { expr: "a[np.where(b)[0]]", label: "Segment y" }),
      node("centers", "math.formula", 1200, -800, { expr: "np.stack([a, b], axis=1)", label: "XY center matrix" }),
      node("segment-count", "list.length", 1500, -800, {}),
      node("count-view", "view.value", 1800, -800, { label: "Full hexagon segments" }),
      node("hex-radius", "math.formula", -600, -500, { expr: "a / sqrt(3)", label: "Hex corner radius" }),
      node("support-radius", "math.formula", -600, -260, { expr: "a / 2", label: "Support radius" }),
      node("pad-radius", "math.formula", -600, -40, { expr: "a / 2", label: "Pad radius" }),
      node("pad-angles", "input.value", -600, 180, {
        type: "List / matrix", value: "[120, 240]", label: "Other support angles (°)",
      }),
      node("pad-local-x", "math.formula", -300, 100, {
        expr: "a / 2 * cos(b * pi / 180)", label: "Local pad x",
      }),
      node("pad-local-y", "math.formula", -300, 300, {
        expr: "a / 2 * sin(b * pi / 180)", label: "Local pad y",
      }),
      node("origin", "input.value", 0, 500, { type: "Text", value: "0, 0, 0", label: "Back face centre" }),
      node("own-weight", "math.formula", 1500, -540, {
        expr: "a * sqrt(3) / 2 * b ** 2 * c * d * 9810", label: "Mirror weight",
      }),
      node("weight-view", "view.value", 1800, -540, { label: "Own weight", unit: "N" }),
      node("wind-profile", "text.format", 1200, 420, {
        template: "max(0.4, min(1.0, 0.7 + 1.2 * X / {a}))",
      }),
    ],
    edges: [
      edge("mirror-diameter", "value", "span", "a"),
      edge("hex-diameter", "value", "span", "b"),
      edge("span", "value", "q-vector", "a"),
      edge("span", "value", "r-vector", "a"),
      edge("q-vector", "value", "qr-matrix", "a"),
      edge("r-vector", "value", "qr-matrix", "b"),
      edge("qr-matrix", "value", "candidate-x", "a"),
      edge("hex-diameter", "value", "candidate-x", "b"),
      edge("qr-matrix", "value", "candidate-y", "a"),
      edge("hex-diameter", "value", "candidate-y", "b"),
      edge("candidate-x", "value", "inside", "a"),
      edge("candidate-y", "value", "inside", "b"),
      edge("hex-diameter", "value", "inside", "c"),
      edge("mirror-diameter", "value", "inside", "d"),
      edge("candidate-x", "value", "center-x", "a"),
      edge("inside", "value", "center-x", "b"),
      edge("candidate-y", "value", "center-y", "a"),
      edge("inside", "value", "center-y", "b"),
      edge("center-x", "value", "centers", "a"),
      edge("center-y", "value", "centers", "b"),
      edge("center-x", "value", "segment-count", "list"),
      edge("segment-count", "value", "count-view", "value"),
      edge("hex-diameter", "value", "hex-radius", "a"),
      edge("support-diameter", "value", "support-radius", "a"),
      edge("pad-diameter", "value", "pad-radius", "a"),
      edge("support-diameter", "value", "pad-local-x", "a"),
      edge("pad-angles", "value", "pad-local-x", "b"),
      edge("support-diameter", "value", "pad-local-y", "a"),
      edge("pad-angles", "value", "pad-local-y", "b"),
      edge("segment-count", "value", "own-weight", "a"),
      edge("hex-diameter", "value", "own-weight", "b"),
      edge("thickness", "value", "own-weight", "c"),
      edge("density", "value", "own-weight", "d"),
      edge("own-weight", "value", "weight-view", "value"),
      edge("mirror-diameter", "value", "wind-profile", "a"),
    ],
  };

  const results: Graph = {
    nodes: [
      node("mirror-odb", "odb.open", 6300, -760, {}),
      node("zenith-force", "odb.field", 6600, -980, {
        variable: "CTF", quantity: "Magnitude", step: "Zenith",
      }),
      node("horizon-force", "odb.field", 6600, -720, {
        variable: "CTF", quantity: "Magnitude", step: "Horizon",
      }),
      node("zenith-total", "math.reduce", 6900, -980, { op: "Sum" }),
      node("horizon-total", "math.reduce", 6900, -720, { op: "Sum" }),
      node("gravity-totals", "list.merge", 7200, -850, {}),
      node("force-view", "view.value", 7500, -980, { label: "Zenith / horizon support force", unit: "N" }),
      node("weight-ratio", "math.formula", 7500, -720, { expr: "a / b", label: "Force / own weight" }),
      node("ratio-view", "view.value", 7800, -720, { label: "Zenith / horizon force ÷ own weight" }),
      node("wind-u", "odb.field", 6600, -420, { variable: "U", quantity: "Magnitude", step: "Wind" }),
      node("wind-s", "odb.field", 6600, -160, { variable: "S", quantity: "Mises", step: "Wind" }),
      node("wind-u-max", "math.reduce", 6900, -420, { op: "Max" }),
      node("wind-s-max", "math.reduce", 6900, -160, { op: "Max" }),
      node("wind-u-view", "view.value", 7200, -420, { label: "Maximum wind deflection", unit: "mm" }),
      node("wind-s-view", "view.value", 7200, -160, { label: "Maximum wind von Mises", unit: "MPa" }),
    ],
    edges: [
      edge("mirror-job", "path", "mirror-odb", "path"),
      edge("mirror-odb", "odb", "zenith-force", "odb"),
      edge("mirror-odb", "odb", "horizon-force", "odb"),
      edge("zenith-force", "field", "zenith-total", "field"),
      edge("horizon-force", "field", "horizon-total", "field"),
      edge("zenith-total", "value", "gravity-totals", "a"),
      edge("horizon-total", "value", "gravity-totals", "b"),
      edge("gravity-totals", "list", "force-view", "value"),
      edge("gravity-totals", "list", "weight-ratio", "a"),
      edge("own-weight", "value", "weight-ratio", "b"),
      edge("weight-ratio", "value", "ratio-view", "value"),
      edge("mirror-odb", "odb", "wind-u", "odb"),
      edge("mirror-odb", "odb", "wind-s", "odb"),
      edge("wind-u", "field", "wind-u-max", "field"),
      edge("wind-s", "field", "wind-s-max", "field"),
      edge("wind-u-max", "value", "wind-u-view", "value"),
      edge("wind-s-max", "value", "wind-s-view", "value"),
    ],
  };
  return join(common, telescopeModel(), results);
}

/** Modelling examples (opened in the Modelling view): each builds its own CAE model, ready for the live link or a Job. */
const EXAMPLES: { title: string; build: () => Graph }[] = [
  {
    title: "Segmented telescope mirror: one model, three load steps",
    build: telescopeMirror,
  },
  {
    title: "Bolted bars: couplings, bushing connector, general contact",
    build: () =>
      join(
        {
          nodes: [
            node("m", "model.model", -300, 0, { name: "Bolted" }),
            node("step", "model.step", 1500, -340, { name: "Pull", field: "U, S, RF", history: "CTF1, CU1" }),
            node("fix", "model.condition", 2400, -300, { name: "Fix", kind: "Fixed" }),
            node("near", "model.interaction", 2100, 40, { name: "Near", kind: "Kinematic coupling", point: "105, 5, 5" }),
            node("far", "model.interaction", 2100, 300, { name: "Far", kind: "Kinematic coupling", point: "115, 5, 5" }),
            node("end", "model.interaction", 2100, 560, { name: "End", kind: "Kinematic coupling", point: "230, 5, 5" }),
            node("bolt", "model.interaction", 2400, 160, {
              name: "Bolt", kind: "Connector", connector: "Bushing", stiffness: "1000, 1000, 1000, 1e5, 1e5, 1e5",
            }),
            node("touch", "model.interaction", 2400, 440, { name: "Touch", kind: "General contact", friction: 0.2 }),
            node("pull", "model.condition", 2700, 0, { name: "Pull", kind: "Concentrated force", x: 1000, y: 0, z: 0 }),
            node("job", "model.job", 3000, 160, { name: "Bolted" }),
          ],
          edges: [
            edge("m", "model", "step", "after"),
            edge("step", "step", "fix", "step"),
            edge("near", "interaction", "bolt", "master"),
            edge("far", "interaction", "bolt", "slave"),
            edge("bolt", "interaction", "step", "history_region"),
            edge("step", "step", "touch", "step"),
            edge("fix", "condition", "pull", "step"),
            edge("end", "interaction", "pull", "region"),
            edge("pull", "condition", "job", "analysis"),
          ],
        },
        bar("b", "m", 0, 0, [0, 120]),
        findAt("fixed", "bi1", "Face", "0, 5, 5", 1800, -300),
        findAt("face1", "bi1", "Face", "100, 5, 5", 1800, 40),
        findAt("face2", "bi2", "Face", "120, 5, 5", 1800, 300),
        findAt("face3", "bi2", "Face", "220, 5, 5", 1800, 560),
        wires(
          edge("fixed", "region", "fix", "region"),
          edge("face1", "region", "near", "slave"),
          edge("face2", "region", "far", "slave"),
          edge("face3", "region", "end", "slave"),
        ),
      ),
  },
  {
    title: "Remote load: coupling to a point, force and history there",
    build: () =>
      join(
        {
          nodes: [
            node("m", "model.model", -300, 0, { name: "Remote" }),
            node("rp", "model.interaction", 2100, 200, { name: "Grip", kind: "Kinematic coupling", point: "130, 5, 5" }),
            node("step", "model.step", 2400, -220, { name: "Bend", field: "U, S", history: "U2, RF2" }),
            node("fix", "model.condition", 2700, -220, { name: "Fix", kind: "Fixed" }),
            node("load", "model.condition", 3000, 0, { name: "Lift", kind: "Concentrated force", x: 0, y: -500, z: 0 }),
            node("job", "model.job", 3300, 0, { name: "Remote" }),
          ],
          edges: [
            edge("m", "model", "step", "after"),
            edge("rp", "interaction", "step", "history_region"),
            edge("step", "step", "fix", "step"),
            edge("fix", "condition", "load", "step"),
            edge("rp", "interaction", "load", "region"),
            edge("load", "condition", "job", "analysis"),
          ],
        },
        bar("b", "m", 0, 0, [0]),
        findAt("root", "bi1", "Face", "0, 5, 5", 1800, -220),
        findAt("tip", "bi1", "Face", "100, 5, 5", 1800, 200),
        wires(edge("root", "region", "fix", "region"), edge("tip", "region", "rp", "slave")),
      ),
  },
  {
    title: "Pressure varying over space and time (distribution + amplitude)",
    build: () =>
      join(
        {
          nodes: [
            node("m", "model.model", -300, 0, { name: "Varying" }),
            node("s", "model.sketch", 0, 0, { name: "Block", width: 200, height: 40 }),
            node("p", "model.part", 300, 0, { name: "Block", body: "Solid", method: "Extrude", depth: 40 }),
            node("sec", "model.section", 600, 0, {}),
            node("mesh", "model.mesh", 900, 0, { size: 8 }),
            node("i", "model.instance", 1200, 0, {}),
            node("step", "model.step", 1500, -300, { name: "Load", intervals: 20, field: "U, S" }),
            node("fix", "model.condition", 2100, -260, { name: "Base", kind: "Fixed" }),
            node("ramp", "input.value", 2100, 380, {
              type: "List / matrix", label: "Amplitude: time, factor", value: "[[0, 0], [0.4, 1], [0.7, 1], [1, 0.3]]",
            }),
            node("press", "model.condition", 2400, 0, {
              name: "Wind", kind: "Pressure", magnitude: 2, amplitude_shape: "Smooth step",
              distribution: "1 + 2 * X / 200 + 0.5 * sin(pi * Z / 40)",
            }),
            node("job", "model.job", 2700, 0, { name: "Varying" }),
          ],
          edges: [
            edge("m", "model", "s", "model"),
            edge("s", "sketch", "p", "profile"),
            edge("p", "part", "sec", "target"),
            edge("sec", "part", "mesh", "target"),
            edge("mesh", "part", "i", "part"),
            edge("m", "model", "step", "after"),
            edge("step", "step", "fix", "step"),
            edge("fix", "condition", "press", "step"),
            edge("ramp", "value", "press", "amplitude"),
            edge("press", "condition", "job", "analysis"),
          ],
        },
        findAt("base", "i", "Face", "100, 0, 20", 1800, -260),
        findAt("top", "i", "Face", "100, 40, 20", 1800, 120),
        wires(edge("base", "region", "fix", "region"), edge("top", "region", "press", "region")),
      ),
  },
  {
    title: "Load mapped from another analysis' results",
    build: () =>
      join(
        {
          nodes: [
            node("m", "model.model", -300, 0, { name: "Mapped" }),
            node("odb", "odb.open", 1500, 420, { path: "mock://cantilever?load=700" }),
            node("s", "odb.field", 1800, 420, { variable: "S", quantity: "Mises" }),
            node("step", "model.step", 1500, -300, { name: "Load" }),
            node("fix", "model.condition", 2100, -260, { name: "Root", kind: "Fixed" }),
            node("press", "model.condition", 2400, 0, { name: "From-results", kind: "Pressure", magnitude: 0.01 }),
          ],
          edges: [
            edge("m", "model", "step", "after"),
            edge("step", "step", "fix", "step"),
            edge("odb", "odb", "s", "odb"),
            edge("fix", "condition", "press", "step"),
            edge("s", "field", "press", "distribution"),
          ],
        },
        bar("b", "m", 0, 0, [0]),
        findAt("root", "bi1", "Face", "0, 5, 5", 1800, -260),
        findAt("top", "bi1", "Face", "50, 10, 5", 1800, 120),
        wires(edge("root", "region", "fix", "region"), edge("top", "region", "press", "region")),
      ),
  },
  {
    title: "Local mesh refinement and mesh quality",
    build: () =>
      join(
        {
          nodes: [
            node("m", "model.model", -300, 0, { name: "Refined" }),
            node("s", "model.sketch", 0, 0, { name: "Block", width: 100, height: 50 }),
            node("p", "model.part", 300, 0, { name: "Block", body: "Solid", method: "Extrude", depth: 50 }),
            node("coarse", "model.mesh", 600, 0, { size: 10, generate: "No" }),
            node("graded", "model.mesh", 1200, 0, { seed: "Biased", number: 20, ratio: 5, bias: "Fine at both ends" }),
            node("nodes", "model.measure", 1500, -320, { quantity: "Nodes" }),
            node("elements", "model.measure", 1500, -120, { quantity: "Elements" }),
            node("worst", "model.measure", 1500, 80, { quantity: "Worst aspect ratio" }),
            node("failed", "model.measure", 1500, 280, { quantity: "Failed elements" }),
            node("i", "model.instance", 1500, 480, {}),
          ],
          edges: [
            edge("m", "model", "s", "model"),
            edge("s", "sketch", "p", "profile"),
            edge("p", "part", "coarse", "target"),
            edge("graded", "part", "nodes", "first"),
            edge("graded", "part", "elements", "first"),
            edge("graded", "part", "worst", "first"),
            edge("graded", "part", "failed", "first"),
            edge("graded", "part", "i", "part"),
          ],
        },
        findAt("edge", "coarse", "Edge", "50, 0, 0", 900, 160, true),
        wires(edge("edge", "region", "graded", "target")),
      ),
  },
  {
    title: "Mesh convergence: one mesh per seed size",
    build: () => ({
      nodes: [
        node("m", "model.model", -300, 0, { name: "Convergence" }),
        node("sizes", "loop.for", 300, 260, { by: "Count", start: 20, stop: 2.5, count: 4, spacing: "Geometric" }),
        node("s", "model.sketch", 0, 0, { name: "Block", width: 100, height: 20 }),
        node("p", "model.part", 300, 0, { name: "Block", body: "Solid", method: "Extrude", depth: 20 }),
        node("mesh", "model.mesh", 600, 0, {}),
        node("count", "model.measure", 900, -120, { quantity: "Elements" }),
        node("ratio", "model.measure", 900, 120, { quantity: "Mean aspect ratio" }),
      ],
      edges: [
        edge("m", "model", "s", "model"),
        edge("s", "sketch", "p", "profile"),
        edge("p", "part", "mesh", "target"),
        edge("sizes", "i", "mesh", "param:size"),
        edge("mesh", "part", "count", "first"),
        edge("mesh", "part", "ratio", "first"),
      ],
    }),
  },
  {
    title: "Bolt-hole flange from a sequence (For + Formula + Sketch shapes)",
    build: () => ({
      nodes: [
        node("m", "model.model", -300, 0, { name: "Flange" }),
        node("i", "loop.for", 0, 300, { by: "Count", start: 0, stop: 300, count: 6 }),
        node("x", "math.formula", 300, 200, { expr: "100 + 65 * cos(a * pi / 180)", label: "Hole x" }),
        node("y", "math.formula", 300, 420, { expr: "100 + 65 * sin(a * pi / 180)", label: "Hole y" }),
        node("holes", "model.sketch", 600, 300, { name: "Holes", shape: "Circle", radius: 8 }),
        node("slots", "model.sketch", 600, 560, {
          name: "Slot", shape: "Slot", x: 100, y: 18, width: 40, height: 10,
        }),
        node("bore", "model.sketch", 600, 40, { name: "Bore", shape: "Polygon", x: 100, y: 100, radius: 30, sides: 6 }),
        node("plate", "model.sketch", 900, 160, {
          name: "Flange", shape: "Rectangle", width: 200, height: 200, corner: 25,
        }),
        node("part", "model.part", 1200, 160, { name: "Flange", body: "Solid", method: "Extrude", depth: 15 }),
        node("inst", "model.instance", 1500, 160, {}),
      ],
      edges: [
        edge("m", "model", "plate", "model"),
        edge("i", "i", "x", "a"),
        edge("i", "i", "y", "a"),
        edge("x", "value", "holes", "param:x"),
        edge("y", "value", "holes", "param:y"),
        edge("holes", "sketch", "plate", "add"),
        edge("bore", "sketch", "plate", "add"),
        edge("slots", "sketch", "plate", "add"),
        edge("plate", "sketch", "part", "profile"),
        edge("part", "part", "inst", "part"),
      ],
    }),
  },
  {
    title: "Find every bolt hole By shape: list them (count, radius, center) and load them",
    build: () => ({
      nodes: [
        node("m", "model.model", -300, 0, { name: "BoltCircle" }),
        node("i", "loop.for", -300, 300, { by: "Count", start: 0, stop: 315, count: 8 }),
        node("x", "math.formula", 0, 220, { expr: "120 + 90 * cos(a * pi / 180)", label: "Hole x" }),
        node("y", "math.formula", 0, 420, { expr: "120 + 90 * sin(a * pi / 180)", label: "Hole y" }),
        node("holes", "model.sketch", 300, 300, { name: "Holes", shape: "Circle", radius: 6 }),
        node("bore", "model.sketch", 300, 40, { name: "Bore", shape: "Circle", x: 120, y: 120, radius: 40 }),
        node("disc", "model.sketch", 600, 160, { name: "Flange", shape: "Circle", x: 120, y: 120, radius: 120 }),
        node("part", "model.part", 900, 160, { name: "Flange", body: "Solid", method: "Extrude", depth: 12 }),
        node("sec", "model.section", 1200, 160, {}),
        node("mesh", "model.mesh", 1500, 160, { size: 6 }),
        node("inst", "model.instance", 1800, 160, {}),
        node("bolts", "model.region", 2100, 60, {
          method: "By shape", shape: "Cylindrical faces", radius: 6, tolerance: 0.01, create: "Surface",
          name: "Bolt-Holes",
        }),
        node("bores", "model.region", 2100, 420, {
          method: "By shape", shape: "Cylindrical faces", radius: 40, tolerance: 0.01,
        }),
        node("count", "model.measure", 2400, -320, { quantity: "Count" }),
        node("radius", "model.measure", 2400, -170, { quantity: "Radius" }),
        node("center", "model.measure", 2400, -20, { quantity: "Center" }),
        node("step", "model.step", 2100, 700, { name: "Bolt-Load" }),
        node("fix", "model.condition", 2700, 420, { name: "Fix-Bore", kind: "Fixed" }),
        node("load", "model.condition", 2700, 120, { name: "Bolt-Pressure", kind: "Pressure", magnitude: 50 }),
      ],
      edges: [
        edge("m", "model", "disc", "model"),
        edge("i", "i", "x", "a"),
        edge("i", "i", "y", "a"),
        edge("x", "value", "holes", "param:x"),
        edge("y", "value", "holes", "param:y"),
        edge("holes", "sketch", "disc", "add"),
        edge("bore", "sketch", "disc", "add"),
        edge("disc", "sketch", "part", "profile"),
        edge("part", "part", "sec", "target"),
        edge("sec", "part", "mesh", "target"),
        edge("mesh", "part", "inst", "part"),
        edge("inst", "instance", "bolts", "source"),
        edge("inst", "instance", "bores", "source"),
        edge("bolts", "region", "count", "first"),
        edge("bolts", "region", "radius", "first"),
        edge("bolts", "region", "center", "first"),
        edge("m", "model", "step", "after"),
        edge("step", "step", "fix", "step"),
        edge("step", "step", "load", "step"),
        edge("bores", "region", "fix", "region"),
        edge("bolts", "region", "load", "region"),
      ],
    }),
  },
  {
    title: "Defeature: remove every hole up to a radius By shape (before meshing), see the volume change",
    build: () => ({
      nodes: [
        node("m", "model.model", -300, 0, { name: "Defeature" }),
        node("i", "loop.for", -300, 300, { by: "Count", start: 0, stop: 315, count: 8 }),
        node("x", "math.formula", 0, 220, { expr: "120 + 90 * cos(a * pi / 180)", label: "Hole x" }),
        node("y", "math.formula", 0, 420, { expr: "120 + 90 * sin(a * pi / 180)", label: "Hole y" }),
        node("holes", "model.sketch", 300, 300, { name: "Holes", shape: "Circle", radius: 6 }),
        node("bore", "model.sketch", 300, 40, { name: "Bore", shape: "Circle", x: 120, y: 120, radius: 40 }),
        node("disc", "model.sketch", 600, 160, { name: "Flange", shape: "Circle", x: 120, y: 120, radius: 120 }),
        node("part", "model.part", 900, 160, { name: "Flange", body: "Solid", method: "Extrude", depth: 12 }),
        node("small", "model.region", 1200, 0, {
          method: "By shape", shape: "Cylindrical faces", radius: 10, match: "Up to",
        }),
        node("count", "model.measure", 1500, -200, { quantity: "Count" }),
        node("defeature", "model.feature", 1500, 160, { operation: "Remove faces", name: "No-Small-Holes" }),
        node("sec", "model.section", 1800, 160, {}),
        node("mesh", "model.mesh", 2100, 160, { size: 6 }),
        node("inst", "model.instance", 2400, 160, {}),
      ],
      edges: [
        edge("m", "model", "disc", "model"),
        edge("i", "i", "x", "a"),
        edge("i", "i", "y", "a"),
        edge("x", "value", "holes", "param:x"),
        edge("y", "value", "holes", "param:y"),
        edge("holes", "sketch", "disc", "add"),
        edge("bore", "sketch", "disc", "add"),
        edge("disc", "sketch", "part", "profile"),
        edge("part", "part", "small", "source"),
        edge("small", "region", "count", "first"),
        edge("part", "part", "defeature", "part"),
        edge("small", "region", "defeature", "region"),
        edge("defeature", "part", "sec", "target"),
        edge("sec", "part", "mesh", "target"),
        edge("mesh", "part", "inst", "part"),
      ],
    }),
  },
  {
    title: "Assemble by aligning: a pin made coaxial with a plate's hole, then flush with its bottom",
    build: () => ({
      nodes: [
        node("m", "model.model", -300, 200, { name: "PinPlate" }),
        node("hole", "model.sketch", 0, 0, { name: "Hole", shape: "Circle", radius: 5 }),
        node("ps", "model.sketch", 300, 0, { name: "Plate", x: -50, y: -30, width: 100, height: 60, corner: 8 }),
        node("plate", "model.part", 600, 0, { name: "Plate", body: "Solid", method: "Extrude", depth: 10 }),
        node("pi", "model.instance", 900, 0, { name: "Plate-1" }),
        node("bore", "model.region", 1200, -120, { method: "By shape", shape: "Cylindrical faces", radius: 5, tolerance: 0.01 }),
        node("under", "model.region", 1200, 160, { method: "By shape", shape: "Flat faces", facing: "0, 0, -1" }),
        node("pin_s", "model.sketch", 0, 460, { name: "Pin", shape: "Circle", radius: 5 }),
        node("pin", "model.part", 300, 460, { name: "Pin", body: "Solid", method: "Extrude", depth: 40 }),
        node("shank", "model.region", 600, 380, { method: "By shape", shape: "Cylindrical faces" }),
        node("foot", "model.region", 600, 660, { method: "By shape", shape: "Flat faces", facing: "0, 0, -1" }),
        node("in", "model.instance", 1500, 300, { name: "Pin-1", x: 200, y: 30, z: 25, how: "Coaxial" }),
        node("flush", "model.instance", 1800, 300, { how: "Face to face", flip: "Yes" }),
      ],
      edges: [
        edge("hole", "sketch", "ps", "add"),
        edge("ps", "sketch", "plate", "profile"),
        edge("plate", "part", "pi", "part"),
        edge("pi", "instance", "bore", "source"),
        edge("pi", "instance", "under", "source"),
        edge("pin_s", "sketch", "pin", "profile"),
        edge("pin", "part", "shank", "source"),
        edge("pin", "part", "foot", "source"),
        edge("pin", "part", "in", "part"),
        edge("shank", "region", "in", "align"),
        edge("bore", "region", "in", "to"),
        edge("in", "instance", "flush", "part"),
        edge("foot", "region", "flush", "align"),
        edge("under", "region", "flush", "to"),
      ],
    }),
  },
  {
    title: "Bolted joint: bolt aligned into the holes, preloaded (bolt load), tied, contact, then pulled",
    build: () =>
      join(
        {
          nodes: [
            node("m", "model.model", -300, 300, { name: "Joint" }),
            node("hole", "model.sketch", 0, 0, { name: "Hole", shape: "Circle", radius: 6 }),
            node("ps", "model.sketch", 300, 0, { name: "Plate", x: -30, y: -20, width: 60, height: 40 }),
            node("plate", "model.part", 600, 0, { name: "Plate", body: "Solid", method: "Extrude", depth: 10 }),
            node("psec", "model.section", 900, 0, { material: "Steel", section: "Plate" }),
            node("pmesh", "model.mesh", 1200, 0, { size: 4 }),
            node("bottom", "model.instance", 1500, -120, { name: "Bottom" }),
            node("top", "model.instance", 1500, 160, { name: "Top", z: 10 }),
            node("bs", "model.sketch", 0, 700, {
              name: "Bolt", shape: "Profile", closed: "Closed",
              points: "0, 0; 10, 0; 10, 5; 6, 5; 6, 25; 10, 25; 10, 30; 0, 30",
            }),
            node("bolt", "model.part", 300, 700, { name: "Bolt", body: "Solid", method: "Revolve" }),
            node("cut", "model.datum", 600, 900, { name: "Mid-Shank", kind: "Principal plane", plane: "XZ", offset: 15 }),
            node("split", "model.feature", 900, 700, { operation: "Partition" }),
            node("bsec", "model.section", 1200, 700, { material: "Bolt-Steel", section: "Bolt" }),
            node("bmesh", "model.mesh", 1500, 700, { size: 2, shape: "Tet", element: "C3D10" }),
            node("shank", "model.region", 1800, 560, { method: "By shape", shape: "Cylindrical faces", radius: 6, tolerance: 0.01 }),
            node("bore", "model.region", 1800, 160, { method: "By shape", shape: "Cylindrical faces", radius: 6, tolerance: 0.01 }),
            node("b1", "model.instance", 2100, 600, { name: "Bolt-1", x: 100, how: "Coaxial" }),
            node("b2", "model.instance", 2400, 600, { how: "Face to face" }),
            node("pre", "model.step", 2100, 1200, { name: "Preload" }),
            node("pull", "model.step", 2400, 1200, { name: "Pull" }),
            node("headtie", "model.interaction", 2700, 300, { name: "Head", kind: "Tie" }),
            node("nuttie", "model.interaction", 2700, 560, { name: "Nut", kind: "Tie" }),
            node("touch", "model.interaction", 2700, 1200, { name: "Touch", kind: "General contact", friction: 0.2 }),
            node("fix", "model.condition", 3000, 900, { name: "Clamp", kind: "Fixed" }),
            node("preload", "model.condition", 3300, 900, { name: "Pretension", kind: "Bolt load", force: 10000 }),
            node("push", "model.condition", 3000, 1300, { name: "Push", kind: "Pressure", magnitude: 20 }),
            node("j", "model.job", 3600, 1300, { name: "Joint", action: "Write input only" }),
          ],
          edges: [
            edge("hole", "sketch", "ps", "add"),
            edge("ps", "sketch", "plate", "profile"),
            edge("plate", "part", "psec", "target"),
            edge("psec", "part", "pmesh", "target"),
            edge("pmesh", "part", "bottom", "part"),
            edge("pmesh", "part", "top", "part"),
            edge("bs", "sketch", "bolt", "profile"),
            edge("bolt", "part", "cut", "part"),
            edge("bolt", "part", "split", "part"),
            edge("cut", "datum", "split", "datum"),
            edge("split", "part", "bsec", "target"),
            edge("bsec", "part", "bmesh", "target"),
            edge("bmesh", "part", "shank", "source"),
            edge("top", "instance", "bore", "source"),
            edge("bmesh", "part", "b1", "part"),
            edge("shank", "region", "b1", "align"),
            edge("bore", "region", "b1", "to"),
            edge("b1", "instance", "b2", "part"),
            edge("m", "model", "pre", "after"),
            edge("pre", "step", "pull", "after"),
            edge("pre", "step", "touch", "step"),
            edge("pre", "step", "fix", "step"),
            edge("fix", "condition", "preload", "step"),
            edge("pull", "step", "push", "step"),
            edge("push", "condition", "j", "analysis"),
          ],
        },
        findAt("underhead", "bmesh", "Face", "8, 5, 0", 1800, 900, true),
        findAt("nutface", "bmesh", "Face", "8, 25, 0", 1800, 1100, true),
        findAt("topface", "top", "Face", "25, 15, 20", 1800, 380),
        findAt("bottomface", "bottom", "Face", "25, 15, 0", 1800, -200),
        findAt("end", "bottom", "Face", "-30, 0, 5", 2700, 900),
        findAt("cutface", "b2", "Face", "1, 1, 10", 3000, 700),
        findAt("side", "top", "Face", "30, 0, 15", 2700, 1450),
        wires(
          edge("underhead", "region", "b2", "align"),
          edge("topface", "region", "b2", "to"),
          edge("topface", "region", "headtie", "master"),
          edge("underhead", "region", "headtie", "slave"),
          edge("bottomface", "region", "nuttie", "master"),
          edge("nutface", "region", "nuttie", "slave"),
          edge("end", "region", "fix", "region"),
          edge("cutface", "region", "preload", "region"),
          edge("side", "region", "push", "region"),
        ),
      ),
  },
];

export const MODEL_EXAMPLES = EXAMPLES.map((e) => ({ title: e.title, build: () => inModel(e.build()) }));

/** Models solved in real Abaqus by the backend's tests and checked against theory (backend/tests/test_solved_examples.py
 * writes the JSON from the same builders); each answer node is named with the value it should give. */
export const SOLVED_EXAMPLES = (solved as unknown as { title: string; graph: Graph }[]).map((e) => ({
  title: e.title,
  build: () => inModel(structuredClone(e.graph)),
}));
