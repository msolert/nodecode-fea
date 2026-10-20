import type { Edge, Node } from "@xyflow/react";
import { groupData, isGroup } from "./groups";
import type { NodeDef, OpNodeData } from "./types";

/** One graph, three ways to look at it: building models (preprocessing), reading results (post-processing), or
 * all of it. A view only hides nodes; everything still runs. */
export type View = "model" | "results" | "all";
type Side = "model" | "results";

/** Model category: model side. ODB, fields, curves and the CAE outputs of results: results side. A Job is the
 * bridge between them (its ODB path feeds Open ODB): on both. Everything else (inputs, maths, text, lists…) is
 * general: it goes where the nodes it feeds (or is fed by) are. */
/** Results-category nodes that also work on the model (Set Operation combines model Regions too, Properties reads
 * steps and parts too): general. */
export const GENERAL_TYPES = ["set.op", "data.properties"];

function ownSide(def: NodeDef | undefined): Side[] | null {
  if (!def) return null;
  if (def.type === "model.job") return ["model", "results"];
  if (def.category === "Model") return ["model"];
  if (GENERAL_TYPES.includes(def.type)) return null;
  if (["Data", "Field", "Series"].includes(def.category) || ["view.contour", "view.chart", "view.group"].includes(def.type))
    return ["results"];
  return null;
}

/** The ids of the nodes a view leaves out. */
export function hiddenByView(nodes: Node<OpNodeData>[], edges: Edge[], defs: Record<string, NodeDef>, view: View): Set<string> {
  const hidden = new Set<string>();
  if (view === "all") return hidden;
  const sides = new Map<string, Set<Side>>();
  for (const n of nodes) {
    if (isGroup(n)) continue;
    const own = ownSide(defs[n.data.type]);
    if (own) sides.set(n.id, new Set(own));
  }
  const memberOf = (id: string) => id.split("::")[0];
  const downstream = new Map<string, string[]>();
  const upstream = new Map<string, string[]>();
  for (const e of edges) {
    const s = memberOf(e.source), t = memberOf(e.target);
    downstream.set(s, [...(downstream.get(s) ?? []), t]);
    upstream.set(t, [...(upstream.get(t) ?? []), s]);
  }
  // general nodes: the sides of what they feed, through other general nodes; else of what feeds them
  const reach = (start: string, next: Map<string, string[]>): Set<Side> => {
    const found = new Set<Side>();
    const seen = new Set([start]);
    const stack = [...(next.get(start) ?? [])];
    while (stack.length) {
      const id = stack.pop()!;
      if (seen.has(id)) continue;
      seen.add(id);
      const own = sides.get(id);
      if (own) own.forEach((s) => found.add(s));
      else stack.push(...(next.get(id) ?? []));
    }
    return found;
  };
  const general = nodes.filter((n) => !isGroup(n) && !sides.has(n.id));
  const resolved = new Map<string, Set<Side>>();
  for (const n of general) {
    let found = reach(n.id, downstream);
    if (!found.size) found = reach(n.id, upstream);
    resolved.set(n.id, found.size ? found : new Set<Side>(["model", "results"]));
  }
  resolved.forEach((v, k) => sides.set(k, v));
  for (const n of nodes) if (!isGroup(n) && !sides.get(n.id)?.has(view)) hidden.add(n.id);
  // a group shows while any of its members does
  for (const g of nodes)
    if (isGroup(g)) {
      const members = groupData(g).members;
      if (members.length && members.every((m) => hidden.has(m))) hidden.add(g.id);
    }
  return hidden;
}

/** The two graphs older versions kept apart (Modelling and Results tabs) as one: the model to the left of the
 * results, ids that clash renamed. */
export function mergeGraphs(
  model: { nodes: Node<OpNodeData>[]; edges: Edge[] },
  results: { nodes: Node<OpNodeData>[]; edges: Edge[] },
): { nodes: Node<OpNodeData>[]; edges: Edge[] } {
  if (!model.nodes.length) return results;
  if (!results.nodes.length) return model;
  const taken = new Set(results.nodes.map((n) => n.id));
  const rename = new Map<string, string>();
  for (const n of model.nodes) {
    let id = n.id;
    for (let k = 1; taken.has(id); k++) id = `model${k === 1 ? "" : k}_${n.id}`;
    taken.add(id);
    rename.set(n.id, id);
  }
  const re = (id: string) => rename.get(id) ?? id;
  const reHandle = (h: string | null | undefined) =>
    h && h.includes("::") ? `${re(h.split("::")[0])}::${h.split("::").slice(1).join("::")}` : h ?? null;
  const xs = (g: { nodes: Node[] }) => g.nodes.map((n) => n.position.x);
  const shift = Math.min(...xs(results)) - Math.max(...xs(model)) - 600;
  const minY = Math.min(...results.nodes.map((n) => n.position.y)) - Math.min(...model.nodes.map((n) => n.position.y));
  const nodes = model.nodes.map((n) => ({
    ...n,
    id: re(n.id),
    position: { x: n.position.x + shift, y: n.position.y + minY },
    data: isGroup(n) ? { ...n.data, members: groupData(n).members.map(re) } as unknown as OpNodeData : n.data,
  }));
  const edges = model.edges.map((e) => ({
    ...e,
    id: `${re(e.source)}.${e.sourceHandle ?? ""}-${re(e.target)}.${e.targetHandle ?? ""}`,
    source: re(e.source),
    target: re(e.target),
    sourceHandle: reHandle(e.sourceHandle),
    targetHandle: reHandle(e.targetHandle),
  }));
  return { nodes: [...nodes, ...results.nodes], edges: [...edges, ...results.edges] };
}
