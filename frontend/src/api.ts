import type { Catalog, InspectedItem, NodeResult, ParamValue } from "./types";

export interface GraphPayload {
  nodes: { id: string; type: string; params: Record<string, ParamValue>; name?: string; disabled?: boolean; combine?: "all"; levels?: "whole" }[];
  edges: { source: string; sourceHandle: string | null; target: string; targetHandle: string | null }[];
  focus?: string | null; // CAE: the output to show in the current viewport
  model?: string; // the Model node unwired Sketch / Part / Step nodes go into (the one selected last)
  tab?: "model" | "results"; // CAE: which editor tab the graph is (modelling updates delete what was removed)
  take?: boolean; // CAE: Sync pressed, so a model another tab built under the same name is rebuilt for this one
  doc?: string; // the editor tab (open graph) it is: each tab's CAE build is kept apart
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const fetchCatalog = () => fetch("/api/nodes").then((r) => json<Catalog>(r));

export const fetchStatus = () =>
  fetch("/api/status").then((r) => json<{ abaqusCommand: string; abaqusFound: boolean }>(r));

export const runGraph = (graph: GraphPayload, signal?: AbortSignal) =>
  fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(graph),
    signal,
  }).then((r) => json<{ results: Record<string, NodeResult> }>(r));

/** The item at ``path`` (one index per list level) of a node's nested result, for the data panel. */
export const inspectNode = async (graph: GraphPayload, node: string, path: number[]) => {
  const res = await fetch("/api/inspect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ graph, node, path }),
  });
  if (!res.ok) throw new Error(((await res.json().catch(() => null)) as { detail?: string } | null)?.detail || res.statusText);
  return (await res.json()) as InspectedItem;
};

export const nativeBrowse = (current: string, extensions?: string[]) =>
  fetch("/api/native-browse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current, extensions }),
  }).then((r) => json<{ available: boolean; path: string | null }>(r));

export interface BrowseResult {
  path: string;
  parent: string | null;
  home: string;
  roots: string[];
  dirs: string[];
  files: { name: string; size: number; modified: number }[];
}

export const browseDir = async (path: string, extensions?: string[]) => {
  const res = await fetch(
    `/api/browse?path=${encodeURIComponent(path)}&extensions=${encodeURIComponent((extensions ?? []).join(","))}`,
  );
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as BrowseResult;
};

export interface CaeExport {
  script: string;
  filename: string;
  contents: string[];
  skipped: string[];
}

export const exportCae = async (graph: GraphPayload) => {
  const res = await fetch("/api/export-cae", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(graph),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as CaeExport;
};

// ------------------------------------------------------------ live CAE link
export interface CaeStatus {
  state: "off" | "starting" | "connected" | "no-link" | "closed";
  session?: string;
  kernelReady?: boolean;
  log?: string[];
  product?: "cae" | "viewer";
}

export type CaeProduct = "auto" | "cae" | "viewer";

/** A model of an Abaqus/CAE file (.cae) as a graph of modelling nodes that rebuilds it; a file with several models
 * and none named gives their names. Notes say what couldn't be rebuilt. */
export const openCae = <G>(path: string, model = "") =>
  post<{ graph?: G; notes?: string[]; model?: string; models?: string[] }>("/api/open-cae", { path, model });
export const migrateGraph = <G>(graph: G) => post<{ graph: G; notes: string[] }>("/api/migrate", graph);

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    const data = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(data?.detail ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export const caeStatus = () => fetch("/api/cae/status").then((r) => json<CaeStatus>(r));
export const caeLaunch = (graph: GraphPayload, product: CaeProduct = "auto") =>
  post<CaeStatus>("/api/cae/launch", { graph, product });
export const caeSync = (graph: GraphPayload) =>
  post<{ ok: boolean | null; output?: string; error?: string; pending?: boolean; contents: string[]; results?: Record<string, NodeResult> }>("/api/cae/sync", graph);
export const nodeActionRequest = (graph: GraphPayload, node: string, action: string) =>
  post<{ notes: string[]; revision: number; active: number; open?: string[] }>("/api/node-action", { graph, node, action });
export const jobsStatus = () => fetch("/api/jobs").then((r) => json<{ revision: number; active: number; results: number }>(r));
export const caePick = (kind: string) => post<{ id: string }>("/api/cae/pick", { kind });
export type CaeObjectKind = "sketch" | "part" | "instance" | "material" | "section";
export const caeObjects = (kind: CaeObjectKind) =>
  fetch(`/api/cae/objects?kind=${kind}`).then((r) => json<{ items: string[] }>(r));
/** A geometry Set or Surface defined in CAE, as Find-at points ("@x,y,z;…") a Region can take over. */
export type CaeSet = {
  model: string; part: string; instance: string; name: string; form: "Set" | "Surface";
  kind: "face" | "edge" | "vertex" | "cell"; count: number; value: string;
};
export const caeSets = () => fetch("/api/cae/sets").then((r) => json<{ items: CaeSet[] }>(r));
export const caeResult = (id: string) =>
  fetch(`/api/cae/result/${encodeURIComponent(id)}`).then((r) =>
    json<{ done: boolean; result: { ok: boolean; kind?: string; labels?: string[]; value?: string; error?: string; cancelled?: boolean } | null }>(r),
  );
