export type PortType =
  | "odb" | "field" | "set" | "series" | "number" | "bool" | "text" | "model" | "sketch" | "part" | "instance" | "region" | "datum" | "analysis_step" | "condition" | "amplitude" | "interaction" | "job" | "any";

export interface PortDef {
  name: string;
  type: PortType;
  label: string;
  optional: boolean;
  multi: boolean;
  help: string;
  when?: Record<string, string[]> | null; // shown only when settings have these values (or it's wired); "unwired:<port>": while that port has no wire; "wired:<port>": once it has one; "named:<setting>": while that comma-separated setting names an item at this position
}

export type ParamValue = string | number;

export interface ParamDef {
  name: string;
  kind: "text" | "number" | "int" | "select" | "file" | "pick" | "action";
  default: ParamValue;
  label: string;
  options: string[] | null;
  help: string;
  port: PortType | null; // settings that can also be fed by a wire, through handle "param:<name>"
  when?: Record<string, string[]> | null; // shown only when other settings have these values (or it's wired); "unwired:<port>": while that port has no wire; "wired:<port>": once it has one
  per_step?: boolean; // takes "value; Step: value; …" (stepwise.ts); edited in the node's Per-step table too
  table_only?: boolean; // edited only in the Per-step table (Active in): a field only when wired
}

export interface NodeDef {
  type: string;
  title: string;
  category: string;
  description: string;
  inputs: PortDef[];
  outputs: PortDef[];
  params: ParamDef[];
  lists: boolean;
  hidden?: boolean; // replaced by another node: still runs in saved graphs, not offered for new ones
  replacedBy?: string; // for hidden nodes: the node that does the same now
}

/** Old names a node answers to in searches (the nodes merged into it), e.g. Compare ← "Filter by Value". */
export const oldNames = (nodes: NodeDef[], type: string) =>
  nodes.filter((n) => n.replacedBy === type).map((n) => n.title);

/** Does a search query match this node (title, description, or the name of a node merged into it)? */
export const nodeMatches = (nodes: NodeDef[], n: NodeDef, q: string) =>
  !q || n.title.toLowerCase().includes(q) || n.description.toLowerCase().includes(q) ||
  oldNames(nodes, n.type).some((t) => t.toLowerCase().includes(q));

export interface Catalog {
  categories: string[];
  nodes: NodeDef[];
}

export interface Option {
  value: ParamValue;
  label: string;
}

/** A part as the 3D preview draws it: its profile and how it is made, placed by an instance's steps. */
export interface Body3d {
  part: string;
  body: string; // Solid, Shell or Wire
  method: string; // Planar, Extrude or Revolve
  loops: [number, number][][];
  points: [number, number, number][];
  depth: number;
  angle: number;
  steps: (["move", [number, number, number]] | ["rotate", string, number, [number, number, number]])[];
  instance: string; // "" for a part on its own
  features: string[]; // shape features the preview doesn't draw
}

/** The mesh CAE generated for a part (its surface: element faces as corner-node polygons, beam/wire segments), with
 * a Region's element faces, edges and nodes highlighted; faces, lines and points index ``nodes``. */
export interface Mesh3d {
  nodes?: [number, number, number][];
  faces?: number[][];
  lines?: number[][];
  elements?: number;
  too_big?: number; // too many element faces to send: how many elements
  highlight?: { faces?: number[][]; lines?: number[][]; points?: number[]; elements?: number; note?: string };
}

/** A datum in part coordinates: a plane (origin, normal), an axis (origin, direction), a point, or an offset from a
 * face CAE alone knows (its picked points). */
export interface Datum3d {
  name: string;
  kind: "plane" | "axis" | "point" | "face";
  origin?: [number, number, number];
  normal?: [number, number, number];
  direction?: [number, number, number];
  points?: [number, number, number][];
}

export type Display =
  | { kind: "value"; value: number | null; label: string; unit: string; where: string; digits: number }
  | { kind: "info"; title: string; items: [string, string][] }
  | { kind: "text"; text: string; label: string }
  | { kind: "log"; title: string; text: string; path: string }
  | { kind: "sketch"; title: string; lines: [number, number][][] }
  | { kind: "model3d"; title: string; model: string; focus: string; bodies: Body3d[]; markers?: [number, number, number][]; datums?: Datum3d[]; mesh?: Mesh3d }
  | {
      kind: "table";
      title: string;
      columns: string[];
      rows: (string | number | null)[][];
      allRows: (string | number | null)[][];
      total: number;
      stats: { name: string; min: number | null; max: number | null }[];
      // Columns and rows numbered as a Formula indexes them (a[row, column]); rowIndex: each shown row's number.
      indexed?: boolean;
      rowIndex?: number[];
    }
  | {
      kind: "chart";
      title: string;
      xlabel: string;
      ylabel: string;
      series: { name: string; x: (number | null)[]; y: (number | null)[] }[];
      table?: Extract<Display, { kind: "table" }>;
    }
  | {
      kind: "contour";
      title: string;
      label: string;
      positions: number[];
      triangles: number[];
      lines: number[];
      values: (number | null)[];
      min: number;
      max: number;
      deformScale: number;
      bands: number;
      spectrum?: string;
      view?: string;
      limits?: [boolean, boolean]; // min / max typed in (not automatic)
      markers: { position: [number, number, number]; text: string; where: string; extreme?: boolean }[];
      notes: string[];
    };

