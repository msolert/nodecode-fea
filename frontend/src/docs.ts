import type { Edge, Node } from "@xyflow/react";
import type { OpNodeData } from "./types";

/** The graphs open in the editor's tabs (documents): each its own graph, stored apart, with its own undo history,
 * file and CAE build (the backend keeps what each tab built in CAE apart by its id). */
export interface DocMeta {
  id: string;
  name: string;
}

export type DocGraph = { nodes: Node<OpNodeData>[]; edges: Edge[] };

const INDEX_KEY = "nodecode-fea.docs.v1";
/** The first tab keeps the graph where the single-graph editor kept it, so nothing saved before is lost. */
export const MAIN_DOC = "main";
export const PROJECT_KEY = "nodecode-fea.project.v1";

export const docKey = (id: string) => (id === MAIN_DOC ? PROJECT_KEY : `nodecode-fea.doc.${id}`);

export function loadIndex(): { docs: DocMeta[]; active: string } {
  try {
    const raw = localStorage.getItem(INDEX_KEY);
    if (raw) {
      const v = JSON.parse(raw) as { docs: DocMeta[]; active: string };
      if (Array.isArray(v.docs) && v.docs.length) {
        return { docs: v.docs, active: v.docs.some((d) => d.id === v.active) ? v.active : v.docs[0].id };
      }
    }
  } catch {
    /* ignore unreadable storage */
  }
  return { docs: [{ id: MAIN_DOC, name: "Graph 1" }], active: MAIN_DOC };
}

export function saveIndex(docs: DocMeta[], active: string) {
  try {
    localStorage.setItem(INDEX_KEY, JSON.stringify({ docs, active }));
  } catch {
    /* storage full or unavailable */
  }
}

export function readDoc(id: string): DocGraph | null {
  try {
    const raw = localStorage.getItem(docKey(id));
    if (raw) {
      const g = JSON.parse(raw) as DocGraph;
      if (Array.isArray(g.nodes) && Array.isArray(g.edges)) return g;
    }
  } catch {
    /* ignore */
  }
  return null;
}

export function writeDoc(id: string, g: DocGraph) {
  try {
    localStorage.setItem(docKey(id), JSON.stringify(g));
  } catch {
    /* storage full or unavailable */
  }
}

export function dropDoc(id: string) {
  try {
    localStorage.removeItem(docKey(id));
  } catch {
    /* ignore */
  }
}

export const newDocId = () => `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

/** The CAE models a graph builds: its Model nodes' names, or NoCode-Model for modelling without a Model node. Two
 * open tabs building one model name rebuild it over each other in CAE. */
export function modelNames(nodes: { data?: OpNodeData }[]): string[] {
  const named = nodes.filter((n) => n.data?.type === "model.model" && !n.data.disabled)
    .map((n) => String(n.data!.params?.name || "NoCode-Model"));
  const modelling = nodes.some((n) => n.data?.type?.startsWith("model.") && n.data.type !== "model.model");
  return [...new Set(named.length ? named : modelling ? ["NoCode-Model"] : [])];
}

/** What each tab keeps while another one is shown (the editor is remounted per tab): undo history and the file it
 * saves to. */
export interface DocSession {
  past: unknown[];
  future: unknown[];
  committed: string;
  fileHandle: unknown;
  fileName: string | null;
  saved: string; // the graph as last opened from or saved to its file
}
export const sessions = new Map<string, DocSession>();

/** A graph opened from a file into a new tab: picked up by that tab's editor when it mounts. */
export const opening = new Map<string, { view?: string; fileHandle?: unknown; fileName: string; library?: unknown[]; autoGroup?: boolean }>();

/** Copied nodes, shared by all tabs so a part of one graph can be pasted into another. */
export const sharedClipboard: { current: DocGraph | null } = { current: null };
