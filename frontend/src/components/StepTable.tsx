import { useEffect, useRef, useState } from "react";
import { useEditor } from "../context";
import { asSetting, formatActive, join, split } from "../stepwise";
import type { NodeResult, OpNodeData, ParamDef } from "../types";

function Cell({ value, placeholder, disabled, onCommit }: {
  value: string; placeholder: string; disabled: boolean; onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <input
      className="nodrag"
      value={draft}
      placeholder={placeholder}
      disabled={disabled}
      spellCheck={false}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft !== value && onCommit(draft)}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

const TIP = "Values and activity per analysis step, one row per step of the model. Active: from its Step onward " +
  "unless you untick steps (off there; on again later makes a fresh copy). An empty cell keeps the value from the " +
  "step before (greyed). The Per step input takes the same table from another node (Step, Active, setting columns).";

/** One row per analysis step of the model, one column per per-step setting (and Active): edits each setting's
 * "value; Step: value" text and Active in. The first active row holds the value where it is created; a cell left
 * empty keeps the step before's value (shown greyed). */
export function StepTable({ id, data, params, result }: {
  id: string; data: OpNodeData; params: ParamDef[]; result: NodeResult | undefined;
}) {
  const { setParam, wiredParams } = useEditor();
  // the last steps it knew stay while it has an error that lists none, so a tick can be put back
  const known = useRef<{ steps: string[]; active: string[] }>({ steps: [], active: [] });
  if (result?.hints?.__steps?.length) {
    known.current = { steps: result.hints.__steps.map((o) => String(o.value)),
                      active: (result.hints.__active ?? []).map((o) => String(o.value)) };
  }
  const steps = known.current.steps;
  if (!steps.length) return null;
  const active = new Set(known.current.active);
  const activeDef = params.find((p) => p.name === "active");
  const columns = params.filter((p) => p.name !== "active");
  const first = steps.find((s) => active.has(s)) ?? steps[0];
  const value = (p: ParamDef) => data.params[p.name] ?? p.default;
  const numeric = (p: ParamDef) => p.kind === "number" || p.kind === "int";

  const edit = (p: ParamDef, step: string, text: string) => {
    const sv = split(value(p));
    if (step === first) sv.base = text.trim();
    else {
      const changes = sv.changes.filter(([s]) => s.toLowerCase() !== step.toLowerCase());
      if (text.trim()) changes.push([step, text.trim()]);
      const order = (s: string) => {
        const i = steps.findIndex((x) => x.toLowerCase() === s.toLowerCase());
        return i < 0 ? steps.length : i;
      };
      sv.changes = changes.sort((a, b) => order(a[0]) - order(b[0]));
    }
    setParam(id, p.name, asSetting(join(sv), numeric(p)));
  };
  const toggle = (step: string) => {
    const next = new Set(active);
    if (next.has(step)) next.delete(step);
    else next.add(step);
    if (next.size) setParam(id, "active", formatActive(next, steps));
  };

  // fed by a wire (a table from another node): shown as the node read it, not editable here
  const fed = wiredParams.has(`${id}|>steps`);
  const cells = (result?.hints?.__cells?.[0]?.value ?? {}) as unknown as Record<string, Record<string, string>>;

  return (
    <div className="step-table nodrag nowheel">
      <div className="step-table-head">
        <span className="muted" title={TIP}>Per step{fed ? " (from the wire)" : ""}</span>
      </div>
      <div className="step-table-scroll">
        <table>
          <thead>
            <tr>
              <th>Step</th>
              {activeDef && <th title={activeDef.help}>Active</th>}
              {columns.map((p) => <th key={p.name} title={p.help || undefined}>{p.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {steps.map((s) => {
              const on = active.has(s);
              return (
                <tr key={s} className={on ? "" : "step-off"}>
                  <td className="step-name">{s}</td>
                  {activeDef && (
                    <td>
                      <input type="checkbox" className="nodrag" checked={on}
                        disabled={fed || wiredParams.has(`${id}|active`) || (on && active.size === 1)}
                        onChange={() => toggle(s)} />
                    </td>
                  )}
                  {columns.map((p) => {
                    if (fed) {
                      return <td key={p.name}><Cell value={cells[p.name]?.[s] ?? ""} placeholder="" disabled onCommit={() => {}} /></td>;
                    }
                    const wired = wiredParams.has(`${id}|${p.name}`);
                    const sv = split(value(p));
                    const own = s === first ? sv.base : (sv.changes.find(([x]) => x.toLowerCase() === s.toLowerCase())?.[1] ?? "");
                    // what holds here when the cell is empty: the latest value before this step
                    let held = sv.base || String(p.default);
                    for (const x of steps.slice(0, steps.indexOf(s))) {
                      const c = sv.changes.find(([y]) => y.toLowerCase() === x.toLowerCase());
                      if (c) held = c[1] || "default";
                    }
                    return (
                      <td key={p.name}>
                        <Cell value={own} placeholder={s === first ? String(p.default) : held}
                          disabled={wired || !on} onCommit={(v) => edit(p, s, v)} />
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}


