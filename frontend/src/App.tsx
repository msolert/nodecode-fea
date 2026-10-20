import {
  addEdge,
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  SelectionMode,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type FinalConnectionState,
  type Edge,
  type IsValidConnection,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  caeLaunch,
  caePick,
  caeResult,
  caeStatus,
  caeSync,
  migrateGraph,
  fetchCatalog,
  fetchStatus,
  inspectNode,
  nativeBrowse,
  openCae,
  nodeActionRequest,
  jobsStatus,
  runGraph,
  type CaeStatus,
  type CaeProduct,
  type GraphPayload,
} from "./api";
import { CaeLinkBar } from "./components/CaeLinkBar";
import { useLayout } from "./layout";
import { CaeExportDialog } from "./components/CaeExportDialog";
import { PortMenu, existingPorts, type PortChoice, type PortMenuRequest } from "./components/PortMenu";
import { QuickAdd, type QuickAddChoice, type QuickAddRequest } from "./components/QuickAdd";
import { FileBrowser } from "./components/FileBrowser";
import { DRAG_MIME, Palette } from "./components/Palette";
import { NodeCard } from "./components/NodeCard";
import { GROUP_COLORS, GroupCard, type GroupData } from "./components/GroupCard";
import { groupData, groupFocus, isGroup, layoutEdges, layoutNodes, membership, realEnd } from "./groups";
import { align, autoArrange, distribute, type AlignMode } from "./arrange";
import { autoGroup } from "./autogroup";
import { Viewer } from "./components/Viewer";
import { AppView } from "./components/AppView";
import { EditorContext } from "./context";
import { EXAMPLES, exampleGraph, modelGraph } from "./defaultGraph";
import { MODEL_EXAMPLES, SOLVED_EXAMPLES } from "./modelExamples";
import { GENERAL_TYPES, hiddenByView, mergeGraphs, type View } from "./views";
import { MAIN_DOC, PROJECT_KEY, docKey, loadIndex, modelNames, newDocId, opening, readDoc, saveIndex, sessions,
         sharedClipboard, writeDoc, dropDoc, type DocMeta } from "./docs";
import { DocTabs } from "./components/DocTabs";
import { MatrixEditor, parseGrid } from "./components/views/MatrixEditor";
import { CATEGORY_COLORS, PORT_COLORS, findPort, typesFit, type Catalog, type NodeDef, type NodeResult, type OpNodeData, type ParamValue } from "./types";

/** One graph holds the model and its results; the Modelling / Results / All views only choose what is shown. */
export type Tab = View;
// where older versions kept the two tabs' graphs apart (merged into one the first time)
const OLD_STORAGE = { results: "nodecode-fea.graph.v1", model: "nodecode-fea.graph.model.v1" };
const TAB_KEY = "nodecode-fea.tab";
function loadTab(): Tab {
  try {
    const v = localStorage.getItem(TAB_KEY);
    return v === "model" || v === "results" ? v : "all";
  } catch {
    return "all";
  }
}
/** Modelling nodes name what they make in CAE (Part-1, Sketch-1…): a new or copied one takes the next free
 * number among nodes of its type, as CAE does, instead of replacing the first one's object. */
function withFreeNames(existing: Node<OpNodeData>[], added: Node<OpNodeData>[]): Node<OpNodeData>[] {
  const taken = new Set(
    existing.filter((n) => n.data.type?.startsWith("model.")).map((n) => `${n.data.type}|${n.data.params?.name}`),
  );
  return added.map((n) => {
    const name = n.data.params?.name;
    if (!n.data.type?.startsWith("model.") || typeof name !== "string" || !name) return n;
    const m = /^(.*?)-(\d+)$/.exec(name);
    let [base, k] = m ? [m[1], Number(m[2])] : [name, 1];
    let next = name;
    while (taken.has(`${n.data.type}|${next}`)) next = `${base}-${++k}`;
    taken.add(`${n.data.type}|${next}`);
    return next === name ? n : { ...n, data: { ...n.data, params: { ...n.data.params, name: next } } };
  });
}

/** Nodes that don't belong in a view are left out of its node list (All offers everything). */
export const hiddenInTab = (tab: Tab, def: { category: string; type: string }) =>
  tab === "all"
    ? false
    : tab === "results"
      ? def.category === "Model"
      : !GENERAL_TYPES.includes(def.type) &&
        (["Data", "Field", "Series"].includes(def.category) || ["view.contour", "view.chart", "view.group"].includes(def.type));
const nodeTypes = { op: NodeCard, group: GroupCard };
const LIBRARY_KEY = "nodecode-fea.library.v1";
const APP_MODE_KEY = "nodecode-fea.app-mode";

/** A group saved as a reusable node: its nodes (positions relative to the group's corner) and inner wires. */
export interface LibraryEntry {
  name: string;
  color: string;
  nodes: { id: string; position: { x: number; y: number }; data: OpNodeData }[];
  edges: { source: string; sourceHandle: string | null; target: string; targetHandle: string | null }[];
}

function loadLibrary(): LibraryEntry[] {
  try {
    const v = JSON.parse(localStorage.getItem(LIBRARY_KEY) ?? "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

type GraphState = { nodes: Node<OpNodeData>[]; edges: Edge[] };

/** Graphs saved by older versions: Field Output's "Frame #" port is now its Frame setting. */
function migrate(g: GraphState): GraphState {
  const types = new Map(g.nodes.map((n) => [n.id, n.data?.type]));
  return {
    ...g,
    edges: g.edges.map((e) =>
      types.get(e.target) === "odb.field" && e.targetHandle === "frame" ? { ...e, targetHandle: "param:frame" } : e,
    ),
  };
}

function readGraph(key: string): GraphState | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw) {
      const g = JSON.parse(raw) as GraphState;
      if (Array.isArray(g.nodes) && Array.isArray(g.edges)) return migrate(g);
    }
  } catch {
    /* ignore unreadable storage */
  }
  return null;
}

/** The project graph; the first time, the two graphs older versions kept in their tabs, merged (they stay stored). */
function loadProject(): GraphState {
  const saved = readGraph(PROJECT_KEY);
  if (saved) return saved;
  const model = readGraph(OLD_STORAGE.model), results = readGraph(OLD_STORAGE.results);
  if (model || results) return mergeGraphs(model ?? { nodes: [], edges: [] }, results ?? { nodes: [], edges: [] });
  return mergeGraphs(modelGraph(), exampleGraph());
}

/** The part of the graph worth undoing: no selection or measured sizes. */
const snapshot = (nodes: Node<OpNodeData>[], edges: Edge[]): GraphState => ({
  nodes: nodes.map(({ id, type, position, data }) => ({ id, type, position, data })),
  edges: edges.map(({ id, source, sourceHandle, target, targetHandle }) => ({ id, source, sourceHandle, target, targetHandle })),
});

let counter = 0;
const newId = (type: string) => `${type.split(".").pop()}_${Date.now().toString(36)}${(counter++).toString(36)}`;

/** What an editor tells the tab bar about its graph: unsaved changes since its file, the CAE models it builds. */
type DocStatus = { dirty: boolean; models: string[]; fileName: string | null };

