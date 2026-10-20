import { useRef } from "react";
import type { Edge, Node } from "@xyflow/react";
import { useEditor } from "../context";
import { findPort, type NodeDef, type OpNodeData } from "../types";
import { applies, ParamField } from "./NodeCard";
import { DisplayBody } from "./Viewer";

/** Node types shown as results in the app without being asked: the outputs, and a job's state. */
const RESULT_TYPES = new Set(["model.job", "model.measure"]);

type OpNode = Node<OpNodeData>;

/** An Input's kind and caption are the graph author's choice, not the app user's: the label names the field. */
const FORM_HIDDEN = new Set(["type", "label"]);

const byPosition = (a: OpNode, b: OpNode) => a.position.y - b.position.y || a.position.x - b.position.x;
const label = (def: NodeDef, n: OpNode) => n.data.name || String(n.data.params.label || "") || def.title;
/** A node as the app lists it: its own name, else its type and the CAE name it makes (Sketch Bore). */
const longLabel = (def: NodeDef, n: OpNode) =>
  n.data.name || (n.data.params.name ? `${def.title} ${n.data.params.name}` : def.title);

/** What the app shows of a graph: its Input nodes as a form, its outputs (and nodes added by hand) as results.
 * `data.app` true adds a node to the results, false leaves it out of both. */
export function appParts(nodes: OpNode[], defs: Record<string, NodeDef>) {
  const ops = nodes.filter((n) => n.type === "op" && defs[n.data.type]).sort(byPosition);
  const inputs = ops.filter((n) => n.data.app !== false && defs[n.data.type].category === "Input");
  const results = ops.filter(
    (n) =>
      !inputs.includes(n) &&
      (n.data.app === true ||
        (n.data.app !== false && (defs[n.data.type].category === "Output" || RESULT_TYPES.has(n.data.type)))),
  );
  const others = ops.filter((n) => !inputs.includes(n) && !results.includes(n));
  return { inputs, results, others };
}

/** Settings that can become a field of the app: typed, wireable and not already fed by a wire. */
const EXPOSABLE = new Set(["number", "int", "text", "select"]);

