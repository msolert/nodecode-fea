import type { Edge, Node } from "@xyflow/react";
import { autoArrange } from "./arrange";
import { GROUP_COLORS } from "./components/GroupCard";
import { HEAD, PAD, groupData, isGroup, membership } from "./groups";
import type { NodeDef, OpNodeData } from "./types";

/** Auto-group a graph: one frame per part (its sketches, features, sections, mesh, datums and regions), one for the
 *  assembly (instances and their regions), one per step (its loads, BCs and interactions), one for the job, one for
 *  materials shared by several parts, an Inputs frame for the parameters feeding several places and an Outputs frame
 *  for the plots, tables and values. Groups made before are kept as they are. The whole graph is then laid out block
 *  by block, following the wires, so the frames don't overlap. */

type Kind = "step" | "assembly" | "part" | "job" | "materials" | "inputs" | "outputs";
// When a node takes from several groups it joins the most "downstream" one (a BC on a part's set is in its step).
const RANK: Record<Kind, number> = { job: 4, step: 3, assembly: 2, part: 1, materials: 0, inputs: 0, outputs: 0 };
const SEEDS: Record<string, Kind> = {
  "model.part": "part",
  "model.instance": "assembly",
  "model.merge_instances": "assembly",
  "model.step": "step",
  "model.job": "job",
};

const W = (n: Node) => n.measured?.width ?? 250;
const H = (n: Node) => n.measured?.height ?? 140;