function Editor({ catalog, abaqus, doc, onStatus, onOpenNew }: {
  catalog: Catalog;
  abaqus: string;
  doc: DocMeta;
  onStatus: (id: string, status: DocStatus) => void;
  /** A file opened while this tab has a graph: it goes in a new tab. */
  onOpenNew: (name: string, g: GraphState, extra: { view?: string; fileHandle?: unknown; fileName: string; library?: unknown[]; autoGroup?: boolean }) => void;
}) {
  const [tab, setTab] = useState<Tab>(loadTab);
  const initial = useMemo(() => {
    if (doc.id === MAIN_DOC) return loadProject();
    const g = readDoc(doc.id);
    return g ? migrate(g) : { nodes: [], edges: [] };
  }, [doc.id]);
  const session = useMemo(() => sessions.get(doc.id), [doc.id]);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<OpNodeData>>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);
  const [results, setResults] = useState<Record<string, NodeResult>>({});
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState("");
  const [selected, setSelected] = useState<string | null>("contour");
  const [layout, setLayout] = useLayout();
  const workspace = useRef<HTMLDivElement>(null);
  const flow = useReactFlow();
  const fileInput = useRef<HTMLInputElement>(null);
  const [browsing, setBrowsing] = useState<{ nodeId: string; param: string; current: string; extensions?: string[] } | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [caeExport, setCaeExport] = useState<GraphPayload | null>(null);
  const [cae, setCae] = useState<CaeStatus>({ state: "off" });
  const [live, setLive] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [lastSync, setLastSync] = useState("");
  const [picking, setPicking] = useState<string | null>(null);
  const syncedSignature = useRef("");
  const [quickAdd, setQuickAdd] = useState<QuickAddRequest | null>(null);
  const [portMenu, setPortMenu] = useState<PortMenuRequest | null>(null);
  const [toast, setToast] = useState<{ text: string; undo: boolean } | null>(null);

  // ---- undo / redo: snapshots of the graph, taken when it settles
  const past = useRef<GraphState[]>((session?.past as GraphState[]) ?? []);
  const future = useRef<GraphState[]>((session?.future as GraphState[]) ?? []);
  const committed = useRef<string>(session?.committed ?? JSON.stringify(snapshot(initial.nodes, initial.edges)));
  const [, setHistoryTick] = useState(0); // re-render so the undo/redo buttons update
  const latest = useRef({ nodes, edges });
  latest.current = { nodes, edges };

  const commit = useCallback(() => {
    const now = JSON.stringify(snapshot(latest.current.nodes, latest.current.edges));
    if (now === committed.current) return;
    past.current = [...past.current.slice(-99), JSON.parse(committed.current) as GraphState];
    future.current = [];
    committed.current = now;
    setHistoryTick((t) => t + 1);
  }, []);

  const restore = useCallback(
    (g: GraphState) => {
      committed.current = JSON.stringify(g);
      setNodes(g.nodes);
      setEdges(g.edges);
      setHistoryTick((t) => t + 1);
    },
    [setNodes, setEdges],
  );

  const undo = useCallback(() => {
    commit();
    const prev = past.current.pop();
    if (!prev) return;
    future.current.push(JSON.parse(committed.current) as GraphState);
    restore(prev);
    setToast(null);
  }, [commit, restore]);

  const redo = useCallback(() => {
    const next = future.current.pop();
    if (!next) return;
    past.current.push(JSON.parse(committed.current) as GraphState);
    restore(next);
  }, [restore]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, select, textarea")) return; // let text fields keep their own undo
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if (mod && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 8000);
    return () => clearTimeout(t);
  }, [toast]);

  const defs = useMemo(() => Object.fromEntries(catalog.nodes.map((n) => [n.type, n])) as Record<string, NodeDef>, [catalog]);

  const setParam = useCallback(
    (id: string, name: string, value: ParamValue) =>
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, params: { ...n.data.params, [name]: value } } } : n))),
    [setNodes],
  );

  // Lists into a node: pair up item by item (default), or every combination (a grid).
  const toggleCombine = useCallback(
    (id: string) => {
      setNodes((ns) => {
        const target = ns.find((n) => n.id === id);
        if (!target) return ns;
        const ids = target.selected ? new Set(ns.filter((n) => n.selected).map((n) => n.id)) : new Set([id]);
        const all = target.data.combine !== "all";
        return ns.map((n) => (ids.has(n.id) ? { ...n, data: { ...n.data, combine: all ? "all" : undefined } } : n));
      });
    },
    [setNodes],
  );

  // Inputs deeper than the node takes: run once per row (default), or take them whole (Pick 0 on a grid: the first row).
  const toggleWhole = useCallback(
    (id: string) => {
      setNodes((ns) => {
        const target = ns.find((n) => n.id === id);
        if (!target) return ns;
        const ids = target.selected ? new Set(ns.filter((n) => n.selected).map((n) => n.id)) : new Set([id]);
        const whole = target.data.levels !== "whole";
        return ns.map((n) => (ids.has(n.id) ? { ...n, data: { ...n.data, levels: whole ? "whole" : undefined } } : n));
      });
    },
    [setNodes],
  );

  // Deactivate / activate a node, or the whole selection when the node is part of it.
  const toggleDisabled = useCallback(
    (id: string) => {
      setNodes((ns) => {
        const target = ns.find((n) => n.id === id);
        if (!target) return ns;
        const ids = target.selected ? new Set(ns.filter((n) => n.selected).map((n) => n.id)) : new Set([id]);
        const off = !target.data.disabled;
        return ns.map((n) => (ids.has(n.id) ? { ...n, data: { ...n.data, disabled: off || undefined } } : n));
      });
    },
    [setNodes],
  );

  // Shrink / grow a node, or the whole selection when the node is part of it: full -> compact -> header only.
  const cycleSize = useCallback(
    (id: string) => {
      setNodes((ns) => {
        const target = ns.find((n) => n.id === id);
        if (!target) return ns;
        const ids = target.selected ? new Set(ns.filter((n) => n.selected).map((n) => n.id)) : new Set([id]);
        const next = target.data.size === undefined ? "compact" : target.data.size === "compact" ? "min" : undefined;
        return ns.map((n) => (ids.has(n.id) && n.type === "op" ? { ...n, data: { ...n.data, size: next } } : n));
      });
      setTimeout(commit, 0);
    },
    [setNodes, commit],
  );

  // A node widened by dragging its right edge, to read long labels, values and messages.
  const setWidth = useCallback(
    (id: string, width: number | undefined) => {
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, width } } : n)));
      setTimeout(commit, 0); // undoable
    },
    [setNodes, commit],
  );

  // A node's own name (shown in its header and in menus); it doesn't change results, so it doesn't re-run.
  const [renaming, setRenaming] = useState<string | null>(null);
  const setName = useCallback(
    (id: string, name: string) => {
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, name: name.trim() || undefined } } : n)));
      setTimeout(commit, 0); // undoable
    },
    [setNodes, commit],
  );

  // Only what affects results; moving nodes around doesn't trigger a run.
  // The model unwired Sketch / Part / Step nodes go into: the Model node selected last, else the first one.
  const [currentModel, setCurrentModel] = useState<string | null>(null);
  useEffect(() => {
    if (selected && nodes.find((n) => n.id === selected)?.data.type === "model.model") setCurrentModel(selected);
  }, [selected, nodes]);
  const payload: GraphPayload = useMemo(
    () => ({
      doc: doc.id,  // the backend keeps what each tab built in CAE apart
      ...(currentModel && nodes.some((n) => n.id === currentModel) ? { model: currentModel } : {}),
      nodes: nodes.filter((n) => n.type === "op").map((n) => ({
        id: n.id, type: n.data.type, params: n.data.params,
        ...(n.data.name ? { name: n.data.name } : {}), ...(n.data.disabled ? { disabled: true } : {}),
        ...(n.data.combine === "all" ? { combine: "all" } : {}),
        ...(n.data.levels === "whole" ? { levels: "whole" } : {}),
      })),
      edges: edges.map((e) => ({ source: e.source, sourceHandle: e.sourceHandle ?? null, target: e.target, targetHandle: e.targetHandle ?? null })),
    }),
    [nodes, edges, currentModel, doc.id],
  );
  const signature = JSON.stringify(payload);

  // What CAE's current viewport shows: the Contour Plot/Chart or modelling node clicked last (else the last
  // contour, or for modelling the node that changed).
  const [focus, setFocus] = useState<string | null>(null);
  useEffect(() => {
    const n = nodes.find((x) => x.id === selected);
    const picked = n?.data.type === "input.value" && String(n.data.params.type ?? "").endsWith(" in CAE");
    if (n && (n.data.type === "view.contour" || n.data.type === "view.chart" || n.data.type.startsWith("model.") || picked))
      setFocus(n.id);
  }, [selected, nodes]);
  // Analysis jobs run outside the graph (see the jobs poll below). `liveResults` moves when a running job's ODB
  // gets new increments: it re-syncs CAE's Results outputs so they show the new frames.
  const [jobRevision, setJobRevision] = useState(0);
  const [liveResults, setLiveResults] = useState(0);
  // tab "model": the update also deletes from CAE what was removed from the graph's modelling, also its last model
  // node; a graph that never had modelling leaves CAE's models alone.
  const hasModel = nodes.some((n) => n.data.type?.startsWith("model."));
  const hadModel = useRef(hasModel);
  if (hasModel) hadModel.current = true;
  const caePayload = useMemo(
    () => ({ ...payload, focus, tab: (hasModel || hadModel.current ? "model" : "results") as "model" | "results", ...(liveResults ? { live: liveResults } : {}) }),
    [payload, focus, hasModel, liveResults],
  );
  const caeSignature = JSON.stringify(caePayload);

  useEffect(() => {
    const t = setTimeout(commit, 400);
    return () => clearTimeout(t);
  }, [signature, commit]);

  // Analysis jobs run outside the graph: when one is queued, progresses or ends, run the graph again so the Job
  // node shows it and an Open ODB fed by its path reads the new results.
  // An Open ODB on a running job's results is followed the same way: each increment it writes moves the revision.
  const hasJobs = nodes.some((n) => n.data.type === "model.job" || n.data.type === "odb.open");
  useEffect(() => {
    if (!hasJobs) return;
    let alive = true;
    const tick = () =>
      jobsStatus()
        .then((s) => {
          if (!alive) return;
          setJobRevision(s.revision);
          setLiveResults(s.results ?? 0);
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [hasJobs]);

  // Auto update off: edits wait until Update (or Ctrl+Enter) runs the graph; the results shown are the last run's.
  const [autoRun, setAutoRunState] = useState(() => {
    try {
      return localStorage.getItem("nodecode-fea.autorun") !== "0";
    } catch {
      return true;
    }
  });
  const setAutoRun = (v: boolean) => {
    setAutoRunState(v);
    try {
      localStorage.setItem("nodecode-fea.autorun", v ? "1" : "0");
    } catch {
      /* ignore */
    }
  };
  const [ranSignature, setRanSignature] = useState(signature);
  const runSignature = autoRun ? signature : ranSignature;
  const outdated = !autoRun && signature !== ranSignature;
  const [runNonce, setRunNonce] = useState(0);  // Update with nothing changed still runs again (new ODB results…)
  const updateNow = useCallback(() => {
    setRanSignature(latestSignature.current);
    setRunNonce((n) => n + 1);
  }, []);
  const latestSignature = useRef(signature);
  latestSignature.current = signature;
  useEffect(() => {
    if (autoRun) setRanSignature(signature);
  }, [autoRun, signature]);
  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      setRunning(true);
      runGraph(JSON.parse(runSignature) as GraphPayload, ctrl.signal)
        .then((r) => {
          setResults(r.results);
          setRunError("");
        })
        .catch((e: Error) => {
          if (e.name !== "AbortError") setRunError(e.message);
        })
        .finally(() => !ctrl.signal.aborted && setRunning(false));
    }, 250);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [runSignature, jobRevision, runNonce]);

  useEffect(() => {
    try {
      localStorage.setItem(docKey(doc.id), JSON.stringify({ nodes, edges }));
    } catch {
      /* storage full or unavailable */
    }
  }, [nodes, edges]);

  const portType = useCallback(
    (nodeId: string, handle: string | null | undefined, dir: "in" | "out") => {
      const [id, h] = realEnd(nodes, nodeId, handle);
      const n = nodes.find((x) => x.id === id);
      return findPort(n && defs[n.data.type], h, dir);
    },
    [nodes, defs],
  );

  const isValidConnection: IsValidConnection = useCallback(
    (c) => {
      if (c.source === c.target) return false;
      const out = portType(c.source, c.sourceHandle, "out");
      const inp = portType(c.target, c.targetHandle, "in");
      return !!out && !!inp && typesFit(out.type, inp.type);
    },
    [portType],
  );

  const onConnect = useCallback(
    (raw: Connection) => {
      // A collapsed group's ports stand for its members' ports: wire to those.
      const [source, sourceHandle] = realEnd(nodes, raw.source, raw.sourceHandle);
      const [target, targetHandle] = realEnd(nodes, raw.target, raw.targetHandle);
      const c = { ...raw, source, sourceHandle: sourceHandle ?? null, target, targetHandle: targetHandle ?? null };
      const inp = portType(c.target, c.targetHandle, "in");
      setEdges((es) => {
        const kept = inp?.multi ? es : es.filter((e) => !(e.target === c.target && e.targetHandle === c.targetHandle));
        return addEdge({ ...c, id: `${c.source}.${c.sourceHandle}-${c.target}.${c.targetHandle}` }, kept);
      });
    },
    [portType, setEdges, nodes],
  );

  // what the view leaves out (the other side of the graph); it still runs
  const outOfView = useMemo(() => hiddenByView(nodes, edges, defs, tab), [nodes, edges, defs, tab]);
  const styledEdges = useMemo(
    () =>
      layoutEdges(edges, nodes).map((e) => {
        if (outOfView.has(e.source) || outOfView.has(e.target)) return { ...e, hidden: true };
        const t = portType(e.source, e.sourceHandle, "out")?.type;
        const failed = results[e.source] && results[e.source].status !== "ok";
        return {
          ...e,
          style: { stroke: t ? PORT_COLORS[t] : "#888", strokeWidth: 2.2, opacity: failed ? 0.35 : 1 },
          animated: running && !results[e.target],
        };
      }),
    [edges, nodes, portType, results, running, outOfView],
  );

  // ---- groups: frames around nodes that move together, collapsible into one node, saveable as reusable nodes
  const viewNodes = useMemo(
    () => layoutNodes(nodes, edges, defs, results).map((n) => (outOfView.has(n.id) ? { ...n, hidden: true, selected: false } : n)),
    [nodes, edges, defs, results, outOfView],
  );
  const viewPos = useRef(new Map<string, { x: number; y: number }>());
  viewPos.current = new Map(viewNodes.filter(isGroup).map((n) => [n.id, n.position]));

  const handleNodesChange = useCallback(
    (changes: NodeChange<Node<OpNodeData>>[]) => {
      const groupIds = new Set(nodes.filter(isGroup).map((n) => n.id));
      const moves: { members: string[]; dx: number; dy: number }[] = [];
      const removedCollapsed: string[] = [];
      for (const c of changes) {
        if (c.type !== "position" || !groupIds.has(c.id)) continue;
        const at = viewPos.current.get(c.id);
        const g = nodes.find((n) => n.id === c.id)!;
        if (c.position && at) moves.push({ members: groupData(g).members, dx: c.position.x - at.x, dy: c.position.y - at.y });
      }
      // A moved group carries its members; their own drag changes (when they are selected too) would move them twice.
      const carried = new Set(moves.flatMap((m) => m.members));
      const rest = changes.filter((c) => {
        if (c.type === "position" && groupIds.has(c.id)) return false; // a group's place follows its members
        if (c.type === "position" && carried.has(c.id) && c.position) return false;
        if (c.type === "remove" && groupIds.has(c.id)) {
          const g = nodes.find((n) => n.id === c.id)!;
          if (groupData(g).collapsed) removedCollapsed.push(...groupData(g).members);
        }
        return true;
      });
      onNodesChange(rest);
      if (moves.length) {
        setNodes((ns) =>
          ns.map((n) => {
            const m = moves.find((mv) => mv.members.includes(n.id));
            return m ? { ...n, position: { x: n.position.x + m.dx, y: n.position.y + m.dy } } : n;
          }),
        );
      }
      if (removedCollapsed.length) {  // deleting a collapsed group deletes what's inside it
        const gone = new Set(removedCollapsed);
        setNodes((ns) => ns.filter((n) => !gone.has(n.id)));
        setEdges((es) => es.filter((e) => !gone.has(e.source) && !gone.has(e.target)));
      }
    },
    [nodes, onNodesChange, setNodes, setEdges],
  );

  // Groups whose members are all gone go too.
  useEffect(() => {
    const ids = new Set(nodes.map((n) => n.id));
    if (nodes.some((n) => isGroup(n) && !groupData(n).members.some((m) => ids.has(m)))) {
      setNodes((ns) => ns.filter((n) => !isGroup(n) || groupData(n).members.some((m) => ids.has(m))));
    }
  }, [nodes, setNodes]);

  const setGroup = useCallback(
    (id: string, patch: Partial<GroupData>) =>
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } as unknown as OpNodeData } : n))),
    [setNodes],
  );

  const groupSelection = useCallback(() => {
    const sel = nodes.filter((n) => n.selected);
    // Grouping with a group selected adds to it (the biggest one, if several): it keeps its name and colour.
    const keep = sel.filter(isGroup).sort((a, b) => groupData(b).members.length - groupData(a).members.length)[0];
    const members = new Set<string>();
    for (const n of keep ? [keep, ...sel] : sel) {
      if (isGroup(n)) groupData(n).members.forEach((m) => members.add(m));
      else if (n.type === "op") members.add(n.id);
    }
    if (!members.size) return;
    if (keep) {
      setNodes((ns) =>
        ns
          .filter((n) => !(isGroup(n) && n.selected && n.id !== keep.id))  // other selected groups merge into it
          .map((n) =>
            n.id === keep.id
              ? { ...n, selected: true, data: { ...n.data, members: [...members] } as unknown as OpNodeData }
              : isGroup(n)
                ? { ...n, data: { ...n.data, members: groupData(n).members.filter((m) => !members.has(m)) } as unknown as OpNodeData }
                : { ...n, selected: false },
          ),
      );
      setSelected(null);
      setTimeout(commit, 0);
      return;
    }
    const id = newId("group");
    const used = nodes.filter(isGroup).length;
    const group = {
      id,
      type: "group",
      position: { x: 0, y: 0 },
      selected: true,
      data: { type: "group", params: {}, name: `Group ${used + 1}`, color: GROUP_COLORS[used % GROUP_COLORS.length],
              members: [...members] } as unknown as OpNodeData,
    } as Node<OpNodeData>;
    setNodes((ns) => [
      group,
      ...ns
        .filter((n) => !(isGroup(n) && n.selected))  // grouping groups merges them
        .map((n) =>
          isGroup(n)  // a node belongs to one group: take it out of any other
            ? { ...n, data: { ...n.data, members: groupData(n).members.filter((m) => !members.has(m)) } as unknown as OpNodeData }
            : { ...n, selected: false },
        ),
    ]);
    setSelected(null);
    setTimeout(commit, 0);
  }, [nodes, setNodes, commit]);

  /** A node dropped with its centre inside another group's frame joins that group (the smallest frame, if they overlap). */
  const dropIntoFrame = useCallback(
    (_e: React.MouseEvent, _node: Node, dragged: Node[]) => {
      const draggedGroups = dragged.filter(isGroup);
      const carried = new Set(draggedGroups.flatMap((g) => groupData(g).members));
      const frames = viewNodes.filter((f) => isGroup(f) && !groupData(f).collapsed && !f.hidden && f.style);
      const ofNode = membership(nodes);
      const joins = new Map<string, string>();  // node -> group it joins
      for (const d of dragged) {
        if (d.type !== "op" || carried.has(d.id)) continue;
        const cx = d.position.x + (d.measured?.width ?? 0) / 2;
        const cy = d.position.y + (d.measured?.height ?? 0) / 2;
        const area = (f: Node) => Number(f.style!.width) * Number(f.style!.height);
        const inside = frames
          .filter((f) => f.id !== ofNode.get(d.id))
          .filter((f) => cx >= f.position.x && cx <= f.position.x + Number(f.style!.width)
                      && cy >= f.position.y && cy <= f.position.y + Number(f.style!.height))
          .sort((a, b) => area(a) - area(b))[0];
        if (inside) joins.set(d.id, inside.id);
      }
      if (joins.size) {
        setNodes((ns) =>
          ns.map((n) => {
            if (!isGroup(n)) return n;
            const kept = groupData(n).members.filter((m) => !joins.has(m));  // a node belongs to one group
            const added = [...joins].filter(([, g]) => g === n.id).map(([m]) => m);
            return kept.length === groupData(n).members.length && !added.length
              ? n
              : { ...n, data: { ...n.data, members: [...kept, ...added] } as unknown as OpNodeData };
          }),
        );
        setTimeout(commit, 0);
      } else commit();
    },
    [nodes, viewNodes, setNodes, commit],
  );

  const ungroupSelection = useCallback(() => {
    // A selected group is dissolved; selected nodes in a group that isn't selected just leave it.
    const ofNode = membership(nodes);
    const sel = nodes.filter((n) => n.selected);
    const drop = new Set(sel.filter(isGroup).map((n) => n.id));
    const leave = new Set(sel.filter((n) => n.type === "op" && ofNode.has(n.id) && !drop.has(ofNode.get(n.id)!)).map((n) => n.id));
    if (!drop.size && !leave.size) return;
    setNodes((ns) =>
      ns
        .filter((n) => !drop.has(n.id))
        .map((n) =>
          isGroup(n) && groupData(n).members.some((m) => leave.has(m))
            ? { ...n, data: { ...n.data, members: groupData(n).members.filter((m) => !leave.has(m)) } as unknown as OpNodeData }
            : n,
        ),
    );
    setTimeout(commit, 0);
  }, [nodes, setNodes, commit]);

  /** Frame the graph by part, assembly, step, job, inputs and outputs (groups made before stay as they are). */
  const autoGroupModel = useCallback(() => {
    const { nodes: next, names } = autoGroup(latest.current.nodes, latest.current.edges, defs, newId);
    if (!names.length) {
      setToast({ text: "Nothing to group: auto-group frames parts, the assembly, steps, the job, shared inputs and outputs", undo: false });
      return;
    }
    commit();
    setNodes(next);
    setTimeout(() => {
      commit();
      flow.fitView({ padding: 0.1, maxZoom: 1.2, duration: 300 });
    }, 60);
    setToast({ text: `Grouped into ${names.length}: ${names.join(", ")}`, undo: true });
  }, [defs, setNodes, commit, flow]);

  // ---- tidying: align, distribute, auto-arrange (positions only), snap to grid
  const [snap, setSnap] = useState(() => {
    try {
      return localStorage.getItem("nodecode-fea.snap") === "1";
    } catch {
      return false;
    }
  });
  const toggleSnap = () => {
    setSnap((v) => {
      try {
        localStorage.setItem("nodecode-fea.snap", v ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !v;
    });
  };
  const movable = useCallback(
    (all = false) => viewNodes.filter((n) => n.type === "op" && !n.hidden && (all || n.selected)),
    [viewNodes],
  );
  const applyPositions = useCallback(
    (pos: Map<string, { x: number; y: number }>) => {
      if (!pos.size) return;
      setNodes((ns) => ns.map((n) => (pos.has(n.id) ? { ...n, position: pos.get(n.id)! } : n)));
      setTimeout(commit, 0);
    },
    [setNodes, commit],
  );
  const alignSel = (mode: AlignMode) => applyPositions(align(movable(), mode));
  const distributeSel = (axis: "x" | "y") => applyPositions(distribute(movable(), axis));
  const arrange = (all: boolean) => {
    const list = movable(all);
    applyPositions(autoArrange(list.length > 1 ? list : movable(true), edges));
    if (all || list.length <= 1) setTimeout(() => flow.fitView({ padding: 0.1, maxZoom: 1.2, duration: 300 }), 60);
  };
  const selectedOps = viewNodes.filter((n) => n.type === "op" && n.selected && !n.hidden).map((n) => n.id);

  const [library, setLibrary] = useState<LibraryEntry[]>(loadLibrary);
  const saveLibrary = (lib: LibraryEntry[]) => {
    setLibrary(lib);
    try {
      localStorage.setItem(LIBRARY_KEY, JSON.stringify(lib));
    } catch {
      /* storage full or unavailable */
    }
  };

  const saveGroupAsNode = useCallback(
    (groupId: string) => {
      const g = nodes.find((n) => n.id === groupId);
      if (!g) return;
      const data = groupData(g);
      const members = nodes.filter((n) => data.members.includes(n.id));
      const x0 = Math.min(...members.map((m) => m.position.x));
      const y0 = Math.min(...members.map((m) => m.position.y));
      const ids = new Set(members.map((m) => m.id));
      const entry: LibraryEntry = {
        name: data.name,
        color: data.color,
        nodes: members.map((m) => ({ id: m.id, position: { x: m.position.x - x0, y: m.position.y - y0 }, data: m.data })),
        edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({
          source: e.source, sourceHandle: e.sourceHandle ?? null, target: e.target, targetHandle: e.targetHandle ?? null,
        })),
      };
      saveLibrary([...library.filter((l) => l.name !== entry.name), entry]);
      setToast({ text: `Saved "${entry.name}" to My groups in the node list`, undo: false });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, edges, library],
  );

  const addNode = useCallback(
    (type: string, position?: { x: number; y: number }) => {
      const def = defs[type];
      const pos =
        position ??
        flow.screenToFlowPosition({ x: window.innerWidth * 0.35, y: window.innerHeight * 0.4 });
      const id = newId(type);
      const params = Object.fromEntries(def.params.map((p) => [p.name, p.default]));
      setNodes((ns) => [
        ...ns.map((n) => ({ ...n, selected: false })),
        ...withFreeNames(ns, [{ id, type: "op", position: pos, data: { type, params }, selected: true }]),
      ]);
      setSelected(id);
      // A node that takes a Model (Sketch, Part, a first Step) starts wired to the current model, so it isn't
      // built in NoCode-Model next to it; rewire it to use another model.
      const models = nodes.filter((n) => n.type === "op" && n.data.type === "model.model");
      const port = def.inputs.find(
        (p) =>
          p.type === "model" ||
          (type === "model.step" && p.name === "after" && !nodes.some((n) => n.data.type === "model.step")),
      );
      const model = models.find((n) => n.id === currentModel) ?? models[0];
      if (port && model) {
        setEdges((es) =>
          addEdge(
            { source: model.id, sourceHandle: "model", target: id, targetHandle: port.name,
              id: `${model.id}.model-${id}.${port.name}` },
            es,
          ),
        );
      }
      return id;
    },
    [defs, flow, setNodes, setEdges, nodes, currentModel],
  );

  // Dropping a wire on empty canvas offers the nodes that fit it, right at the mouse.
  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
      // Not a valid connection (a nearby port that doesn't fit counts as no port).
      if (state.isValid || !state.fromHandle) return;
      const point = "changedTouches" in event ? event.changedTouches[0] : event;
      // Only a drop on empty canvas, judged by what is under the pointer (the event's target is the port the
      // drag started from); a click on a port also ends a "connection" and must not open this menu.
      const under = document.elementFromPoint(point.clientX, point.clientY);
      if (!under?.closest(".react-flow__pane") || under.closest(".react-flow__node")) return;
      const fromSide = state.fromHandle.type;
      const port = portType(state.fromHandle.nodeId, state.fromHandle.id, fromSide === "source" ? "out" : "in");
      if (!port) return;
      const [fromNode, fromHandle] = realEnd(nodes, state.fromHandle.nodeId, state.fromHandle.id);
      setQuickAdd({
        x: point.clientX,
        y: point.clientY,
        fromNode,
        fromHandle: fromHandle ?? "",
        fromSide,
        type: port.type,
      });
    },
    [portType, nodes],
  );

  const pickQuickAdd = useCallback(
    (req: QuickAddRequest, choice: QuickAddChoice) => {
      setQuickAdd(null);
      const at = flow.screenToFlowPosition({ x: req.x, y: req.y });
      if (!req.fromNode) {  // opened by double-clicking empty canvas: just add the node there
        addNode(choice.nodeType, { x: at.x - 20, y: at.y - 15 });
        return;
      }
      // Put the new node so its port sits near the drop point.
      const pos = req.fromSide === "source" ? { x: at.x + 10, y: at.y - 40 } : { x: at.x - 250, y: at.y - 40 };
      const id = addNode(choice.nodeType, pos);
      const conn: Connection =
        req.fromSide === "source"
          ? { source: req.fromNode, sourceHandle: req.fromHandle, target: id, targetHandle: choice.port }
          : { source: id, sourceHandle: choice.port, target: req.fromNode, targetHandle: req.fromHandle };
      // The new node isn't in `nodes` yet, so wire it directly instead of through onConnect's lookups.
      setEdges((es) => {
        // the wire replaces what was in a single input (the dragged-from one, or the new node's model wire)
        const replace =
          req.fromSide === "source" || !portType(req.fromNode, req.fromHandle, "in")?.multi;
        const kept = replace ? es.filter((e) => !(e.target === conn.target && e.targetHandle === conn.targetHandle)) : es;
        return addEdge({ ...conn, id: `${conn.source}.${conn.sourceHandle}-${conn.target}.${conn.targetHandle}` }, kept);
      });
    },
    [flow, addNode, setEdges, portType],
  );

  /** Put a saved group into the graph, collapsed, at a point (or the middle of the view). */
  const addFromLibrary = (entry: LibraryEntry, at?: { x: number; y: number }) => {
    const pos = at ?? flow.screenToFlowPosition({ x: window.innerWidth * 0.35, y: window.innerHeight * 0.4 });
    const g: GraphState = {
      nodes: [
        { id: "__group", type: "group", position: { x: 0, y: 0 },
          data: { type: "group", params: {}, name: entry.name, color: entry.color, collapsed: true,
                  members: entry.nodes.map((n) => n.id) } as unknown as OpNodeData },
        ...entry.nodes.map((n) => ({ id: n.id, type: "op", position: n.position, data: n.data })),
      ],
      edges: entry.edges.map((e) => ({ ...e, id: `${e.source}.${e.sourceHandle}-${e.target}.${e.targetHandle}` })),
    };
    insertCopy(g, [], pos);
    setTimeout(commit, 0);
  };

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      const lib = e.dataTransfer.getData(DRAG_MIME + "-group");
      if (lib !== "") {
        e.preventDefault();
        addFromLibrary(library[Number(lib)], flow.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
        return;
      }
      const type = e.dataTransfer.getData(DRAG_MIME);
      if (!type) return;
      e.preventDefault();
      addNode(type, flow.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [addNode, flow, library],
  );

  // Save writes back to the same file (Chrome/Edge: File System Access), else downloads a copy. The file also
  // carries My groups, so they travel with the graph.
  const arriving = useMemo(() => opening.get(doc.id), [doc.id]);
  const fileHandle = useRef<any>(session?.fileHandle ?? arriving?.fileHandle ?? null);  // eslint-disable-line @typescript-eslint/no-explicit-any
  const [fileName, setFileName] = useState<string | null>(session?.fileName ?? arriving?.fileName ?? null);
  // the graph as last opened from or saved to its file (unsaved changes since: ✱ on the tab)
  const saved = useRef<string>(session?.saved ?? (arriving ? JSON.stringify(snapshot(initial.nodes, initial.edges)) : ""));
  const [, setSavedTick] = useState(0);
  const save = async (as = false) => {
    const text = JSON.stringify({ ...snapshot(nodes, edges), view: tab, library }, null, 2);
    const w = window as any;  // eslint-disable-line @typescript-eslint/no-explicit-any
    if (w.showSaveFilePicker) {
      try {
        if (as || !fileHandle.current) {
          fileHandle.current = await w.showSaveFilePicker({
            suggestedName: fileName ?? `${doc.name}.abqflow.json`,
            types: [{ description: "NodeCode FEA graph", accept: { "application/json": [".json"] } }],
          });
        }
        const out = await fileHandle.current.createWritable();
        await out.write(text);
        await out.close();
        setFileName(fileHandle.current.name);
        saved.current = JSON.stringify(snapshot(nodes, edges));
        setSavedTick((t) => t + 1);
        setToast({ text: `Saved ${fileHandle.current.name}`, undo: false });
      } catch (e) {
        if ((e as Error).name !== "AbortError") setToast({ text: `Couldn't save: ${(e as Error).message}`, undo: false });
      }
      return;
    }
    const blob = new Blob([text], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = fileName ?? `${doc.name}.abqflow.json`;
    a.click();
    saved.current = JSON.stringify(snapshot(nodes, edges));
    setSavedTick((t) => t + 1);
    URL.revokeObjectURL(a.href);
  };

  const replaceGraph = (g: GraphState, message?: string) => {
    commit();
    setNodes(g.nodes);
    setEdges(g.edges);
    setResults({});
    setSelected(null);
    setTimeout(() => {
      flow.fitView({ padding: 0.1, maxZoom: 1.2 });
      commit();
    }, 50);
    if (message) setToast({ text: message, undo: true });
  };

  // Graphs saved before nodes were merged: turn the old nodes into the general ones (same results).
  const hasOldNodes = nodes.some((n) => defs[n.data.type]?.hidden);
  useEffect(() => {
    if (!hasOldNodes) return;
    let alive = true;
    migrateGraph({ nodes, edges })
      .then(({ graph, notes }) => {
        if (!alive || !notes.length) return;
        commit();
        setNodes(graph.nodes as Node<OpNodeData>[]);
        setEdges(graph.edges as Edge[]);
        setTimeout(commit, 0);
        const changed = notes.filter((n) => n.includes("→")).length;
        setToast({ text: `Updated ${changed} older node${changed === 1 ? "" : "s"} to the current ones (same results).`, undo: true });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasOldNodes]);

  const graphNow = JSON.stringify(snapshot(nodes, edges));
  const dirty = !!saved.current && graphNow !== saved.current;
  const models = modelNames(nodes).join("\n");
  useEffect(() => {
    onStatus(doc.id, { dirty, models: models ? models.split("\n") : [], fileName });
  }, [doc.id, dirty, models, fileName, onStatus]);
  const keepSession = useRef<() => void>(() => {});
  keepSession.current = () => sessions.set(doc.id, {
    past: past.current, future: future.current, committed: committed.current, fileHandle: fileHandle.current,
    fileName, saved: saved.current,
  });
  useEffect(() => () => keepSession.current(), []);
  // a file opened into this new tab: its view and the groups saved with it
  useEffect(() => {
    if (!arriving) return;
    opening.delete(doc.id);
    if (arriving.view) setTab(arriving.view as Tab);
    const extra = ((arriving.library ?? []) as LibraryEntry[]).filter((l) => !library.some((m) => m.name === l.name));
    if (extra.length) saveLibrary([...library, ...extra]);
    setTimeout(() => flow.fitView({ padding: 0.1, maxZoom: 1.2 }), 60);
    setToast({ text: `Opened ${arriving.fileName} in a new tab`, undo: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // a CAE model opened as a graph arrives grouped by part, assembly, step and job (once its nodes have their sizes)
  const pendingGroup = useRef(!!arriving?.autoGroup);
  const measuredAll = viewNodes.every((n) => n.type !== "op" || n.hidden || !!n.measured?.width);
  useEffect(() => {
    if (!pendingGroup.current) return;
    const run = () => {
      if (!pendingGroup.current) return;
      pendingGroup.current = false;
      autoGroupModel();
    };
    if (measuredAll) return run();
    const t = setTimeout(run, 1500);  // sizes that never come (nothing visible) don't block it
    return () => clearTimeout(t);
  }, [measuredAll, autoGroupModel]);

  const saveRef = useRef(save);
  saveRef.current = save;  // the keyboard shortcut always saves the current graph

  const open = (file: File, handle: unknown = null) =>
    file.text().then((t) => {
      try {
        const g = JSON.parse(t) as GraphState & {
          library?: LibraryEntry[]; tabs?: Record<"model" | "results", GraphState>; view?: Tab;
        };
        let graph: GraphState, view: Tab;
        if (g.tabs) {  // saved with two tabs: their graphs become one
          const empty = { nodes: [], edges: [] };
          graph = mergeGraphs(migrate(g.tabs.model ?? empty), migrate(g.tabs.results ?? empty));
          view = "all";
        } else {  // one graph (files from before tabs hold a results graph)
          if (!Array.isArray(g.nodes) || !Array.isArray(g.edges)) throw new Error();
          graph = migrate({ nodes: g.nodes, edges: g.edges });
          view = g.view ?? "all";
        }
        if (nodes.length) {  // this tab has a graph: the file opens in its own tab
          onOpenNew(file.name.replace(/(\.abqflow)?\.json$/i, ""), graph,
            { view, fileHandle: handle, fileName: file.name, library: g.library });
          return;
        }
        loadInto(view, graph, `Opened ${file.name}`);
        fileHandle.current = handle;
        setFileName(file.name);
        saved.current = JSON.stringify(snapshot(graph.nodes, graph.edges));
        // Groups saved with the graph join My groups (yours win on a name clash).
        const extra = (g.library ?? []).filter((l) => !library.some((m) => m.name === l.name));
        if (extra.length) saveLibrary([...library, ...extra]);
      } catch {
        alert("That file is not a saved graph.");
      }
    });
  const openPicker = async () => {
    const w = window as any;  // eslint-disable-line @typescript-eslint/no-explicit-any
    if (!w.showOpenFilePicker) {
      fileInput.current?.click();
      return;
    }
    try {
      const [h] = await w.showOpenFilePicker({
        types: [{ description: "NodeCode FEA graph", accept: { "application/json": [".json"] } }],
      });
      open(await h.getFile(), h);  // Save then writes back to this file
    } catch (e) {
      if ((e as Error).name !== "AbortError") fileInput.current?.click();
    }
  };

  /** Open CAE model…: a .cae or an input file (.inp) read by Abaqus (in the background) becomes a graph that
   * rebuilds it, in a new tab. */
  const openCaeModel = async () => {
    let path: string | null = null;
    const ask = "Path of the Abaqus/CAE model database (.cae) or input file (.inp):";
    try {
      const r = await nativeBrowse("", [".cae", ".inp"]);
      path = r.available ? r.path : window.prompt(ask);
    } catch {
      path = window.prompt(ask);
    }
    if (!path) return;
    const file = path.split(/[\\/]/).pop()!;
    const name = file.replace(/\.(cae|inp)$/i, "");
    try {
      setToast({ text: `Reading ${file} with Abaqus/CAE… (this takes a minute)`, undo: false });
      let r = await openCae<GraphState>(path);
      if (!r.graph && r.models?.length) {
        const model = window.prompt(`${file} has several models: which one? (${r.models.join(", ")})`, r.models[0]);
        if (!model) return setToast(null);
        r = await openCae<GraphState>(path, model);
      }
      if (!r.graph) throw new Error("No model to open");
      onOpenNew(r.model && r.model !== name ? `${name} · ${r.model}` : name, r.graph,
                { view: "model", fileName: "", autoGroup: true });
      const n = r.notes?.length ?? 0;
      setToast({ text: `Opened ${file} as a graph` + (n ? `: ${n} thing${n === 1 ? "" : "s"} not rebuilt, listed on its "Not rebuilt" node` : ""), undo: false });
    } catch (e) {
      setToast({ text: `Couldn't open ${file}: ${(e as Error).message}`, undo: false });
    }
  };

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const box = workspace.current?.getBoundingClientRect();
    if (!box) return;
    const clamp = (v: number, size: number) => Math.round(Math.min(Math.max(v, 160), size - 160));
    const move = (ev: MouseEvent) =>
      layout.dock === "right"
        ? setLayout({ viewerWidth: clamp(box.right - ev.clientX, box.width) })
        : setLayout({ viewerHeight: clamp(box.bottom - ev.clientY, box.height) });
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  // ---- duplicate / copy / paste
  const clipboard = sharedClipboard as { current: GraphState | null };  // shared by the tabs: paste into another graph
  const [menu, setMenu] = useState<{ x: number; y: number; onNode: boolean } | null>(null);

  const insertCopy = useCallback(
    (g: GraphState, keepInputs: Edge[] = [], offset = { x: 40, y: 40 }) => {
      const idMap = new Map(g.nodes.map((n) => [n.id, newId(n.data.type)]));
      const copies = g.nodes.map((n) => ({
        ...n,
        id: idMap.get(n.id)!,
        position: { x: n.position.x + offset.x, y: n.position.y + offset.y },
        data: isGroup(n)
          ? ({ ...n.data, members: groupData(n).members.map((m) => idMap.get(m)).filter(Boolean) } as unknown as OpNodeData)
          : { ...n.data, params: { ...n.data.params } },
        selected: true,
      }));
      copies.sort((a, b) => Number(isGroup(b)) - Number(isGroup(a)));  // frames first, so they're drawn behind
      const wires = [...g.edges, ...keepInputs].filter((e) => idMap.has(e.target)).map((e) => {
        const source = idMap.get(e.source) ?? e.source; // wires from outside the copy keep their source
        const target = idMap.get(e.target)!;
        return { ...e, source, target, id: `${source}.${e.sourceHandle}-${target}.${e.targetHandle}` };
      });
      setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), ...withFreeNames(ns, copies)]);
      setEdges((es) => [...es, ...wires]);
      setSelected(copies.length === 1 ? copies[0].id : null);
    },
    [setNodes, setEdges],
  );

  const selectedIds = useCallback(() => {
    const ids = new Set(nodes.filter((n) => n.selected).map((n) => n.id));
    // A selected group brings its members (copy, duplicate); an expanded frame alone is just the frame.
    for (const n of nodes) if (isGroup(n) && n.selected && groupData(n).collapsed) groupData(n).members.forEach((m) => ids.add(m));
    for (const n of nodes) if (isGroup(n) && n.selected && !groupData(n).collapsed && groupData(n).members.some((m) => ids.has(m))) groupData(n).members.forEach((m) => ids.add(m));
    return ids;
  }, [nodes]);
  const subgraph = useCallback(
    (ids: Set<string>) => snapshot(nodes.filter((n) => ids.has(n.id)), edges.filter((e) => ids.has(e.source) && ids.has(e.target))),
    [nodes, edges],
  );

  /** Copies keep the wires between themselves and the wires feeding into them. */
  const duplicate = useCallback(() => {
    const ids = selectedIds();
    if (!ids.size) return;
    insertCopy(subgraph(ids), edges.filter((e) => !ids.has(e.source) && ids.has(e.target)));
  }, [selectedIds, subgraph, insertCopy, edges]);

  const copy = useCallback(() => {
    const ids = selectedIds();
    if (!ids.size) return;
    clipboard.current = subgraph(ids);
    setToast({ text: `Copied ${ids.size} node${ids.size > 1 ? "s" : ""}`, undo: false });
  }, [selectedIds, subgraph]);

  const paste = useCallback(() => {
    const g = clipboard.current;
    if (!g) return;
    insertCopy(g, [], { x: 60, y: 60 });
    // Repeated pastes cascade instead of stacking on top of each other.
    clipboard.current = { ...g, nodes: g.nodes.map((n) => ({ ...n, position: { x: n.position.x + 60, y: n.position.y + 60 } })) };
  }, [insertCopy]);

  const removeSelected = useCallback(() => {
    const ids = selectedIds();
    setNodes((ns) => ns.filter((n) => !ids.has(n.id)));
    setEdges((es) => es.filter((e) => !ids.has(e.source) && !ids.has(e.target)));
  }, [selectedIds, setNodes, setEdges]);

  const selectAll = useCallback(
    () => setNodes((ns) => ns.map((n) => ({ ...n, selected: !outOfView.has(n.id) }))),
    [setNodes, outOfView],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {  // also while typing in a field
        e.preventDefault();
        saveRef.current(e.shiftKey);
        return;
      }
      if ((e.target as HTMLElement).closest("input, select, textarea")) return;
      if (e.key === "Escape") setMenu(null);
      if ((e.key === "h" || e.key === "H") && !e.ctrlKey && !e.metaKey && selected) {
        e.preventDefault();
        cycleSize(selected);
      }
      if ((e.key === "l" || e.key === "L") && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        arrange(false);  // the selection, or everything when nothing (or one node) is selected
      }
      if ((e.key === "m" || e.key === "M") && !e.ctrlKey && !e.metaKey && selected) {
        e.preventDefault();
        toggleDisabled(selected);
      }
      if (e.key === "F2" && selected) {
        e.preventDefault();
        setRenaming(selected);
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "g") {
        e.preventDefault();
        if (e.altKey) autoGroupModel();
        else if (e.shiftKey) ungroupSelection();
        else groupSelection();
        return;
      }
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      if (e.key === "Enter") {
        e.preventDefault();
        updateNow();
        return;
      }
      const k = e.key.toLowerCase();
      const action = {
        d: duplicate,
        c: copy,
        v: paste,
        a: selectAll,
        b: () => setLayout({ paletteOpen: !layout.paletteOpen }),
        j: () => setLayout({ viewerOpen: !layout.viewerOpen }),
      }[k];
      if (action) {
        e.preventDefault();
        action();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [updateNow, duplicate, copy, paste, selectAll, layout.paletteOpen, layout.viewerOpen, setLayout, selected, toggleDisabled,
      groupSelection, ungroupSelection, autoGroupModel, cycleSize, arrange]);

  const browse = useCallback(
    async (nodeId: string, param: string, current: string, extensions?: string[]) => {
      setToast({ text: "Opening the file dialog… (check your taskbar if it doesn't come to the front)", undo: false });
      try {
        const r = await nativeBrowse(current, extensions);
        setToast(null);
        if (!r.available) setBrowsing({ nodeId, param, current, extensions }); // no native dialog: the built-in one
        else if (r.path) setParam(nodeId, param, r.path);
      } catch {
        setToast(null);
        setBrowsing({ nodeId, param, current, extensions });
      }
    },
    [setParam],
  );

  // ---- live link to Abaqus/CAE
  useEffect(() => {
    let alive = true;
    const tick = () =>
      caeStatus()
        .then((st) => alive && setCae(st))
        .catch(() => {});
    tick();
    const t = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Which Abaqus program Launch starts: Auto = Viewer for post-processing only, CAE when the graph models.
  const [product, setProduct] = useState<CaeProduct>(() => {
    try {
      return (localStorage.getItem("nodecode-fea.product") as CaeProduct) || "auto";
    } catch {
      return "auto";
    }
  });
  const chooseProduct = (p: CaeProduct) => {
    setProduct(p);
    try {
      localStorage.setItem("nodecode-fea.product", p);
    } catch {
      /* ignore */
    }
  };

  const launchCae = useCallback(() => {
    const what = product === "cae" ? "Abaqus/CAE" : product === "viewer" ? "Abaqus/Viewer" : "Abaqus";
    setToast({ text: `Starting ${what}… it shows the graph's outputs once it's up.`, undo: false });
    caeLaunch(caePayload, product)
      .then((st) => {
        setCae(st);
        syncedSignature.current = caeSignature;
      })
      .catch((e: Error) => setToast({ text: `Couldn't start CAE: ${e.message}`, undo: false }));
  }, [caePayload, caeSignature, product]);

  const leftNote = useRef("");
  const syncCae = useCallback(
    (sig: string, take = false) => {
      setSyncing(true);
      syncedSignature.current = sig;
      // take: Sync pressed here, so a model another tab built under the same name is rebuilt for this one;
      // automatic updates (Live edits, a running job) leave it, so two tabs never rebuild it over each other
      const graph = { ...(JSON.parse(sig) as GraphPayload), take };
      const send = async () => {
        let response = await caeSync(graph);
        const output = [response.output];
        // Some generic nodes ask CAE to identify geometry first (for example, one Region per circular face).
        // Re-evaluate and send the same graph once CAE has answered so one click creates its dependants too.
        for (let followUp = 0; followUp < 2 && response.ok !== false && !response.pending && response.results; followUp += 1) {
          setResults(response.results);
          response = await caeSync(graph);
          output.push(response.output);
        }
        if (response.results) setResults(response.results);
        return { ...response, output: output.filter(Boolean).join("\n") };
      };
      send()
        .then((r) => {
          if (r.ok === false) setToast({ text: `CAE update failed: ${(r.error ?? "").split("\n").filter(Boolean).pop()}`, undo: false });
          else {
            // One step can fail while the update as a whole runs (e.g. one instance CAE can't build): say so.
            const failed = (r.output ?? "").split("\n").filter((l) => / failed: /.test(l)).map((l) => l.replace("NodeCode FEA: ", ""));
            const left = (r.output ?? "").split("\n").find((l) => l.includes("left as another editor tab built them"));
            if (left && left !== leftNote.current) setToast({ text: `In CAE: ${left.replace("NodeCode FEA: ", "")}`, undo: false });
            leftNote.current = left ?? "";
            if (failed.length)
              setToast({ text: `In CAE: ${failed[0]}${failed.length > 1 ? ` (+${failed.length - 1} more)` : ""}. See Log.`, undo: false });
          }
          setLastSync(r.pending ? "sent" : `updated ${new Date().toLocaleTimeString()}`);
        })
        .catch((e: Error) => {
          // "Nothing to export" just means there are no output nodes yet.
          if (!/Nothing to export/.test(e.message)) setToast({ text: `CAE update failed: ${e.message}`, undo: false });
        })
        .finally(() => setSyncing(false));
    },
    [],
  );

  // Live: push the graph to CAE shortly after it changes and has been evaluated.
  useEffect(() => {
    // (Auto update off: CAE waits for Update too)
    // (a pick in CAE: an update would switch CAE's module and cancel it, so Live waits for the pick)
    if (!live || outdated || picking || cae.state !== "connected" || running || syncing || caeSignature === syncedSignature.current) return;
    const t = setTimeout(() => syncCae(caeSignature), 1200);
    return () => clearTimeout(t);
  }, [live, outdated, picking, cae.state, running, syncing, caeSignature, results, syncCae]);

  // Job's Results: the Job wired into Open ODB, shown in the Results view (set below, once the view helpers exist)
  const openResults = useRef<(jobId: string, message: string) => void>(() => {});
  const nodeAction = useCallback(
    (nodeId: string, action: string) => {
      nodeActionRequest(JSON.parse(signature) as GraphPayload, nodeId, action)
        .then((r) => {
          setJobRevision(r.revision);
          const message = r.notes.join(" · ") || `${action}: done`;
          if (r.open?.length) openResults.current(nodeId, message);
          else setToast({ text: message, undo: false });
        })
        .catch((e: Error) => setToast({ text: `${action} failed: ${e.message}`, undo: false }));
    },
    [signature],
  );

  const pickInCae = useCallback(
    async (nodeId: string) => {
      const node = nodes.find((n) => n.id === nodeId);
      // a setting left at its default isn't always stored (examples, imported or migrated nodes): read it as the
      // card shows it, or a Region showing "Any geometry" would pick nodes
      const setting = (name: string) =>
        node?.data.params[name] ?? defs[String(node?.data.type)]?.params.find((p) => p.name === name)?.default;
      const inputType = String(setting("type") ?? "");
      const kind = inputType.endsWith(" in CAE") ? inputType.slice(0, -7).toLowerCase() :
        String(setting("kind") ?? "node").toLowerCase();
      const pickKind = kind === "any geometry" ? "geometry" : kind;
      setPicking(nodeId); // before asking CAE, so no Live update slips in and cancels the pick
      try {
        const { id } = await caePick(pickKind);
        setToast({ text: pickKind === "geometry" ? "Click faces, edges, vertices, or cells in the CAE viewport, then click Done there." :
          `Click ${kind === "node" || kind === "element" ? `${kind}s` : `geometry on one ${kind}`} in the CAE viewport, then click Done there.`, undo: false });
        const end = Date.now() + 10 * 60 * 1000;
        while (Date.now() < end) {
          await new Promise((r) => setTimeout(r, 700));
          const r = await caeResult(id);
          if (!r.done || !r.result) continue;
          if (r.result.ok && r.result.labels) {
            setParam(nodeId, "labels", r.result.labels.join(", "));
            setToast({ text: `Picked ${r.result.labels.length} ${kind}(s) in CAE.`, undo: false });
          } else if (r.result.ok && r.result.value) {
            const actualKind = String(r.result.kind ?? kind);
            const geometry = actualKind === "face" || actualKind === "edge" || actualKind === "vertex" || actualKind === "cell";
            if (pickKind === "geometry" && geometry)
              setParam(nodeId, "kind", actualKind.charAt(0).toUpperCase() + actualKind.slice(1));
            setParam(nodeId, "value", geometry ? r.result.value.split("|").slice(-1)[0] : r.result.value);
            setToast({ text: geometry ? `Picked ${actualKind} geometry from CAE.` :
              `Selected ${kind} ${r.result.value.split("|").slice(1, 2)[0]} from CAE.`, undo: false });
          } else if (r.result.cancelled) {
            setToast({ text: r.result.error ?? "Pick cancelled in CAE.", undo: false });
          } else {
            setToast({ text: `Pick failed: ${r.result.error ?? "see the CAE link log"}`, undo: false });
          }
          return;
        }
        setToast({ text: "Gave up waiting for the pick in CAE.", undo: false });
      } catch (e) {
        setToast({ text: `Couldn't start the pick: ${(e as Error).message}`, undo: false });
      } finally {
        setPicking(null);
      }
    },
    [nodes, defs, setParam],
  );

  const startPaletteResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = layout.paletteHeight;
    const move = (ev: MouseEvent) =>
      setLayout({ paletteHeight: Math.round(Math.min(Math.max(startH + startY - ev.clientY, 90), window.innerHeight * 0.5)) });
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const palette = (
    <Palette
      catalog={{ ...catalog, nodes: catalog.nodes.filter((n) => !hiddenInTab(tab, n)) }}
      onAdd={(t) => addNode(t)}
      library={library}
      onAddGroup={(i: number) => addFromLibrary(library[i])}
      onRemoveGroup={(i) => saveLibrary(library.filter((_, k) => k !== i))}
      onCollapse={() => setLayout({ paletteOpen: false })}
      dock={layout.paletteDock}
      onDock={() => setLayout({ paletteDock: layout.paletteDock === "bottom" ? "left" : "bottom" })}
    />
  );
  const paletteStrip = (
    <button
      className={`side-strip palette-strip strip-${layout.paletteDock}`}
      onClick={() => setLayout({ paletteOpen: true })}
      title="Show the nodes (Ctrl+B)"
    >
      <span>{layout.paletteDock === "bottom" ? "˄" : "›"}</span>
      <span className="strip-label">Nodes</span>
    </button>
  );
  const paletteArea =
    layout.paletteDock === "left" ? (
      layout.paletteOpen ? palette : paletteStrip
    ) : layout.paletteOpen ? (
      <div className="palette-bottom-dock" style={{ height: layout.paletteHeight }}>
        <div className="splitter palette-splitter" onMouseDown={startPaletteResize} title="Drag to resize" />
        {palette}
      </div>
    ) : (
      paletteStrip
    );

  const wiredParams = useMemo(
    () => new Set(edges.map((e) => (e.targetHandle?.startsWith("param:")
      ? `${e.target}|${e.targetHandle.slice(6)}` : `${e.target}|>${e.targetHandle ?? ""}`))),
    [edges],
  );

  const openPortMenu = useCallback(
    (rawId: string, rawHandle: string, side: "in" | "out", x: number, y: number) => {
      const [nodeId, h] = realEnd(nodes, rawId, rawHandle);
      const handle = h ?? "";
      const port = portType(nodeId, handle, side);
      if (port) setPortMenu({ nodeId, handle, side, x, y, type: port.type });
    },
    [portType, nodes],
  );

  const pickPort = useCallback(
    (req: PortMenuRequest, choice: PortChoice) => {
      setPortMenu(null);
      const conn: Connection =
        req.side === "in"
          ? { source: choice.nodeId, sourceHandle: choice.handle, target: req.nodeId, targetHandle: req.handle }
          : { source: req.nodeId, sourceHandle: req.handle, target: choice.nodeId, targetHandle: choice.handle };
      if (choice.connected) {
        setEdges((es) =>
          es.filter((e) => !(e.source === conn.source && e.sourceHandle === conn.sourceHandle &&
            e.target === conn.target && e.targetHandle === conn.targetHandle)),
        );
      } else {
        onConnect(conn);
      }
    },
    [onConnect, setEdges],
  );

  // The data panel shows the pinned node (kept while you edit others; it updates live), else the selected one.
  const [pinned, setPinned] = useState<string | null>(null);
  // The App view: the graph as a form of its inputs and a page of its results.
  const [appMode, setAppMode] = useState(() => {
    try {
      return localStorage.getItem(APP_MODE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const showApp = (on: boolean) => {
    setAppMode(on);
    try {
      localStorage.setItem(APP_MODE_KEY, on ? "1" : "0");
    } catch {
      /* not remembered */
    }
  };
  const setInApp = (id: string, show: boolean | undefined) =>
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, app: show } } : n)));
  // A setting as a field of the app: an Input with its current value, wired into it, named after it.
  const exposeParam = (nodeId: string, param: string) => {
    const target = nodes.find((n) => n.id === nodeId);
    const def = target && defs[target.data.type];
    const p = def?.params.find((x) => x.name === param);
    const idef = defs["input.value"];
    if (!target || !def || !p || !idef) return;
    const numeric = p.kind === "number" || p.kind === "int";
    const id = newId("input.value");
    const params = {
      ...Object.fromEntries(idef.params.map((q) => [q.name, q.default])),
      type: numeric ? "Number" : "Text",
      value: String(target.data.params[param] ?? p.default),
    };
    const owner = target.data.name || String(target.data.params.name || "") || def.title;
    setNodes((ns) => [
      ...ns,
      { id, type: "op", position: { x: target.position.x - 280, y: target.position.y }, data: { type: "input.value", params, name: `${owner} ${p.label}` } },
    ]);
    setEdges((es) =>
      addEdge({ source: id, sourceHandle: "value", target: nodeId, targetHandle: `param:${param}`, id: `${id}.value-${nodeId}.param:${param}` }, es),
    );
  };
  // ---- views: Modelling, Results or All of the one graph (nothing is swapped: hidden nodes still run)
  const switchTab = (t: Tab) => {
    if (t === tab) return;
    setNodes((ns) => ns.map((n) => (n.selected ? { ...n, selected: false } : n)));
    if (selected && outOfView.has(selected)) setSelected(null);
    setTab(t);
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch {
      /* ignore */
    }
    setTimeout(() => flow.fitView({ padding: 0.1, maxZoom: 1.2 }), 60);
  };
  /** Replace the graph (an example, a file) and show it in a view. */
  const loadInto = (t: Tab, g: GraphState, message: string) => {
    replaceGraph(g, message);
    setTab(t);
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch {
      /* ignore */
    }
  };
  /** Post-process a job: its ODB path wired into Open ODB (one already wired to it, else the first Open ODB not fed
   * by another Job, its typed path or wire replaced; else a new one next to the Job), then the Results view. */
  openResults.current = (jobId: string, message: string) => {
    const job = nodes.find((n) => n.id === jobId);
    if (!job) return;
    const opens = nodes.filter((n) => n.data.type === "odb.open");
    const intoPath = (e: Edge, id: string) => e.target === id && (e.targetHandle === "path" || e.targetHandle === "param:path");
    const fedByJob = (id: string) =>
      edges.some((e) => intoPath(e, id) && nodes.find((n) => n.id === e.source)?.data.type === "model.job");
    let target = opens.find((o) => edges.some((e) => intoPath(e, o.id) && e.source === jobId));
    commit();
    if (!target) {
      target = opens.find((o) => !fedByJob(o.id));
      if (!target) {
        target = { id: newId("odb.open"), type: "op", position: { x: job.position.x + 420, y: job.position.y },
                   data: { type: "odb.open", params: {} } };
        setNodes((ns) => [...ns, target!]);
      }
      const id = target.id;
      setEdges((es) => [
        ...es.filter((e) => !intoPath(e, id)),
        { id: `${jobId}.path-${id}.path`, source: jobId, sourceHandle: "path", target: id, targetHandle: "path" },
      ]);
    }
    setTimeout(commit, 0);
    setSelected(target.id);
    switchTab("results");
    setToast({ text: message, undo: true });
  };

  const shownRaw = pinned && nodes.some((n) => n.id === pinned) ? pinned : selected;
  const shownGroup = nodes.find((n) => n.id === shownRaw && isGroup(n));
  const shown = shownGroup ? groupFocus(shownGroup, nodes, edges, defs) : shownRaw;
  const selectedNode = nodes.find((n) => n.id === shown);
  const selectionCount = nodes.filter((n) => n.selected).length;
  // A typed list or matrix (Input) is edited as a grid in the data panel.
  const shownNode = nodes.find((n) => n.id === shown);
  const matrixEditor =
    shownNode && shownNode.data.type === "input.value" && shownNode.data.params.type === "List / matrix" &&
    parseGrid(String(shownNode.data.params.value ?? "")) ? (
      <MatrixEditor
        value={String(shownNode.data.params.value ?? "")}
        onChange={(v) => setParam(shownNode.id, "value", v)}
      />
    ) : undefined;
  // nodes with model-check warnings, in graph order; the pill steps through them
  const warned = nodes.filter((n) => results[n.id]?.warnings?.length).map((n) => n.id);
  const nextWarning = () => {
    const at = warned.indexOf(selected ?? "");
    goToNode(warned[(at + 1) % warned.length]);
  };
  const goToNode = (id: string) => {
    setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === id })));
    setSelected(id);
    const node = flow.getNode(id);
    if (node) {
      const w = node.measured?.width ?? 240, h = node.measured?.height ?? 200;
      flow.setCenter(node.position.x + w / 2, node.position.y + h / 2, { zoom: 1, duration: 300 });
    }
  };
  const counts = Object.values(results).reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.status]: (acc[r.status] ?? 0) + 1 }), {});

  return (
    <EditorContext.Provider
      value={{
        defs,
        results,
        setParam,
        browse,
        caeConnected: cae.state === "connected",
        pickInCae,
        picking,
        nodeAction,
        wiredParams,
        openPortMenu,
        setName,
        renaming,
        setRenaming,
        toggleDisabled,
        setGroup,
        cycleSize,
        setWidth,
      }}
    >
      <div
        className={`app palette-${layout.paletteDock}`}
        style={
          layout.paletteDock === "left"
            ? { gridTemplateColumns: `${layout.paletteOpen ? 220 : 30}px minmax(0, 1fr)` }
            : { gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: "48px 1fr auto" }
        }
      >
        <header className="topbar" onWheel={(e) => {
          const bar = e.currentTarget;  // too narrow for everything: the wheel scrolls the bar sideways
          if (e.deltaY && bar.scrollWidth > bar.clientWidth) bar.scrollLeft += e.deltaY;
        }}>
          <div className="brand">
            <span className="logo" />
            <span className="brand-name">NodeCode <b>FEA</b></span>
            <span className="tabs" role="tablist">
              <button role="tab" className={tab === "model" ? "on" : ""} onClick={() => switchTab("model")}
                title="Show the modelling side of the graph: parts, assembly, steps, loads, jobs (Abaqus/CAE). The rest still runs.">
                Modelling
              </button>
              <button role="tab" className={tab === "results" ? "on" : ""} onClick={() => switchTab("results")}
                title="Show the results side of the graph: ODBs, fields, curves, plots (Abaqus/Viewer or CAE)">
                Results
              </button>
              <button role="tab" className={tab === "all" ? "on" : ""} onClick={() => switchTab("all")}
                title="The whole graph: the model, its jobs and their results, wired together">
                All
              </button>
            </span>
            <button className={`app-toggle ${appMode ? "on" : ""}`} onClick={() => showApp(!appMode)}
              title={appMode ? "Back to the graph" : "Use this graph as an app: its inputs as a form, its results as a page (and a report to share)"}>
              {appMode ? "◇ Graph" : "▤ App"}
            </button>
            {fileName && <span className="file-name" title="Ctrl+S saves to this file">{fileName}</span>}
          </div>
          <div className="run-status">
            <label className="auto-run" title="Auto update: run the graph after every change. Off: changes wait until you press Update (Ctrl+Enter), for heavy graphs (big ODBs, long lists).">
              <input type="checkbox" checked={autoRun} onChange={(e) => setAutoRun(e.target.checked)} /> Auto
            </label>
            {!autoRun && (
              <button className={`pill pill-update ${outdated ? "due" : ""}`} onClick={updateNow} disabled={running}
                title={outdated ? "The graph changed since its results were computed: run it now (Ctrl+Enter)" : "Run the graph again (Ctrl+Enter)"}>
                ⟳ Update{outdated ? " ●" : ""}
              </button>
            )}
            {runError ? (
              <span className="pill pill-error">Backend unreachable: {runError}</span>
            ) : running ? (
              <span className="pill pill-run">Running…</span>
            ) : (
              <span className="pill pill-ok">
                {counts.ok ?? 0} ok{counts.error ? ` · ${counts.error} error` : ""}
                {counts.waiting ? ` · ${counts.waiting} waiting` : ""}
              </span>
            )}
            {warned.length > 0 && (
              <button className="pill pill-warn" onClick={nextWarning}
                title={"Model checks: what Abaqus would refuse or get wrong. Click to go to each node.\n\n" +
                  warned.map((id) => {
                    const n = nodes.find((x) => x.id === id);
                    return `• ${n?.data.name || defs[n?.data.type ?? ""]?.title || id}: ${results[id].warnings![0]}`;
                  }).join("\n")}>
                ⚠ {warned.length} to check
              </button>
            )}
          </div>
          {abaqus && !abaqus.startsWith("Abaqus: ") && <span className="abaqus-status missing">{abaqus}</span>}
          <div className="actions">
            <CaeLinkBar
              abaqus={abaqus}
              status={cae}
              live={live}
              syncing={syncing}
              lastSync={lastSync}
              onLaunch={launchCae}
              product={product}
              onProduct={chooseProduct}
              onSync={() => syncCae(caeSignature, true)}
              onLive={setLive}
            />
            <button onClick={() => save()} title="Save to the same file (Ctrl+S); Ctrl+Shift+S saves as a new file">
              Save
            </button>
            <button onClick={openPicker}>Open</button>
            <button onClick={openCaeModel} title="Open an Abaqus/CAE model database (.cae) or input file (.inp) as a graph of modelling nodes that rebuilds it, in a new tab (needs Abaqus; what can't be rebuilt is listed in the graph)">
              Open CAE model…
            </button>
            <button onClick={undo} disabled={!past.current.length && committed.current === JSON.stringify(snapshot(nodes, edges))} title="Undo (Ctrl+Z)">
              ↶
            </button>
            <button onClick={redo} disabled={!future.current.length} title="Redo (Ctrl+Shift+Z)">
              ↷
            </button>
            <select
              className="examples-menu"
              value=""
              title="Ready-made graphs (they replace the graph; Undo brings yours back): modelling ones open in the Modelling view, solved ones (model, job and the answer it should give) in All, results ones in Results (on the demo ODB; change Open ODB's path to use your own)"
              onChange={(e) => {
                const [where, i] = e.target.value.split(":");
                const ex = (where === "model" ? MODEL_EXAMPLES : where === "solved" ? SOLVED_EXAMPLES : EXAMPLES)[Number(i)];
                const view = where === "model" ? "model" : where === "solved" ? "all" : "results";
                if (ex) loadInto(view, ex.build(), `Loaded the example "${ex.title}"`);
              }}
            >
              <option value="" disabled>Examples…</option>
              <optgroup label="Modelling">
                {MODEL_EXAMPLES.map((ex, i) => (
                  <option key={ex.title} value={`model:${i}`}>{ex.title}</option>
                ))}
              </optgroup>
              <optgroup label="Solved in Abaqus (model, job and answer)">
                {SOLVED_EXAMPLES.map((ex, i) => (
                  <option key={ex.title} value={`solved:${i}`}>{ex.title}</option>
                ))}
              </optgroup>
              <optgroup label="Results">
                {EXAMPLES.map((ex, i) => (
                  <option key={ex.title} value={`results:${i}`}>{ex.title}</option>
                ))}
              </optgroup>
            </select>
            <button onClick={() => (nodes.length ? setConfirmClear(true) : undefined)} disabled={!nodes.length}>
              Clear
            </button>
            <input
              ref={fileInput}
              type="file"
              accept=".json"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) open(f);
                e.target.value = "";
              }}
            />
          </div>
        </header>
        {layout.paletteDock === "left" && !appMode && paletteArea}
        {appMode && (
          <AppView
            nodes={nodes}
            edges={edges}
            title={fileName?.replace(/\.json$/i, "") || (tab === "model" ? "Model" : tab === "results" ? "Results" : "Project")}
            running={running}
            onShow={setInApp}
            onExpose={exposeParam}
            onEdit={(id) => {
              showApp(false);
              setTimeout(() => goToNode(id), 50);
            }}
          />
        )}
        <div className={`workspace dock-${layout.dock}`} ref={workspace} style={appMode ? { display: "none" } : undefined}>
        <main className="canvas" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
          <ReactFlow
            nodes={viewNodes}
            edges={styledEdges}
            nodeTypes={nodeTypes}
            onNodesChange={handleNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onConnectEnd={onConnectEnd}
            connectOnClick={false}
            zoomOnDoubleClick={false}
            onDoubleClick={(e: React.MouseEvent) => {
              // Double-click on empty canvas: search for a node to add at that spot (like dropping a wire there).
              if (!(e.target as HTMLElement).classList.contains("react-flow__pane")) return;
              setMenu(null);
              setQuickAdd({ x: e.clientX, y: e.clientY, fromNode: "", fromHandle: "", fromSide: "source", type: "any" });
            }}
            onNodeDragStop={dropIntoFrame}
            onNodeContextMenu={(e, node) => {
              e.preventDefault();
              if (!node.selected) {
                setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === node.id })));
                setSelected(node.id);
              }
              setMenu({ x: e.clientX, y: e.clientY, onNode: true });
            }}
            onPaneContextMenu={(e) => {
              e.preventDefault();
              setMenu({ x: e.clientX, y: e.clientY, onNode: false });
            }}
            onMoveStart={() => {
              setMenu(null);
              setQuickAdd(null);
            }}
            multiSelectionKeyCode={["Control", "Meta"]}
            // Drag on empty canvas draws a selection rectangle (touching a node is enough);
            // pan with the middle mouse button, or hold Space and drag.
            selectionOnDrag
            selectionMode={SelectionMode.Partial}
            panOnDrag={[1]}
            panActivationKeyCode="Space"
            connectionRadius={40}
            isValidConnection={isValidConnection}
            onSelectionChange={({ nodes: sel }) => sel.length === 1 && setSelected(sel[0].id)}
            onPaneClick={() => {
              // Not closing the quick-add menu here: releasing a wire over the canvas also fires a click,
              // which would close the menu as it opens. QuickAdd closes itself on a real click elsewhere.
              setSelected(null);
              setMenu(null);
            }}
            fitView
            fitViewOptions={{ padding: 0.1, maxZoom: 1.2 }}
            minZoom={0.03}
            deleteKeyCode={["Backspace", "Delete"]}
            snapToGrid={snap}
            snapGrid={[20, 20]}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} color="#3a4256" />
            <Controls>
              <ControlButton onClick={() => arrange(true)} title="Arrange all nodes in columns following the wires (L)">
                ⇶
              </ControlButton>
              <ControlButton onClick={autoGroupModel}
                title="Auto-group: a frame per part, the assembly, each step, the job, the shared inputs and the outputs, laid out in order (Ctrl+Alt+G)">
                ▣
              </ControlButton>
              <ControlButton onClick={toggleSnap} title={snap ? "Snap to grid: on" : "Snap to grid: off"}
                className={snap ? "control-on" : ""}>
                #
              </ControlButton>
            </Controls>
            {selectedOps.length > 1 && (
            <Panel position="top-center">
              <div className="align-bar">
                <span className="align-count">{selectedOps.length} selected</span>
                <button onClick={() => alignSel("left")} title="Align left edges">⇤</button>
                <button onClick={() => alignSel("center")} title="Align centres (vertical line)">↔</button>
                <button onClick={() => alignSel("right")} title="Align right edges">⇥</button>
                <span />
                <button onClick={() => alignSel("top")} title="Align tops">⤒</button>
                <button onClick={() => alignSel("middle")} title="Align middles (horizontal line)">↕</button>
                <button onClick={() => alignSel("bottom")} title="Align bottoms">⤓</button>
                <span />
                <button onClick={() => distributeSel("x")} title="Even gaps, left to right" disabled={selectedOps.length < 3}>⋯</button>
                <button onClick={() => distributeSel("y")} title="Even gaps, top to bottom" disabled={selectedOps.length < 3}>⋮</button>
                <span />
                <button onClick={() => arrange(false)} title="Arrange these nodes in columns following the wires (L)">⇶</button>
                <button onClick={() => selectedOps[0] && cycleSize(selectedOps[0])} title="Shrink / grow these nodes (H)">▭</button>
                <button onClick={() => groupSelection()} title="Frame these nodes (Ctrl+G)">▣</button>
              </div>
            </Panel>
            )}
            <MiniMap
              pannable
              zoomable
              nodeColor={(n) => CATEGORY_COLORS[defs[(n.data as OpNodeData).type]?.category] ?? "#666"}
              maskColor="rgba(10,14,24,0.6)"
            />
          </ReactFlow>
        </main>
        {layout.viewerOpen ? (
          <>
            <div className="splitter" onMouseDown={startResize} title="Drag to resize" />
            <div
              className="viewer-dock"
              style={layout.dock === "right" ? { width: layout.viewerWidth } : { height: layout.viewerHeight }}
            >
              <Viewer
                def={selectedNode ? defs[selectedNode.data.type] : undefined}
                name={selectedNode?.data.name}
                nodeId={shown ?? undefined}
                result={shown ? results[shown] : undefined}
                inspect={shown ? (path) => inspectNode(payload, shown, path) : undefined}
                pinned={!!pinned && pinned === shown}
                editor={matrixEditor}
                onPin={shown ? () => setPinned((p) => (p ? null : shown)) : undefined}
                dock={layout.dock}
                onDock={() => setLayout({ dock: layout.dock === "right" ? "bottom" : "right" })}
                onCollapse={() => setLayout({ viewerOpen: false })}
              />
            </div>
          </>
        ) : (
          <button className="side-strip viewer-strip" onClick={() => setLayout({ viewerOpen: true })} title="Show the data panel (Ctrl+J)">
            <span>{layout.dock === "right" ? "‹" : "˄"}</span>
            <span className="strip-label">Data</span>
          </button>
        )}
        </div>
        {layout.paletteDock === "bottom" && !appMode && paletteArea}
      </div>
      {menu && (
        <div className="context-menu" style={{ left: menu.x, top: menu.y }} onMouseLeave={() => setMenu(null)}>
          {menu.onNode ? (
            <>
              {selectionCount <= 1 && selected && (
                <button onClick={() => (setRenaming(selected), setMenu(null))}>
                  Rename… <kbd>F2</kbd>
                </button>
              )}
              {(() => {
                const g = nodes.find((n) => n.selected && isGroup(n));
                return g ? (
                  <>
                    <button onClick={() => (setGroup(g.id, { collapsed: !groupData(g).collapsed }), setMenu(null))}>
                      {groupData(g).collapsed ? "Expand group" : "Collapse group"}
                    </button>
                    <button onClick={() => (setRenaming(g.id), setMenu(null))}>Rename group…</button>
                    <button onClick={() => (saveGroupAsNode(g.id), setMenu(null))}>Save as node (My groups)</button>
                    <button onClick={() => (ungroupSelection(), setMenu(null))}>
                      Ungroup <kbd>Ctrl+Shift+G</kbd>
                    </button>
                  </>
                ) : (
                  <>
                    <button onClick={() => (groupSelection(), setMenu(null))}>
                      Group{selectionCount > 1 ? ` ${selectionCount} nodes` : ""} <kbd>Ctrl+G</kbd>
                    </button>
                    {(() => {
                      const ofNode = membership(nodes);
                      return nodes.some((n) => n.selected && ofNode.has(n.id)) && (
                        <button onClick={() => (ungroupSelection(), setMenu(null))}>
                          Remove from group <kbd>Ctrl+Shift+G</kbd>
                        </button>
                      );
                    })()}
                  </>
                );
              })()}
              {selected && nodes.find((n) => n.id === selected)?.type === "op" && (
                <button onClick={() => (toggleDisabled(selected), setMenu(null))}>
                  {nodes.find((n) => n.id === selected)?.data.disabled ? "Activate" : "Deactivate"}
                  {selectionCount > 1 ? ` ${selectionCount} nodes` : ""} <kbd>M</kbd>
                </button>
              )}
              {selected && nodes.find((n) => n.id === selected)?.type === "op" && (
                <button onClick={() => (toggleCombine(selected), setMenu(null))}
                  title="When two or more lists come in: pair them up item by item (3 widths, 3 heights: 3 runs), or use every combination (3 widths, 4 heights: a 3 × 4 grid)">
                  Lists: {nodes.find((n) => n.id === selected)?.data.combine === "all" ? "pair up" : "all combinations"}
                  {selectionCount > 1 ? ` (${selectionCount} nodes)` : ""}
                </button>
              )}
              {selected && nodes.find((n) => n.id === selected)?.type === "op" && (
                <button onClick={() => (toggleWhole(selected), setMenu(null))}
                  title="When a grid (a list of lists) comes in: run once per row (Count gives one count per row), or take it whole (Count gives the number of rows, Pick 0 the first row)">
                  Lists: {nodes.find((n) => n.id === selected)?.data.levels === "whole" ? "once per row" : "take whole"}
                  {selectionCount > 1 ? ` (${selectionCount} nodes)` : ""}
                </button>
              )}
              {selected && nodes.find((n) => n.id === selected)?.type === "op" && (
                <button onClick={() => (cycleSize(selected), setMenu(null))}>
                  Shrink / grow{selectionCount > 1 ? ` ${selectionCount} nodes` : ""} <kbd>H</kbd>
                </button>
              )}
              {selectionCount > 1 && (
                <button onClick={() => (arrange(false), setMenu(null))}>
                  Arrange {selectionCount} nodes <kbd>L</kbd>
                </button>
              )}
              <button onClick={() => (duplicate(), setMenu(null))}>
                Duplicate{selectionCount > 1 ? ` ${selectionCount} nodes` : ""} <kbd>Ctrl+D</kbd>
              </button>
              <button onClick={() => (copy(), setMenu(null))}>
                Copy <kbd>Ctrl+C</kbd>
              </button>
              <button onClick={() => (removeSelected(), setMenu(null))}>
                Delete <kbd>Del</kbd>
              </button>
            </>
          ) : (
            <>
              <button disabled={!clipboard.current} onClick={() => (paste(), setMenu(null))}>
                Paste <kbd>Ctrl+V</kbd>
              </button>
              <button onClick={() => (selectAll(), setMenu(null))}>
                Select all <kbd>Ctrl+A</kbd>
              </button>
              <button onClick={() => (arrange(true), setMenu(null))}>
                Arrange all <kbd>L</kbd>
              </button>
              <button onClick={() => (setCaeExport(caePayload), setMenu(null))}
                title="Download a script that recreates these results in any CAE session (File → Run Script…)">
                CAE script…
              </button>
            </>
          )}
        </div>
      )}
      {portMenu && (
        <PortMenu
          req={portMenu}
          nodes={nodes}
          edges={edges}
          defs={defs}
          results={results}
          onPick={(c) => pickPort(portMenu, c)}
          onNewNode={() => {
            setPortMenu(null);
            setQuickAdd({
              x: portMenu.x,
              y: portMenu.y,
              fromNode: portMenu.nodeId,
              fromHandle: portMenu.handle,
              fromSide: portMenu.side === "in" ? "target" : "source",
              type: portMenu.type,
            });
          }}
          onClose={() => setPortMenu(null)}
        />
      )}
      {quickAdd && (
        <QuickAdd
          req={quickAdd}
          defs={catalog.nodes.filter((n) => !n.hidden && !hiddenInTab(tab, n))}
          allDefs={catalog.nodes}
          categories={catalog.categories}
          existing={!quickAdd.fromNode ? [] : existingPorts(
            { nodeId: quickAdd.fromNode, handle: quickAdd.fromHandle, side: quickAdd.fromSide === "target" ? "in" : "out", type: quickAdd.type },
            nodes, edges, defs, results,
          )}
          onPickExisting={(c) => {
            setQuickAdd(null);
            pickPort(
              { nodeId: quickAdd.fromNode, handle: quickAdd.fromHandle, side: quickAdd.fromSide === "target" ? "in" : "out",
                type: quickAdd.type, x: quickAdd.x, y: quickAdd.y },
              c,
            );
          }}
          onPick={(c) => pickQuickAdd(quickAdd, c)}
          onClose={() => setQuickAdd(null)}
        />
      )}
      {caeExport && <CaeExportDialog graph={caeExport} onClose={() => setCaeExport(null)} />}
      {browsing && (
        <FileBrowser
          current={browsing.current}
          extensions={browsing.extensions}
          onClose={() => setBrowsing(null)}
          onPick={(path) => {
            setParam(browsing.nodeId, browsing.param, path);
            setBrowsing(null);
          }}
        />
      )}
      {confirmClear && (
        <div className="modal-backdrop" onMouseDown={() => setConfirmClear(false)}>
          <div className="modal confirm" onMouseDown={(e) => e.stopPropagation()}>
            <b>Clear the whole graph?</b>
            <p className="muted">
              This removes all {nodes.length} nodes and {edges.length} connections. You can bring them back with Undo (Ctrl+Z).
            </p>
            <div className="modal-foot">
              <span style={{ flex: 1 }} />
              <button autoFocus onClick={() => setConfirmClear(false)}>
                Cancel
              </button>
              <button
                className="danger"
                onClick={() => {
                  setConfirmClear(false);
                  replaceGraph({ nodes: [], edges: [] }, "Graph cleared");
                }}
              >
                Clear
              </button>
            </div>
          </div>
        </div>
      )}
      {toast && (
        <div className="toast">
          {toast.text}
          {toast.undo && <button onClick={undo}>Undo</button>}
          <button className="icon-btn" onClick={() => setToast(null)}>
            ✕
          </button>
        </div>
      )}
    </EditorContext.Provider>
  );
}

