import type { Edge, Node } from "@xyflow/react";
import type { GroupData, GroupPort } from "./components/GroupCard";
import { findPort, nodeTitle, type NodeDef, type NodeResult, type OpNodeData } from "./types";

/** Groups are nodes too (type "group"), drawn behind their members; the engine only ever sees "op" nodes. */
export const isGroup = (n: Node) => n.type === "group";
export const groupData = (n: Node) => n.data as unknown as GroupData;

export const PAD = 20;
export const HEAD = 34;
const DEFAULT_W = 250;
const DEFAULT_H = 140;
const SEP = "::";

/** The group (if any) each node belongs to. */
export function membership(nodes: Node[]): Map<string, string> {
  const of = new Map<string, string>();
  for (const n of nodes) if (isGroup(n)) for (const m of groupData(n).members) of.set(m, n.id);
  return of;
}

/** A collapsed group's port handle stands for a member's port: "<member>::<handle>". */
export function realEnd(nodes: Node[], nodeId: string, handle: string | null | undefined): [string, string | null | undefined] {
  const n = nodes.find((x) => x.id === nodeId);
  if (n && isGroup(n) && handle && handle.includes(SEP)) {
    const i = handle.indexOf(SEP);
    return [handle.slice(0, i), handle.slice(i + SEP.length)];
  }
  return [nodeId, handle];
}

function bbox(members: Node[]) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const m of members) {
    const w = m.measured?.width ?? DEFAULT_W;
    const h = m.measured?.height ?? DEFAULT_H;
    x0 = Math.min(x0, m.position.x);
    y0 = Math.min(y0, m.position.y);
    x1 = Math.max(x1, m.position.x + w);
    y1 = Math.max(y1, m.position.y + h);
  }
  return { x0, y0, x1, y1 };
}

/** The ports a collapsed group shows: wires crossing its border, unwired required inputs, and the outputs of the
 *  nodes inside that feed nothing else inside (its results). */
export function groupPorts(g: Node, nodes: Node<OpNodeData>[], edges: Edge[], defs: Record<string, NodeDef>) {
  const members = new Set(groupData(g).members);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const label = (memberId: string, handle: string, dir: "in" | "out") => {
    const n = byId.get(memberId);
    const def = n && defs[n.data.type];
    const p = findPort(def, handle, dir);
    if (!n || !def || !p) return null;
    return { id: `${memberId}${SEP}${handle}`, label: `${nodeTitle(def, n.data).replace(/ \(.*\)$/, "")} · ${p.label}`, type: p.type };
  };
  const ins = new Map<string, GroupPort>();
  const outs = new Map<string, GroupPort>();
  for (const e of edges) {
    const sIn = members.has(e.source), tIn = members.has(e.target);
    if (tIn && !sIn) {
      const p = label(e.target, e.targetHandle ?? "", "in");
      if (p) ins.set(p.id, p);
    }
    if (sIn && !tIn) {
      const p = label(e.source, e.sourceHandle ?? "", "out");
      if (p) outs.set(p.id, p);
    }
  }
  for (const id of members) {
    const n = byId.get(id);
    const def = n && defs[n.data.type];
    if (!n || !def) continue;
    for (const p of def.inputs) {
      if (!p.optional && !edges.some((e) => e.target === id && e.targetHandle === p.name)) {
        const q = label(id, p.name, "in");
        if (q) ins.set(q.id, q);
      }
    }
    const feedsInside = edges.some((e) => e.source === id && members.has(e.target));
    if (!feedsInside) {
      for (const p of def.outputs) {
        const q = label(id, p.name, "out");
        if (q) outs.set(q.id, q);
      }
    }
  }
  return { ins: [...ins.values()], outs: [...outs.values()] };
}

const RANK: Record<string, number> = { error: 4, blocked: 3, waiting: 2, disabled: 1, ok: 0 };

/** The nodes as drawn: frames sized around their members, collapsed groups as one node with their members hidden. */
export function layoutNodes(
  nodes: Node<OpNodeData>[],
  edges: Edge[],
  defs: Record<string, NodeDef>,
  results: Record<string, NodeResult>,
): Node<OpNodeData>[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const hidden = new Set<string>();
  const groups = new Map<string, Node<OpNodeData>>();
  for (const g of nodes) {
    if (!isGroup(g)) continue;
    const data = groupData(g);
    const members = data.members.map((m) => byId.get(m)).filter((m): m is Node<OpNodeData> => !!m);
    if (!members.length) continue;
    const b = bbox(members);
    if (data.collapsed) {
      members.forEach((m) => hidden.add(m.id));
      const { ins, outs } = groupPorts(g, nodes, edges, defs);
      const statuses = members.map((m) => results[m.id]?.status ?? "pending");
      const status = statuses.reduce((a, s) => ((RANK[s] ?? 0) > (RANK[a] ?? 0) ? s : a), "ok");
      const firstOut = outs[0] && results[outs[0].id.split(SEP)[0]];
      const bad = members.map((m) => results[m.id]).find((r) => r && r.status !== "ok" && r.status !== "disabled");
      groups.set(g.id, {
        ...g,
        position: { x: b.x0, y: b.y0 },
        style: undefined,
        zIndex: 1,
        data: { ...g.data, ins, outs, status, summary: bad?.message || firstOut?.summary || "" } as unknown as OpNodeData,
      });
    } else {
      groups.set(g.id, {
        ...g,
        position: { x: b.x0 - PAD, y: b.y0 - PAD - HEAD },
        style: { width: b.x1 - b.x0 + 2 * PAD, height: b.y1 - b.y0 + 2 * PAD + HEAD },
        zIndex: -1,
      });
    }
  }
  return nodes.map((n) => groups.get(n.id) ?? (hidden.has(n.id) ? { ...n, hidden: true } : n.hidden ? { ...n, hidden: false } : n));
}

/** Wires as drawn: ends inside a collapsed group attach to the group's matching port; wires inside it are hidden. */
export function layoutEdges(edges: Edge[], nodes: Node[]): Edge[] {
  const collapsedOf = new Map<string, string>();
  for (const n of nodes) if (isGroup(n) && groupData(n).collapsed) for (const m of groupData(n).members) collapsedOf.set(m, n.id);
  if (!collapsedOf.size) return edges;
  return edges.map((e) => {
    const gs = collapsedOf.get(e.source), gt = collapsedOf.get(e.target);
    if (gs && gs === gt) return { ...e, hidden: true };
    if (!gs && !gt) return e;
    return {
      ...e,
      ...(gs ? { source: gs, sourceHandle: `${e.source}${SEP}${e.sourceHandle}` } : {}),
      ...(gt ? { target: gt, targetHandle: `${e.target}${SEP}${e.targetHandle}` } : {}),
    };
  });
}

/** What the data panel shows for a group: an output inside it, else the node behind its first port. */
export function groupFocus(g: Node, nodes: Node<OpNodeData>[], edges: Edge[], defs: Record<string, NodeDef>): string | null {
  const data = groupData(g);
  const out = data.members.find((m) => defs[nodes.find((n) => n.id === m)?.data.type ?? ""]?.category === "Output");
  if (out) return out;
  const { outs } = groupPorts(g, nodes, edges, defs);
  return outs[0]?.id.split(SEP)[0] ?? data.members[0] ?? null;
}
