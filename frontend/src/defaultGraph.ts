import type { Edge, Node } from "@xyflow/react";
import type { OpNodeData, ParamValue } from "./types";

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

/** A tour of the node types on the built-in mock cantilever (no Abaqus needed). */
export function exampleGraph(): { nodes: Node<OpNodeData>[]; edges: Edge[] } {
  return {
    nodes: [
      node("odb", "odb.open", 0, 260, { path: "mock://cantilever?load=700" }),
      node("stress", "odb.field", 320, 0, { variable: "S", quantity: "Mises" }),
      node("disp", "odb.field", 320, 230, { variable: "U" }),
      node("contour", "view.contour", 900, 0, { scale: 0, bands: 12 }),
      node("max", "math.reduce", 900, 230, { op: "Max" }),
      node("yield", "input.value", 900, 390, { type: "Number", value: "355", label: "Yield stress" }),
      node("sf", "math.formula", 1180, 260, { expr: "b / a", label: "Safety factor" }),
      node("sfview", "view.value", 1460, 260, { label: "Safety factor", unit: "", digits: 3 }),
      node("tipu", "odb.history", 320, 470, { region: "Node BEAM-1.533", output: "U2" }),
      node("rf", "odb.history", 320, 690, { region: "Node set FIXED", output: "RF2" }),
      node("xy", "series.make", 620, 560, { xscale: -1, yscale: 1 }),
      node("curve", "view.chart", 900, 560, {
        title: "Load–displacement",
        xlabel: "Tip deflection [mm]",
        ylabel: "Reaction force [N]",
      }),
      node("path", "field.path", 620, 830, { points: "0, 5, 0; 100, 5, 0", samples: 60, method: "Interpolate" }),
      node("pathChart", "view.chart", 900, 830, {
        title: "Mises along the top fibre",
        xlabel: "Distance from the fixed end [mm]",
        ylabel: "Mises [MPa]",
      }),
    ],
    edges: [
      edge("odb", "odb", "stress", "odb"),
      edge("odb", "odb", "disp", "odb"),
      edge("stress", "field", "contour", "field"),
      edge("disp", "field", "contour", "deform"),
      edge("stress", "field", "max", "field"),
      edge("max", "value", "sf", "a"),
      edge("yield", "value", "sf", "b"),
      edge("sf", "value", "sfview", "value"),
      edge("odb", "odb", "tipu", "odb"),
      edge("odb", "odb", "rf", "odb"),
      edge("tipu", "series", "xy", "x"),
      edge("rf", "series", "xy", "y"),
      edge("xy", "series", "curve", "series"),
      edge("max", "value", "contour", "markers"),
      edge("stress", "field", "path", "field"),
      edge("path", "series", "pathChart", "series"),
    ],
  };
}

const M = "mock://cantilever?load=700";