export default function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [abaqus, setAbaqus] = useState<string>("");

  useEffect(() => {
    fetchCatalog().then(setCatalog).catch((e: Error) => setError(e.message));
    fetchStatus()
      .then((s) => setAbaqus(s.abaqusFound ? `Abaqus: ${s.abaqusCommand}` : "Abaqus not found — mock ODBs only"))
      .catch(() => {});
  }, []);

  if (error) {
    return (
      <div className="splash">
        <h2>Can't reach the backend</h2>
        <p>{error}</p>
        <p>
          Start it with <code>nodecode-fea</code> (or <code>python -m nodecode_fea.app</code>) and reload.
        </p>
      </div>
    );
  }
  if (!catalog) return <div className="splash">Loading…</div>;
  return <Tabs catalog={catalog} abaqus={abaqus} />;
}

/** Several graphs open at once, one per tab (two models, a script and a model…); one editor shows the active one. */
function Tabs({ catalog, abaqus }: { catalog: Catalog; abaqus: string }) {
  const [index, setIndex] = useState(loadIndex);
  const { docs, active } = index;
  const [status, setStatus] = useState<Record<string, DocStatus>>({});
  useEffect(() => saveIndex(docs, active), [docs, active]);
  // the models tabs not shown build, from their stored graphs (the shown tab reports its own as it changes)
  useEffect(() => {
    setStatus((st) => {
      const next = { ...st };
      for (const d of docs) {
        if (!next[d.id]) next[d.id] = { dirty: false, fileName: null, models: modelNames(readDoc(d.id)?.nodes ?? []) };
      }
      return next;
    });
  }, [docs]);
  const onStatus = useCallback((id: string, s: DocStatus) => setStatus((st) => ({ ...st, [id]: s })), []);

  const clashes = useMemo(() => {
    const out: Record<string, string[]> = {};
    for (const d of docs) {
      const mine = status[d.id]?.models ?? [];
      const others = new Set(docs.filter((o) => o.id !== d.id).flatMap((o) => status[o.id]?.models ?? []));
      const both = mine.filter((m) => others.has(m));
      if (both.length) out[d.id] = both;
    }
    return out;
  }, [docs, status]);

  const select = (id: string) => setIndex((ix) => ({ ...ix, active: id }));
  const nextName = () => {
    let n = docs.length + 1;
    while (docs.some((d) => d.name === `Graph ${n}`)) n++;
    return `Graph ${n}`;
  };
  const add = (name: string, g?: GraphState) => {
    const id = newDocId();
    writeDoc(id, g ?? { nodes: [], edges: [] });
    setIndex((ix) => ({ docs: [...ix.docs, { id, name }], active: id }));
    return id;
  };
  const close = (id: string) => {
    const d = docs.find((x) => x.id === id);
    const s = status[id];
    const empty = !(readDoc(id)?.nodes.length);
    const unsaved = s?.dirty || (!s?.fileName && !empty);
    if (unsaved && !window.confirm(`Close "${d?.name}"? ${s?.dirty ? "Its changes since it was saved" : "It isn't saved to a file, so it"} will be lost.`)) return;
    sessions.delete(id);
    dropDoc(id);
    setStatus((st) => {
      const { [id]: _gone, ...rest } = st;  // eslint-disable-line @typescript-eslint/no-unused-vars
      return rest;
    });
    setIndex((ix) => {
      const left = ix.docs.filter((x) => x.id !== id);
      if (!left.length) {  // the last tab closed: a new empty graph
        const fresh = newDocId();
        writeDoc(fresh, { nodes: [], edges: [] });
        return { docs: [{ id: fresh, name: "Graph 1" }], active: fresh };
      }
      const at = ix.docs.findIndex((x) => x.id === id);
      return { docs: left, active: ix.active === id ? left[Math.min(at, left.length - 1)].id : ix.active };
    });
  };
  const rename = (id: string, name: string) =>
    setIndex((ix) => ({ ...ix, docs: ix.docs.map((d) => (d.id === id ? { ...d, name } : d)) }));
  const openNew = (name: string, g: GraphState, extra: { view?: string; fileHandle?: unknown; fileName: string; library?: unknown[]; autoGroup?: boolean }) => {
    const id = newDocId();
    opening.set(id, extra);
    writeDoc(id, g);
    setIndex((ix) => ({ docs: [...ix.docs, { id, name }], active: id }));
  };

  const doc = docs.find((d) => d.id === active) ?? docs[0];
  return (
    <div className="doc-shell">
      <DocTabs docs={docs} active={doc.id} clashes={clashes}
        dirty={Object.fromEntries(Object.entries(status).map(([k, v]) => [k, v.dirty]))}
        onSelect={select} onNew={() => add(nextName())} onClose={close} onRename={rename} />
      <div className="doc-body">
        <ReactFlowProvider key={doc.id}>
          <Editor catalog={catalog} abaqus={abaqus} doc={doc} onStatus={onStatus} onOpenNew={openNew} />
        </ReactFlowProvider>
      </div>
    </div>
  );
}
