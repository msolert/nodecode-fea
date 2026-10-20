import { Handle, Position, useConnection, useReactFlow, useStore, type Node, type NodeProps } from "@xyflow/react";
import { memo, useEffect, useState } from "react";
import { useEditor } from "../context";
import { caeObjects, caeSets, type CaeObjectKind, type CaeSet } from "../api";
import { FormulaInput } from "./FormulaInput";
import { StepTable } from "./StepTable";
import { outputNames } from "./ScriptEditor";
import { asSetting } from "../stepwise";
import {
  CATEGORY_COLORS,
  PORT_COLORS,
  PORT_TYPE_NAMES,
  type NodeDef,
  type OpNodeData,
  type Option,
  type ParamDef,
  type ParamValue,
  type PortDef,
  type PortType,
  findPort,
  typesFit,
} from "../types";

function TextInput({ value, onCommit, placeholder }: { value: string; onCommit: (v: string) => void; placeholder?: string }) {
  // Keep typing local so every keystroke doesn't trigger a graph run.
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <input
      className="nodrag"
      value={draft}
      placeholder={placeholder}
      spellCheck={false}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft !== value && onCommit(draft)}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

/** A sketch, part, instance, material or section of the running CAE, chosen from a list of every model's ("model|name", an instance
 * "model|name|part"), into the setting `param`. With `none`, an empty choice (labelled so) stays in the list. */