/** Ready-made graphs for common jobs, on the built-in demo model: change Open ODB's path to use your own. */
export const EXAMPLES: { title: string; build: () => { nodes: Node<OpNodeData>[]; edges: Edge[] } }[] = [
  { title: "Tour of the nodes", build: exampleGraph },
  {
    title: "Peak Mises element: value, map, history, group",
    build: () => ({
      nodes: [
        node("odb", "odb.open", 0, 200, { path: M }),
        node("s", "odb.field", 300, 0, { variable: "S", quantity: "Mises" }),
        node("max", "math.reduce", 600, 0, { op: "Max" }),
        node("val", "view.value", 900, -120, { label: "Peak Mises", unit: "MPa" }),
        node("map", "view.contour", 900, 40, { minmax: "On" }),
        node("hist", "field.time", 600, 260, { variable: "S", quantity: "Mises" }),
        node("chart", "view.chart", 900, 320, { title: "Mises at the peak element", xlabel: "Time", ylabel: "Mises" }),
        node("grp", "view.group", 900, 520, { name: "Peak element" }),
      ],
      edges: [
        edge("odb", "odb", "s", "odb"), edge("s", "field", "max", "field"), edge("max", "value", "val", "value"),
        edge("s", "field", "map", "field"), edge("max", "value", "map", "markers"),
        edge("odb", "odb", "hist", "odb"), edge("max", "value", "hist", "set"), edge("hist", "series", "chart", "series"),
        edge("max", "value", "grp", "members"), edge("odb", "odb", "grp", "odb"),
      ],
    }),
  },
  {
    title: "Hot spots: elements over a limit, displacement only there",
    build: () => ({
      nodes: [
        node("odb", "odb.open", 0, 200, { path: M }),
        node("s", "odb.field", 300, 0, { variable: "S", quantity: "Mises" }),
        node("lim", "input.value", 300, 260, { type: "Number", value: "150", label: "Limit" }),
        node("hot", "logic.test", 600, 60, { op: ">", value: "150" }),
        node("cnt", "view.value", 900, -100, { label: "Elements over the limit" }),
        node("grp", "view.group", 900, 80, { name: "Hot spots", show: "Show only this group" }),
        node("u", "odb.field", 1200, 200, { variable: "U", quantity: "Magnitude" }),
        node("map", "view.contour", 1500, 200, {}),
      ],
      edges: [
        edge("odb", "odb", "s", "odb"), edge("s", "field", "hot", "a"), edge("lim", "value", "hot", "param:value"),
        edge("hot", "count", "cnt", "value"), edge("hot", "kept", "grp", "members"), edge("odb", "odb", "grp", "odb"),
        edge("odb", "odb", "u", "odb"), edge("grp", "set", "u", "where"), edge("u", "field", "map", "field"),
      ],
    }),
  },
  {
    title: "Several ODBs: peak Mises of each",
    build: () => ({
      nodes: [
        node("paths", "input.value", 0, 0, {
          type: "Text", label: "ODB paths",
          value: "mock://cantilever?load=400, mock://cantilever?load=700, mock://cantilever?load=1000",
        }),
        node("split", "text.split", 300, 0, { sep: "," }),
        node("s", "odb.field", 600, 0, { variable: "S", quantity: "Mises" }),
        node("max", "math.reduce", 900, 0, { op: "Max" }),
        node("tab", "view.value", 1200, -120, { label: "Peak Mises per ODB", unit: "MPa" }),
        node("curve", "series.make", 1200, 200, { name: "Peak Mises", xlabel: "ODB #" }),
        node("chart", "view.chart", 1500, 200, { title: "Peak Mises per ODB", ylabel: "Mises" }),
      ],
      edges: [
        edge("paths", "value", "split", "text"), edge("split", "list", "s", "odb"), edge("s", "field", "max", "field"),
        edge("max", "value", "tab", "value"), edge("max", "value", "curve", "y"), edge("curve", "series", "chart", "series"),
      ],
    }),
  },
  {
    title: "Load–displacement curve",
    build: () => ({
      nodes: [
        node("odb", "odb.open", 0, 100, { path: M }),
        node("u", "odb.history", 300, 0, { region: "Node BEAM-1.533", output: "U2" }),
        node("rf", "odb.history", 300, 220, { region: "Node set FIXED", output: "RF2" }),
        node("xy", "series.make", 600, 100, { xscale: -1, name: "Load–displacement" }),
        node("peak", "math.reduce", 900, -60, { op: "Max" }),
        node("val", "view.value", 1200, -60, { label: "Peak reaction", unit: "N" }),
        node("chart", "view.chart", 900, 140, { title: "Load–displacement", xlabel: "Tip deflection [mm]", ylabel: "Reaction force [N]" }),
      ],
      edges: [
        edge("odb", "odb", "u", "odb"), edge("odb", "odb", "rf", "odb"), edge("u", "series", "xy", "x"),
        edge("rf", "series", "xy", "y"), edge("xy", "series", "chart", "series"), edge("xy", "series", "peak", "field"),
        edge("peak", "value", "val", "value"),
      ],
    }),
  },
];

/** The starting graph's model side: a shell plate with a hole and a beam frame, each placed in one CAE model. */
export function modelGraph(): { nodes: Node<OpNodeData>[]; edges: Edge[] } {
  return {
    nodes: [
      node("model", "model.model", 0, 160, { name: "NoCode-Model" }),
      node("hole", "model.sketch", 0, -260, { name: "Hole", shape: "Circle", x: 200, y: 100, radius: 40 }),
      node("outline", "model.sketch", 300, -260, { name: "Plate", shape: "Rectangle", width: 400, height: 200 }),
      node("plate", "model.part", 600, -40, { name: "Plate", body: "Shell", method: "Planar" }),
      node("frame-pts", "input.value", 300, 360, {
        type: "List / matrix", label: "Frame corner points (x, y, z)",
        value: "[[0, 0, 0], [0, 0, 300], [400, 0, 300], [400, 0, 0]]",
      }),
      node("frame", "model.part", 600, 260, { name: "Frame", body: "Wire", method: "Planar" }),
      node("plate-1", "model.instance", 900, -40, { z: 300, axis: "X", angle: 90 }),
      node("frame-1", "model.instance", 900, 260, {}),
    ],
    edges: [
      edge("hole", "sketch", "outline", "add"), edge("outline", "sketch", "plate", "profile"),
      edge("model", "model", "plate", "model"), edge("model", "model", "frame", "model"),
      edge("frame-pts", "value", "frame", "profile"),
      edge("plate", "part", "plate-1", "part"), edge("frame", "part", "frame-1", "part"),
    ],
  };
}