export function AppView({ nodes, edges, title, running, onShow, onEdit, onExpose }: {
  nodes: OpNode[];
  edges: Edge[];
  title: string;
  running: boolean;
  /** Add a node to the app (true), or leave it out (false). */
  onShow: (id: string, show: boolean | undefined) => void;
  /** Back to the graph, on this node. */
  onEdit: (id: string) => void;
  /** Make a node's setting a field: an Input wired into it. */
  onExpose: (id: string, param: string) => void;
}) {
  const { defs, results, setParam, wiredParams } = useEditor();
  const root = useRef<HTMLDivElement>(null);
  const { inputs, results: shown, others } = appParts(nodes, defs);
  const hidden = others.filter((n) => n.data.app === false);
  const exposable = nodes
    .filter((n) => n.type === "op" && defs[n.data.type] && defs[n.data.type].category !== "Input")
    .sort(byPosition)
    .map((n) => {
      const def = defs[n.data.type];
      const params = def.params.filter(
        (p) =>
          EXPOSABLE.has(p.kind) &&
          findPort(def, `param:${p.name}`, "in") &&
          !wiredParams.has(`${n.id}|${p.name}`) &&
          applies(p.when, n.data, def, n.id, wiredParams),
      );
      return { n, def, params };
    })
    .filter((x) => x.params.length);
  const warnings = nodes.filter((n) => n.type === "op" && defs[n.data.type] && results[n.id]?.warnings?.length).sort(byPosition);
  const addable = others.filter((n) => n.data.app !== false && results[n.id]?.display);
  // An Input fed by a wire is computed, not typed: it isn't a field of the form.
  const fields = inputs
    .map((n) => {
      const def = defs[n.data.type];
      const params = def.params.filter(
        (p) => p.kind !== "action" && !FORM_HIDDEN.has(p.name) && !wiredParams.has(`${n.id}|${p.name}`) && applies(p.when, n.data, def, n.id, wiredParams),
      );
      // An Input wired into a choice (Kind, Shape…) offers that choice's options instead of free text.
      const choice = edges
        .filter((e) => e.source === n.id && e.targetHandle?.startsWith("param:"))
        .map((e) => {
          const t = nodes.find((x) => x.id === e.target);
          return t && defs[t.data.type]?.params.find((q) => q.name === e.targetHandle!.slice(6));
        })
        .find((q) => q?.kind === "select" && q.options?.length);
      return { n, def, params: choice ? params.map((p) => (p.name === "value" ? { ...p, kind: "select" as const, options: choice.options } : p)) : params };
    })
    .filter((f) => f.params.length && !edges.some((e) => e.target === f.n.id && !e.targetHandle?.startsWith("param:")));

  return (
    <div className="app-view">
      <div className="app-page" ref={root}>
        <div className="app-head">
          <h1>{title}</h1>
          <span className="no-report app-head-actions">
            {running && <span className="pill pill-run">Running…</span>}
            <button onClick={() => root.current && saveReport(root.current, title)}
              title="Save this page (inputs and results, with the plots as pictures) as one HTML file to share or print">
              Save report
            </button>
          </span>
        </div>
        <section className="app-inputs">
          <h2>Inputs</h2>
          {fields.length ? (
            fields.map(({ n, def, params }) => (
              <div className="app-field" key={n.id}>
                <span className="app-field-label" title={def.description}>
                  {label(def, n)}
                  <button className="icon-btn no-report" title="Leave this input out of the app" onClick={() => onShow(n.id, false)}>
                    ✕
                  </button>
                </span>
                {params.map((p) => (
                  <label key={p.name} title={p.help || undefined} className="app-param">
                    {params.length > 1 && <span className="muted">{p.label}</span>}
                    <ParamField
                      nodeId={n.id}
                      nodeType={n.data.type}
                      def={p}
                      value={n.data.params[p.name] ?? p.default}
                      options={results[n.id]?.hints?.[p.name]}
                      onChange={(v) => setParam(n.id, p.name, v)}
                      params={n.data.params}
                    />
                  </label>
                ))}
              </div>
            ))
          ) : (
            <p className="muted no-report">
              No inputs yet: add a setting below, or wire Input nodes into the settings people should change (the
              Input's name is the field's label).
            </p>
          )}
          {exposable.length > 0 && (
            <select className="no-report app-expose" value=""
              onChange={(e) => {
                const [id, param] = e.target.value.split("|");
                if (id) onExpose(id, param);
              }}
              title="Make a node's setting a field of the app: an Input node is wired into it, with the current value">
              <option value="">＋ Add a setting as input…</option>
              {exposable.map(({ n, def, params }) => (
                <optgroup key={n.id} label={longLabel(def, n)}>
                  {params.map((p) => (
                    <option key={p.name} value={`${n.id}|${p.name}`}>{p.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          )}
        </section>
        {warnings.length > 0 && (
          <section className="app-checks">
            <h2>Checks</h2>
            <ul className="viewer-warnings">
              {warnings.flatMap((n) =>
                results[n.id].warnings!.map((w, i) => (
                  <li key={`${n.id}-${i}`}>
                    ⚠ <b>{longLabel(defs[n.data.type], n)}</b>: {w}
                  </li>
                )),
              )}
            </ul>
          </section>
        )}
        <section className="app-results">
          <h2>Results</h2>
          {shown.length ? (
            shown.map((n) => {
              const def = defs[n.data.type];
              const r = results[n.id];
              const actions = def.params.filter((p) => p.kind === "action");
              return (
                <div className="app-card" key={n.id}>
                  <div className="app-card-head">
                    <b>{longLabel(def, n)}</b>
                    <span className="no-report">
                      <button className="icon-btn" title="Show this node in the graph" onClick={() => onEdit(n.id)}>
                        ⤴
                      </button>
                      <button className="icon-btn" title="Leave this out of the app" onClick={() => onShow(n.id, false)}>
                        ✕
                      </button>
                    </span>
                  </div>
                  {actions.map((p) => (
                    <div className="no-report" key={p.name}>
                      <ParamField nodeId={n.id} nodeType={n.data.type} def={p} value="" options={undefined} onChange={() => {}} />
                    </div>
                  ))}
                  {r?.warnings?.map((w, i) => (
                    <div className="node-warn" key={i}>⚠ {w}</div>
                  ))}
                  <div className="app-card-body">
                    {!r ? (
                      <div className="empty">Running…</div>
                    ) : r.status !== "ok" ? (
                      <div className={`empty msg-${r.status}`}>{r.message}</div>
                    ) : r.display ? (
                      <DisplayBody d={r.display} />
                    ) : (
                      <div className="empty">{r.summary || "No preview."}</div>
                    )}
                  </div>
                </div>
              );
            })
          ) : (
            <p className="muted no-report">No results yet: add Value, Table, Chart or Contour Plot nodes, or add a node below.</p>
          )}
        </section>
        <div className="app-more no-report">
          {addable.length > 0 && (
            <select value="" onChange={(e) => e.target.value && onShow(e.target.value, true)}
              title="Show another node's result in the app">
              <option value="">＋ Show a node's result…</option>
              {addable.map((n) => (
                <option key={n.id} value={n.id}>{longLabel(defs[n.data.type], n)}</option>
              ))}
            </select>
          )}
          {hidden.length > 0 && (
            <span className="muted">
              Left out:{" "}
              {hidden.map((n) => (
                <button key={n.id} className="link-btn" title="Show it in the app again" onClick={() => onShow(n.id, undefined)}>
                  {label(defs[n.data.type], n)}
                </button>
              ))}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

const escape = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

/** The page as one HTML file: the app's own styles, the plots as pictures, the fields as their values. */
export function saveReport(page: HTMLElement, title: string) {
  const copy = page.cloneNode(true) as HTMLElement;
  // 3D views draw on a canvas: keep what they show now.
  const canvases = page.querySelectorAll("canvas");
  copy.querySelectorAll("canvas").forEach((c, i) => {
    const img = document.createElement("img");
    try {
      img.src = canvases[i].toDataURL("image/png");
    } catch {
      return;
    }
    img.style.width = `${canvases[i].clientWidth}px`;
    img.style.height = `${canvases[i].clientHeight}px`;
    c.replaceWith(img);
  });
  // Fields become their current values.
  const fields = page.querySelectorAll("input, select, textarea");
  copy.querySelectorAll("input, select, textarea").forEach((f, i) => {
    const src = fields[i] as HTMLInputElement | HTMLSelectElement;
    const text = src instanceof HTMLSelectElement ? src.selectedOptions[0]?.text ?? "" : src.value;
    const span = document.createElement("span");
    span.className = "report-value";
    span.textContent = text;
    f.replaceWith(span);
  });
  copy.querySelectorAll(".no-report, button").forEach((e) => e.remove());
  const css = Array.from(document.styleSheets)
    .map((s) => {
      try {
        return Array.from(s.cssRules).map((r) => r.cssText).join("\n");
      } catch {
        return "";
      }
    })
    .join("\n");
  const when = new Date().toLocaleString();
  const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escape(title)}</title><style>${css}
html, body { height: auto; overflow: auto; }
.app-view { position: static; overflow: visible; }
</style></head><body><div class="app-view">${copy.outerHTML}<p class="muted report-date">NodeCode FEA report · ${escape(when)}</p></div></body></html>`;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  a.download = `${title.replace(/[^\w.-]+/g, "_") || "report"}.html`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