function CaeObjectSelect({ nodeId, kind, value: raw, param = "value", none }: {
  nodeId: string;
  kind: CaeObjectKind;
  value: string;
  param?: string;
  none?: string;
}) {
  const value = raw.includes("|") ? raw : ""; // e.g. the number typed before switching the Input's type
  const { caeConnected, setParam } = useEditor();
  const [items, setItems] = useState<string[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    setError("");
    try {
      setItems((await caeObjects(kind)).items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  const list = items ?? [];
  const options = value && !list.includes(value) ? [value, ...list] : list;
  const label = (v: string) => {
    const [model, name, part] = v.split("|");
    return `${model} › ${name}${part ? ` (${part})` : ""}`;
  };
  return (
    <div className="file-field" title={error || `The ${kind}s of every model in CAE`}>
      <select
        className="nodrag"
        value={value}
        disabled={!caeConnected && !(none !== undefined && value)}
        onMouseDown={() => items === null && !loading && load()}
        onFocus={() => items === null && !loading && load()}
        onChange={(e) => setParam(nodeId, param, e.target.value)}
      >
        {none !== undefined ? (
          <option value="">{caeConnected || value ? none : `${none} (launch CAE to list its ${kind}s)`}</option>
        ) : (
          !value && <option value="">{caeConnected ? `Choose a ${kind}…` : "Launch CAE first"}</option>
        )}
        {options.map((v) => (
          <option key={v} value={v}>
            {label(v)}
            {items && !list.includes(v) ? " (not in CAE)" : ""}
          </option>
        ))}
        {loading && <option disabled>Asking CAE…</option>}
        {error && <option disabled>{error}</option>}
      </select>
      <button className="nodrag icon-btn" title={`List CAE's ${kind}s again`} disabled={!caeConnected} onClick={load}>
        ⟳
      </button>
    </div>
  );
}

/** Fill a Region from a Set or Surface defined in CAE: its geometry as Find-at points (found again when the part is
 * rebuilt), its kind, and its name and form, so NodeCode FEA makes the same named Set/Surface. A one-off copy: later edits
 * in CAE don't come back. */
function CaeSetSelect({ nodeId }: { nodeId: string }) {
  const { caeConnected, setParam } = useEditor();
  const [items, setItems] = useState<CaeSet[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const load = async () => {
    setLoading(true);
    setError("");
    try {
      setItems((await caeSets()).items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };
  const plural = { face: "faces", edge: "edges", vertex: "vertices", cell: "cells" };
  const label = (s: CaeSet) =>
    `${s.model} › ${s.part || `Assembly (${s.instance})`} › ${s.form === "Surface" ? "surface " : ""}${s.name}: ` +
    `${s.count} ${s.count === 1 ? s.kind : plural[s.kind]}`;
  const take = (s: CaeSet) => {
    setParam(nodeId, "kind", s.kind.charAt(0).toUpperCase() + s.kind.slice(1));
    setParam(nodeId, "value", s.value);
    setParam(nodeId, "create", s.form);
    setParam(nodeId, "name", s.name);
  };
  return (
    <div
      className="file-field"
      title={error || "Take over a Set or Surface made in CAE (wire its Part, or Instance for an assembly set)"}
    >
      <select
        className="nodrag"
        value=""
        disabled={!caeConnected}
        onMouseDown={() => items === null && !loading && load()}
        onFocus={() => items === null && !loading && load()}
        onChange={(e) => items && e.target.value !== "" && take(items[Number(e.target.value)])}
      >
        <option value="">{caeConnected ? "From a CAE set…" : "Launch CAE first"}</option>
        {(items ?? []).map((s, i) => (
          <option key={i} value={i}>
            {label(s)}
          </option>
        ))}
        {items?.length === 0 && <option disabled>No geometry sets or surfaces in CAE</option>}
        {loading && <option disabled>Asking CAE…</option>}
        {error && <option disabled>{error}</option>}
      </select>
      <button className="nodrag icon-btn" title="List CAE's sets again" disabled={!caeConnected} onClick={load}>
        ⟳
      </button>
    </div>
  );
}

export function ParamField({ nodeId, nodeType, def, value, options, onChange, params }: {
  nodeId: string;
  nodeType: string;
  def: ParamDef;
  value: ParamValue;
  options: Option[] | undefined;
  onChange: (v: ParamValue) => void;
  params?: Record<string, ParamValue>;
}) {
  const { browse, caeConnected, pickInCae, picking, nodeAction } = useEditor();
  if (def.kind === "action") {
    return (
      <div className="action-btns">
        {(def.options ?? []).map((a) => (
          <button key={a} className="nodrag" title={def.help || a} onClick={() => nodeAction(nodeId, a)}>
            {a}
          </button>
        ))}
      </div>
    );
  }
  if (def.kind === "pick" && nodeType === "input.value" && String(params?.type).endsWith(" in CAE")) {
    const kind = String(params?.type).split(" ")[0].toLowerCase() as CaeObjectKind;
    return <CaeObjectSelect nodeId={nodeId} kind={kind} value={String(params?.value ?? "")} />;
  }
  if (def.kind === "pick" && nodeType === "model.section") {
    return <CaeObjectSelect nodeId={nodeId} kind="material" value={String(value ?? "")} param={def.name} none="New material (settings below)" />;
  }
  if (def.kind === "pick") {
    const busy = picking === nodeId;
    const button = (
      <button
        className="nodrag pick-btn"
        disabled={!caeConnected || busy}
        title={caeConnected ? "Click the object in the CAE viewport, then click Done there" : "Launch CAE from the top bar first"}
        onClick={() => pickInCae(nodeId)}
      >
        {busy ? "Picking in CAE…" : "Pick in CAE"}
      </button>
    );
    return nodeType === "model.region" ? (
      <div className="pick-or-set">
        {button}
        <CaeSetSelect nodeId={nodeId} />
      </div>
    ) : button;
  }
  const opts: Option[] | undefined = options?.length
    ? options
    : def.options?.map((o) => ({ value: o, label: o }));

  if (def.kind === "select" && opts) {
    const list = opts.some((o) => o.value === value) ? opts : [{ value, label: `${value} (?)` }, ...opts];
    const index = list.findIndex((o) => o.value === value);
    return (
      <select className="nodrag" value={index} onChange={(e) => onChange(list[Number(e.target.value)].value)}>
        {list.map((o, i) => (
          <option key={i} value={i}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }
  if ((def.kind === "number" || def.kind === "int") && def.per_step) {
    // a number, or a value per step: 0; Horizon: -9810
    return (
      <TextInput
        value={String(value)}
        placeholder="value; Step: value"
        onCommit={(v) => onChange(v.trim() === "" ? def.default : asSetting(v, true))}
      />
    );
  }
  if (def.kind === "number" || def.kind === "int") {
    return (
      <TextInput
        value={String(value)}
        onCommit={(v) => {
          const n = def.kind === "int" ? parseInt(v, 10) : parseFloat(v);
          onChange(Number.isFinite(n) ? n : def.default);
        }}
      />
    );
  }
  if (def.kind === "file") {
    return (
      <div className="file-field">
        <TextInput value={String(value)} onCommit={onChange} />
        <button
          className="nodrag icon-btn"
          title={`Browse for ${(def.options?.length ? def.options : [".odb"]).join(", ")} files`}
          onClick={() => browse(nodeId, def.name, String(value), def.options?.map(String))}
        >
          …
        </button>
      </div>
    );
  }
  if (opts?.length) {
    // Free text plus a pick list, e.g. labels typed by hand or chosen from a connected set.
    const current = String(value).split(/[,\s]+/).filter(Boolean);
    return (
      <div className="file-field">
        <TextInput value={String(value)} onCommit={onChange} placeholder="all" />
        <select
          className="nodrag pick-add"
          value=""
          title="Add from the list"
          onChange={(e) => {
            const v = e.target.value;
            if (v === "*") onChange("");
            else if (v && !current.includes(v)) onChange([...current, v].join(", "));
          }}
        >
          <option value="">＋</option>
          <option value="*">(all)</option>
          {opts.map((o, i) => (
            <option key={i} value={String(o.value)} disabled={current.includes(String(o.value))}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
    );
  }
  if (nodeType === "math.formula" && def.name === "expr") {
    return <FormulaInput value={String(value)} onCommit={onChange} placeholder="e.g. np.max(a)" nodeId={nodeId} />;
  }
  return <TextInput value={String(value)} onCommit={onChange} placeholder={def.help ? "see ?" : undefined} />;
}

function HelpPanel({ def }: { def: NodeDef }) {
  const port = (p: PortDef) => (
    <li key={p.name}>
      <span className="help-dot" style={{ background: PORT_COLORS[p.type] }} />
      <b>{p.label}</b> <span className="type-chip">{PORT_TYPE_NAMES[p.type]}</span>
      {p.optional && <span className="type-chip ghost">optional</span>}
      {p.multi && <span className="type-chip ghost">several</span>}
      {p.help && <div className="help-text">{p.help}</div>}
    </li>
  );
  return (
    <div className="help-panel nodrag nowheel" onClick={(e) => e.stopPropagation()}>
      <p>{def.description}</p>
      {def.inputs.length > 0 && (
        <>
          <h4>Needs (left side)</h4>
          <ul>{def.inputs.map(port)}</ul>
        </>
      )}
      {def.params.some((p) => p.help) && (
        <>
          <h4>Settings</h4>
          <ul>
            {def.params.filter((p) => p.help).map((p) => (
              <li key={p.name}>
                <b>{p.label}</b>
                <div className="help-text">{p.help}</div>
              </li>
            ))}
          </ul>
        </>
      )}
      {def.outputs.length > 0 && (
        <>
          <h4>Gives (right side)</h4>
          <ul>{def.outputs.map(port)}</ul>
        </>
      )}
    </div>
  );
}

/** While a wire is being dragged: which port it starts from, as "nodeId|handleId|source|target". */
const connectionKey = (c: { inProgress: boolean; fromHandle?: { nodeId: string; id?: string | null; type: string } | null }) =>
  c.inProgress && c.fromHandle ? `${c.fromHandle.nodeId}|${c.fromHandle.id ?? ""}|${c.fromHandle.type}` : "";

/** A setting or input that only applies for some choices (Radius for a Circle) is hidden otherwise, unless it's
 * wired, or a setting it depends on comes from a wire (its value is then unknown here). */
export function applies(when: Record<string, string[]> | null | undefined, data: OpNodeData, def: NodeDef, nodeId: string,
                 wired: Set<string>): boolean {
  if (!when) return true;
  return Object.entries(when).every(([k, values]) => {
    // "unwired:<port>": only while that input port has no wire (a typed fallback for it)
    if (k.startsWith("unwired:")) return !wired.has(`${nodeId}|>${k.slice(8)}`);
    // "wired:<port>": only once that input port has a wire (Align by, once To is wired)
    if (k.startsWith("wired:")) return wired.has(`${nodeId}|>${k.slice(6)}`);
    // "named:<setting>": while that comma-separated setting names an item at one of these positions (the
    // Formula's More outputs: one port per variable named)
    if (k.startsWith("named:")) return values.some((i) => Number(i) < outputNames(data.params[k.slice(6)]).length);
    if (wired.has(`${nodeId}|${k}`)) return true;
    const v = data.params[k] ?? def.params.find((p) => p.name === k)?.default;
    return values.includes(String(v));
  });
}

const MIN_WIDTH = 210;
const MAX_WIDTH = 900;
const DEFAULT_MAX = 280; // .node's max-width: wider than this, long labels and texts wrap instead of being cut

function NodeCardImpl({ id, data, selected }: NodeProps<Node<OpNodeData>>) {
  const { defs, results, setParam, wiredParams, openPortMenu, setName, renaming, setRenaming, toggleDisabled, cycleSize, setWidth } =
    useEditor();
  const flow = useReactFlow();
  // Drag the right edge to widen the node; the width is saved when the drag ends.
  const startResize = (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    e.preventDefault();
    const card = e.currentTarget.parentElement as HTMLElement;
    const zoom = flow.getZoom();
    const x0 = e.clientX;
    const w0 = card.getBoundingClientRect().width / zoom;
    let w = w0;
    const move = (ev: PointerEvent) => {
      w = Math.round(Math.min(Math.max(w0 + (ev.clientX - x0) / zoom, MIN_WIDTH), MAX_WIDTH));
      card.style.width = `${w}px`;
      card.style.maxWidth = "none";
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (w !== w0) setWidth(id, w);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  // A click (not a drag) on a port lists the matching ports already in the graph.
  const portClick = (handle: string, side: "in" | "out") => (e: React.MouseEvent) => {
    e.stopPropagation();
    openPortMenu(id, handle, side, e.clientX, e.clientY);
  };
  const [help, setHelp] = useState(false);
  const dragging = useConnection(connectionKey);
  const [fromNode, fromHandle, fromSide] = dragging ? dragging.split("|") : [];
  const fromNodeType = useStore((s) => (fromNode ? (s.nodeLookup.get(fromNode)?.data as OpNodeData | undefined)?.type : undefined));

  const def = defs[data.type];
  const result = results[id];
  if (!def) {
    return <div className="node node-error">Unknown node: {data.type}</div>;
  }

  // Work out which of this node's ports can accept the wire being dragged.
  let fromType: string | undefined;
  if (dragging && fromNodeType && defs[fromNodeType]) {
    fromType = findPort(defs[fromNodeType], fromHandle, fromSide === "source" ? "out" : "in")?.type;
  }
  const fits = (p: PortDef, side: "in" | "out") =>
    !!fromType &&
    fromNode !== id &&
    typesFit(p.type, fromType as PortType) &&
    (side === "in" ? fromSide === "source" : fromSide === "target");
  const portClass = (p: PortDef, side: "in" | "out") =>
    !dragging ? "" : fits(p, side) ? "port-fits" : fromNode === id && fromHandle === p.name ? "" : "port-dim";
  const anyFit =
    def.inputs.some((p) => fits(p, "in")) ||
    def.outputs.some((p) => fits(p, "out")) ||
    def.params.some((p) => {
      const port = findPort(def, `param:${p.name}`, "in");
      return !!port && fits(port, "in");
    });

  const status = result?.status ?? "pending";
  // Inputs that don't apply to the current choices are hidden (unless wired).
  const inputs = def.inputs.filter((p) => wiredParams.has(`${id}|>${p.name}`) || applies(p.when, data, def, id, wiredParams));
  // Outputs that only exist for some choices (Part Feature's Reference point) likewise, unless wired.
  const wiredOut = useStore((s) => s.edges.filter((e) => e.source === id).map((e) => e.sourceHandle).join("|"));
  const outputs = def.outputs.filter((p) => wiredOut.split("|").includes(p.name) || applies(p.when, data, def, id, wiredParams));
  const rows = Math.max(inputs.length, outputs.length);
  // settings that take a value per step, as they apply now: the Per-step table's columns
  const stepParams = def.params.filter((p) => p.per_step && (wiredParams.has(`${id}|${p.name}`) || applies(p.when, data, def, id, wiredParams)));
  // A port named by a setting (a Formula's More outputs) shows that name.
  const label = (p: PortDef) => {
    const key = Object.keys(p.when ?? {}).find((k) => k.startsWith("named:"));
    return (key && outputNames(data.params[key.slice(6)])[Number(p.when![key][0])]) || p.label;
  };
  const tip = (p: PortDef) => `${label(p)} — ${PORT_TYPE_NAMES[p.type]}${p.optional ? " (optional)" : ""}${p.help ? `\n${p.help}` : ""}`;

  return (
    <div
      className={`node status-${status} ${data.disabled ? "node-off" : ""} ${selected ? "selected" : ""} ${dragging && !anyFit && fromNode !== id ? "node-dim" : ""} ${(data.width ?? 0) > DEFAULT_MAX ? "node-wide" : ""}`}
      style={data.width ? { width: data.width, maxWidth: "none" } : undefined}
    >
      <div
        className="node-resize nodrag"
        title="Drag to widen the node (double-click: default width)"
        onPointerDown={startResize}
        onDoubleClick={(e) => (e.stopPropagation(), setWidth(id, undefined))}
      />
      <div className="node-head" style={{ background: CATEGORY_COLORS[def.category] ?? "#555" }}>
        {renaming === id ? (
          <input
            className="nodrag rename-input"
            autoFocus
            defaultValue={data.name ?? ""}
            placeholder={def.title}
            onFocus={(e) => e.target.select()}
            onBlur={(e) => (setName(id, e.target.value), setRenaming(null))}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              if (e.key === "Escape") setRenaming(null);
            }}
          />
        ) : (
          <span className="node-title" title="Double-click to name this node" onDoubleClick={() => setRenaming(id)}>
            {data.name ? (
              <>
                {data.name} <small className="node-kind">{def.title}</small>
              </>
            ) : (
              def.title
            )}
          </span>
        )}
        <span className="head-right">
          {data.levels === "whole" && (
            <span className="combine-badge" title="Takes its inputs whole: a grid is one list of rows, not one run per row. Right-click the node to run once per row instead.">
              ▤
            </span>
          )}
          {data.combine === "all" && (
            <span className="combine-badge" title="Lists in: every combination (a grid). Right-click the node to pair them up instead.">
              ⊞
            </span>
          )}
          <button
            className="nodrag help-btn"
            title={data.size === "min" ? "Show the whole node (H)" : data.size === "compact" ? "Header only (H)" : "Shrink: hide the settings (H)"}
            onClick={(e) => {
              e.stopPropagation();
              cycleSize(id);
            }}
          >
            {data.size === "min" ? "▢" : data.size === "compact" ? "▬" : "▭"}
          </button>
          <button
            className={`nodrag help-btn power-btn ${data.disabled ? "off" : ""}`}
            title={data.disabled ? "Activate this node (M)" : "Deactivate this node (M): it passes a matching input through, or drops out"}
            onClick={(e) => {
              e.stopPropagation();
              toggleDisabled(id);
            }}
          >
            ⏻
          </button>
          <button
            className={`nodrag help-btn ${help ? "on" : ""}`}
            title="What does this node need?"
            onClick={(e) => {
              e.stopPropagation();
              setHelp((h) => !h);
            }}
          >
            ?
          </button>
          <span className={`dot dot-${status}`} />
        </span>
      </div>
      {help && <HelpPanel def={def} />}
      {data.size === "min" && (
        // Header only: every port stays as a dot on the header's edge, so wires stay attached.
        <>
          {[...def.inputs.map((p) => p.name), ...def.params.filter((p) => findPort(def, `param:${p.name}`, "in")).map((p) => `param:${p.name}`)].map((h) => (
            <Handle key={h} type="target" position={Position.Left} id={h} className="min-handle" onClick={portClick(h, "in")} />
          ))}
          {def.outputs.map((p) => (
            <Handle key={p.name} type="source" position={Position.Right} id={p.name} className="min-handle"
              onClick={portClick(p.name, "out")} style={{ background: PORT_COLORS[p.type] }} />
          ))}
        </>
      )}
      {data.size !== "min" && rows > 0 && (
        <div className="node-ports">
          {Array.from({ length: rows }, (_, i) => {
            const input = inputs[i];
            const output = outputs[i];
            return (
              <div className="port-row" key={i}>
                {input ? (
                  <div className={`port port-in ${portClass(input, "in")}`} title={tip(input)}>
                    <Handle
                      type="target"
                      position={Position.Left}
                      id={input.name}
                      onClick={portClick(input.name, "in")}
                      style={{ background: PORT_COLORS[input.type], color: PORT_COLORS[input.type] }}
                    />
                    <span>
                      {input.label}
                      {input.optional && <em> opt</em>}
                      {input.multi && <em> ×n</em>}
                    </span>
                  </div>
                ) : (
                  <div />
                )}
                {output ? (
                  <div className={`port port-out ${portClass(output, "out")}`} title={tip(output)}>
                    <span>{label(output)}</span>
                    <Handle
                      type="source"
                      position={Position.Right}
                      id={output.name}
                      onClick={portClick(output.name, "out")}
                      style={{ background: PORT_COLORS[output.type], color: PORT_COLORS[output.type] }}
                    />
                  </div>
                ) : (
                  <div />
                )}
              </div>
            );
          })}
        </div>
      )}
      {data.size !== "min" && def.params.length > 0 && (
        <div className="node-params">
          {def.params.map((p) => {
            const port = findPort(def, `param:${p.name}`, "in");
            const wired = wiredParams.has(`${id}|${p.name}`);
            if (data.size === "compact" && !wired) return null;  // shrunk: only settings fed by a wire stay
            if (!wired && !applies(p.when, data, def, id, wiredParams)) return null;  // not for this choice
            if (p.table_only && !wired) return null;  // its checkboxes are in the Per-step table
            // A row of buttons isn't a <label>: a click beside them would press the first one.
            const Row = p.kind === "action" ? "div" : "label";
            return (
              <Row
                key={p.name}
                title={p.help || undefined}
                className={`${p.kind === "pick" || p.kind === "action" ? "wide" : ""} ${port ? `param-row ${portClass(port, "in")}` : ""}`}
              >
                {port && (
                  <Handle
                    type="target"
                    position={Position.Left}
                    id={port.name}
                    className="param-handle"
                    onClick={portClick(port.name, "in")}
                    title={`Feed '${p.label}' from a wire (${PORT_TYPE_NAMES[port.type]})`}
                    style={{ background: PORT_COLORS[port.type], color: PORT_COLORS[port.type] }}
                  />
                )}
                {p.kind !== "pick" && p.kind !== "action" && <span>{p.label}</span>}
                {wired ? (
                  <span className="wired-param" title="This setting comes from the wire; remove the wire to type a value">
                    ⟵ from wire
                  </span>
                ) : (
                  <ParamField
                    nodeId={id}
                    nodeType={data.type}
                    def={p}
                    value={data.params[p.name] ?? p.default}
                    options={result?.hints?.[p.name]}
                    onChange={(v) => setParam(id, p.name, v)}
                    params={data.params}
                  />
                )}
              </Row>
            );
          })}
        </div>
      )}
      {data.size !== "min" && data.size !== "compact" && stepParams.length > 0 && (
        <StepTable id={id} data={data} result={result} params={stepParams} />
      )}
      {result?.warnings?.length ? (
        <div className="node-warn" title={result.warnings.join("\n\n")}>
          ⚠ {result.warnings[0]}
          {result.warnings.length > 1 && <span className="muted"> (+{result.warnings.length - 1} more)</span>}
        </div>
      ) : null}
      {data.size !== "min" && result && (result.message || result.summary) && (
        <div
          className={`node-foot foot-${status} ${result.message ? "" : "foot-summary"}`}
          title={result.message ? undefined : result.summary}
        >
          {result.message || result.summary}
        </div>
      )}
    </div>
  );
}

export const NodeCard = memo(NodeCardImpl);