export interface NodeResult {
  status: "ok" | "error" | "waiting" | "blocked" | "disabled";
  message: string;
  summary: string;
  display: Display | null;
  hints: Record<string, Option[]>;
  warnings?: string[]; // model checks: what Abaqus would refuse or get wrong because of this node
  shape?: number[]; // a list result: the sizes of its levels ([3, 4] for 3 lists of 4), to open items in the data panel
}

/** One item of a nested result (POST /api/inspect): its display and the list lengths along the way. */
export interface InspectedItem {
  display: Display | null;
  summary: string;
  levels: number[];
}

export interface OpNodeData extends Record<string, unknown> {
  type: string;
  params: Record<string, ParamValue>;
  name?: string; // the user's own name for this node, e.g. "Peak stress"
  disabled?: boolean; // deactivated: passes a matching input through, or drops out
  combine?: "all"; // lists in: every combination (a grid) instead of pairing them up item by item
  levels?: "whole"; // take every input whole: a grid into a list node is one list of rows, not one run per row
  size?: "compact" | "min"; // shrunk: settings hidden (compact) or header only (min)
  width?: number; // widened by dragging its right edge (px); unset: the default width
  app?: boolean; // the App view: true shows its result there, false leaves it out (unset: inputs and outputs shown)
}

/** What a node is called in menus: the user's name if it has one, else its type title. */
export const nodeTitle = (def: { title: string }, data: { name?: string }) =>
  data.name ? `${data.name} (${def.title})` : def.title;

export const PORT_COLORS: Record<PortType, string> = {
  odb: "#9aa6ff",
  field: "#f2a541",
  set: "#5ec27a",
  series: "#4fb3e8",
  number: "#e86ba0",
  bool: "#e8d44f",
  text: "#6fd3c2",
  model: "#d58cff",
  sketch: "#8cc4ff",
  part: "#b08cff",
  instance: "#ff9cc9",
  region: "#ffbf69",
  datum: "#d99b45",
  analysis_step: "#b79cff",
  condition: "#ff7f7f",
  amplitude: "#ffa98c",
  interaction: "#f36f9e",
  job: "#5cc8d7",
  any: "#c9ced8",
};

/** A node's port by handle id: inputs, outputs, or a setting's input ("param:<name>"). */
export function findPort(def: NodeDef | undefined, handle: string | null | undefined, side: "in" | "out"): PortDef | undefined {
  if (!def || !handle) return undefined;
  if (side === "in" && handle.startsWith("param:")) {
    const p = def.params.find((x) => x.name === handle.slice(6));
    return p?.port ? { name: handle, type: p.port, label: p.label, optional: true, multi: false, help: p.help } : undefined;
  }
  return (side === "in" ? def.inputs : def.outputs).find((p) => p.name === handle);
}

/** "any" ports (list nodes) accept and produce every kind of value. */
export const typesFit = (a: PortType, b: PortType) =>
  a === b || a === "any" || b === "any" ||
  // A path (text) can go straight into an ODB input, and an ODB into a text input (as its path).
  (a === "text" && b === "odb") || (a === "odb" && b === "text");

export const PORT_TYPE_NAMES: Record<PortType, string> = {
  odb: "ODB",
  field: "Field",
  set: "Set",
  series: "Curve",
  number: "Number",
  bool: "Yes/No",
  text: "Text",
  model: "Model",
  sketch: "Sketch",
  part: "Part",
  instance: "Instance",
  region: "Region",
  datum: "Datum",
  analysis_step: "Step",
  condition: "Load / BC",
  amplitude: "Amplitude",
  interaction: "Interaction",
  job: "Job",
  any: "Anything / list",
};

export const CATEGORY_COLORS: Record<string, string> = {
  Source: "#5b6cff",
  Data: "#2f8f9d",
  Field: "#d9822b",
  Series: "#2a8fd1",
  Math: "#c2477f",
  Text: "#2f9e8c",
  Logic: "#b59a1f",
  Input: "#c76b3b",
  Model: "#8a63d2",
  Loop: "#9a5bd1",
  List: "#7a6bd1",
  Output: "#4a9d5b",
};