export function autoGroup(
  nodes: Node<OpNodeData>[],
  edges: Edge[],
  defs: Record<string, NodeDef>,
  newId: (type: string) => string,
): { nodes: Node<OpNodeData>[]; names: string[] } {
  const grouped = membership(nodes);
  const free = nodes.filter((n) => n.type === "op" && !grouped.has(n.id));
  const isModel = (n: Node<OpNodeData>) => defs[n.data.type]?.category === "Model";
  const freeIds = new Set(free.map((n) => n.id));
  const wires = edges.filter((e) => freeIds.has(e.source) && freeIds.has(e.target) && e.source !== e.target);
  const ins = new Map<string, string[]>(), outs = new Map<string, string[]>();
  for (const e of wires) {
    ins.set(e.target, [...(ins.get(e.target) ?? []), e.source]);
    outs.set(e.source, [...(outs.get(e.source) ?? []), e.target]);
  }

  // Seeds: each Part, Step and Job starts its own group; all instances share the assembly's.
  const owner = new Map<string, string>();
  const kindOf = new Map<string, Kind>();
  const nameOf = new Map<string, string>();
  const label = (n: Node<OpNodeData>) => n.data.name || String(n.data.params?.name ?? "") || defs[n.data.type]?.title || n.id;
  for (const n of free) {
    const kind = SEEDS[n.data.type];
    if (!kind) continue;
    const key = kind === "assembly" ? "assembly" : n.id;
    owner.set(n.id, key);
    kindOf.set(key, kind);
    if (!nameOf.has(key)) nameOf.set(key, kind === "assembly" ? "Assembly" : label(n));
  }
  // Forward: the other modelling nodes join what they work on (a Mesh its part, a Load its step).
  for (let changed = true, pass = 0; changed && pass < free.length; pass++) {
    changed = false;
    for (const n of free) {
      if (SEEDS[n.data.type] || !isModel(n)) continue;
      const from = (ins.get(n.id) ?? []).map((s) => owner.get(s)).filter((o): o is string => !!o);
      if (!from.length) continue;
      const best = from.reduce((a, b) => (RANK[kindOf.get(b)!] > RANK[kindOf.get(a)!] ? b : a));
      if (owner.get(n.id) !== best) {
        owner.set(n.id, best);
        changed = true;
      }
    }
  }

  // Backward: what feeds only one group goes in it (a part's Sketch, a Region's points, a material for one part).
  for (let changed = true, pass = 0; changed && pass < free.length; pass++) {
    changed = false;
    for (const n of free) {
      if (owner.has(n.id)) continue;
      const to = new Set((outs.get(n.id) ?? []).map((t) => owner.get(t)));
      if (to.size !== 1) continue;
      const [o] = [...to];
      if (!o) continue;
      owner.set(n.id, o);
      changed = true;
    }
  }

  // Materials shared by several parts: what feeds only sections, together.
  const byId = new Map(free.map((n) => [n.id, n]));
  for (const n of free) {
    if (owner.has(n.id)) continue;
    const to = outs.get(n.id) ?? [];
    if (to.length && to.every((t) => byId.get(t)?.data.type === "model.section")) {
      owner.set(n.id, "materials");
      kindOf.set("materials", "materials");
      nameOf.set("materials", "Materials");
    }
  }

  // The graph's own inputs (parameters feeding several places) and its outputs (plots, tables, values), each together.
  for (const n of free) {
    if (owner.has(n.id)) continue;
    const cat = defs[n.data.type]?.category;
    const kind: Kind | null = cat === "Input" && outs.has(n.id) ? "inputs" : cat === "Output" ? "outputs" : null;
    if (!kind) continue;
    owner.set(n.id, kind);
    kindOf.set(kind, kind);
    nameOf.set(kind, kind === "inputs" ? "Inputs" : "Outputs");
  }

  const members = new Map<string, string[]>();
  for (const [id, o] of owner) members.set(o, [...(members.get(o) ?? []), id]);
  const keys = [...members.keys()].filter((k) => members.get(k)!.length > 1);  // a frame around one node is noise
  if (!keys.length) return { nodes, names: [] };

  // Layout: each group arranged on its own, then the groups (and the free modelling nodes) arranged as blocks.
  const pos = new Map<string, { x: number; y: number }>();
  const blockOf = new Map<string, string>();
  const blocks: Node[] = [];
  for (const k of keys) {
    const list = members.get(k)!.map((id) => byId.get(id)!);
    const at = autoArrange(list, wires);
    const x0 = Math.min(...list.map((n) => at.get(n.id)!.x)), y0 = Math.min(...list.map((n) => at.get(n.id)!.y));
    const x1 = Math.max(...list.map((n) => at.get(n.id)!.x + W(n))), y1 = Math.max(...list.map((n) => at.get(n.id)!.y + H(n)));
    for (const n of list) {
      const p = at.get(n.id)!;
      pos.set(n.id, { x: p.x - x0 + PAD, y: p.y - y0 + PAD + HEAD });
      blockOf.set(n.id, k);
    }
    const first = list.reduce((a, b) => (a.position.y < b.position.y || (a.position.y === b.position.y && a.position.x < b.position.x) ? a : b));
    blocks.push({ id: k, position: first.position, data: {}, measured: { width: x1 - x0 + 2 * PAD, height: y1 - y0 + 2 * PAD + HEAD } });
  }
  // Groups made before keep their inside as it is and move as one block.
  const allById = new Map(nodes.map((n) => [n.id, n]));
  for (const g of nodes.filter(isGroup)) {
    const list = groupData(g).members.map((m) => allById.get(m)).filter((m): m is Node<OpNodeData> => !!m);
    if (!list.length) continue;
    const x0 = Math.min(...list.map((n) => n.position.x)), y0 = Math.min(...list.map((n) => n.position.y));
    const x1 = Math.max(...list.map((n) => n.position.x + W(n))), y1 = Math.max(...list.map((n) => n.position.y + H(n)));
    const collapsed = groupData(g).collapsed;
    for (const n of list) {
      pos.set(n.id, collapsed ? { x: n.position.x - x0, y: n.position.y - y0 }
                              : { x: n.position.x - x0 + PAD, y: n.position.y - y0 + PAD + HEAD });
      blockOf.set(n.id, g.id);
    }
    blocks.push({ id: g.id, position: { x: x0, y: y0 }, data: {}, measured: collapsed
      ? { width: W(g), height: H(g) } : { width: x1 - x0 + 2 * PAD, height: y1 - y0 + 2 * PAD + HEAD } });
  }
  for (const n of free) {
    if (blockOf.has(n.id)) continue;
    pos.set(n.id, { x: 0, y: 0 });
    blockOf.set(n.id, n.id);
    blocks.push({ id: n.id, position: n.position, data: {}, measured: { width: W(n), height: H(n) } });
  }
  const blockWires = edges
    .filter((e) => blockOf.has(e.source) && blockOf.has(e.target) && blockOf.get(e.source) !== blockOf.get(e.target))
    .map((e) => ({ ...e, source: blockOf.get(e.source)!, target: blockOf.get(e.target)! }));
  const placed = autoArrange(blocks, blockWires, 120, 60);
  for (const [id, p] of pos) {
    const b = placed.get(blockOf.get(id)!)!;
    pos.set(id, { x: b.x + p.x, y: b.y + p.y });
  }

  const used = nodes.filter(isGroup).length;
  const order: Kind[] = ["inputs", "part", "materials", "assembly", "step", "job", "outputs"];
  keys.sort((a, b) => order.indexOf(kindOf.get(a)!) - order.indexOf(kindOf.get(b)!));
  const groups = keys.map((k, i) => ({
    id: newId("group"),
    type: "group",
    position: { x: 0, y: 0 },
    data: { type: "group", params: {}, name: nameOf.get(k)!, color: GROUP_COLORS[(used + i) % GROUP_COLORS.length],
            members: members.get(k)! } as unknown as OpNodeData,
  }) as Node<OpNodeData>);
  return {
    nodes: [...groups, ...nodes.map((n) => (pos.has(n.id) ? { ...n, position: pos.get(n.id)! } : n))],
    names: keys.map((k) => nameOf.get(k)!),
  };
}
