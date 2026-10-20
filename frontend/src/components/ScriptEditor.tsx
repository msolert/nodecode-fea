import { useReactFlow, useStore } from "@xyflow/react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useEditor } from "../context";
import { realEnd } from "../groups";
import type { OpNodeData } from "../types";
import { type FormulaName, loadNames, matches, wordBefore } from "./FormulaInput";

const LINE = 20; // px per line: the gutter and the suggestion list line up with the text by it
const INDENT = "    ";
const INPUTS = ["a", "b", "c", "d"];

/** "peak, note" → ["peak", "note"]: a Formula's More outputs. */
export const outputNames = (v: unknown) => String(v ?? "").split(/[,;\s]+/).filter(Boolean);

/** The line a node's error points at ("Line 3: …"), if any. */
const errorLine = (message: string | undefined) => {
  const m = /\b[Ll]ine (\d+)\b/.exec(message ?? "");
  return m ? Number(m[1]) : 0;
};

/** The Formula's code window: a script of several lines with line numbers, autocomplete, what each input is,
 * the More outputs and the node's result or error, side by side. */
export function ScriptEditor({ nodeId, value, onCommit, onClose }: {
  nodeId: string;
  value: string;
  onCommit: (v: string) => void;
  onClose: () => void;
}) {
  const { defs, results, setParam } = useEditor();
  const flow = useReactFlow();
  const [draft, setDraft] = useState(value);
  const [list, setList] = useState<FormulaName[]>([]);
  const [active, setActive] = useState(0);
  const [chosen, setChosen] = useState(false); // a suggestion picked with the arrows
  const [caret, setCaret] = useState({ line: 0, col: 0 });
  const [scroll, setScroll] = useState(0);
  const [charW, setCharW] = useState(8);
  const area = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const measure = useRef<HTMLSpanElement>(null);

  const node = useStore((s) => s.nodeLookup.get(nodeId));
  const data = node?.data as OpNodeData | undefined;
  const edges = useStore((s) => s.edges.filter((e) => e.target === nodeId));
  const result = results[nodeId];
  const committed = draft === value;
  const badLine = committed && result?.status === "error" ? errorLine(result.message) : 0;

  useEffect(() => void loadNames(), []);
  useLayoutEffect(() => {
    if (measure.current) setCharW(measure.current.getBoundingClientRect().width / 10);
    area.current?.focus();
  }, []);

  const where = (text: string, at: number) => {
    const before = text.slice(0, at).split("\n");
    setCaret({ line: before.length - 1, col: before[before.length - 1].length });
  };
  const suggest = (text: string, at: number) => {
    where(text, at);
    loadNames().then((all) => {
      // Variables the script sets are offered too.
      const vars = [...new Set([...text.matchAll(/^\s*([A-Za-z_]\w*)\s*=(?!=)/gm)].map((m) => m[1]))]
        .filter((n) => !INPUTS.includes(n))
        .map((n): FormulaName => ({ name: n, kind: "input", doc: "a variable of this script" }));
      setList(matches([...vars, ...all], wordBefore(text, at).word));
      setActive(0);
      setChosen(false);
    });
  };

  // The handlers work on the textarea's own text and caret, which keystrokes faster than React renders keep current.
  /** Put ``next`` in the textarea with the selection from ``a`` to ``b``. */
  const put = (next: string, a: number, b = a) => {
    const el = area.current!;
    el.value = next;
    el.setSelectionRange(a, b);
    setDraft(next);
    where(next, b);
  };
  /** Replace the selection (or from..to) with ``text``, keeping the caret after it. */
  const insert = (text: string, from?: number, to?: number) => {
    const el = area.current!;
    const a = from ?? el.selectionStart;
    const next = el.value.slice(0, a) + text + el.value.slice(to ?? el.selectionEnd);
    put(next, a + text.length);
    return next;
  };

  const accept = (n: FormulaName) => {
    const el = area.current!;
    const at = el.selectionStart;
    const { start } = wordBefore(el.value, at);
    const call = n.kind === "function" && el.value[at] !== "(";
    const next = insert(n.name + (call ? "(" : ""), start, at);
    setList([]);
    if (n.name.endsWith(".")) suggest(next, start + n.name.length);
  };

  const run = () => {
    const text = area.current?.value ?? draft;
    if (text !== value) onCommit(text);
  };
  const close = () => (run(), onClose());

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    e.stopPropagation(); // the canvas' shortcuts (Delete, H, M, L…) stay out of the code
    const el = e.currentTarget;
    const text = el.value;
    if (list.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i + (e.key === "ArrowDown" ? 1 : list.length - 1)) % list.length);
        setChosen(true);
        return;
      }
      // Tab takes the suggestion; Enter only once one was chosen with the arrows (else it's a new line).
      if (e.key === "Tab" || (e.key === "Enter" && chosen)) {
        e.preventDefault();
        accept(list[active]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setList([]);
        return;
      }
      if (e.key === "Enter") setList([]);
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (e.shiftKey) close();
      else run();
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    const a = el.selectionStart;
    const b = el.selectionEnd;
    const lineStart = text.lastIndexOf("\n", a - 1) + 1;
    if (e.key === "Tab") {
      e.preventDefault();
      if (a === b && !e.shiftKey) {
        insert(INDENT);
        return;
      }
      // Indent or unindent every selected line.
      const end = b > a && text[b - 1] === "\n" ? b - 1 : b;
      const changed = text.slice(lineStart, end).split("\n")
        .map((l) => (e.shiftKey ? l.replace(/^ {1,4}/, "") : INDENT + l)).join("\n");
      put(text.slice(0, lineStart) + changed + text.slice(end), lineStart, lineStart + changed.length);
      return;
    }
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
      // Keep the line's indentation, one more after a ':' (if, for, def…).
      e.preventDefault();
      const line = text.slice(lineStart, a);
      const indent = /^\s*/.exec(line)![0] + (/:\s*$/.test(line) ? INDENT : "");
      insert("\n" + indent);
      return;
    }
    if (e.key === "Backspace" && a === b && a > lineStart) {
      // Backspace in leading spaces removes one indentation level.
      const line = text.slice(lineStart, a);
      if (/^ +$/.test(line)) {
        e.preventDefault();
        insert("", a - (line.length % 4 || 4), a);
      }
    }
  };

  const lines = draft.split("\n").length;
  const describe = (k: string) => {
    const edge = edges.find((e) => e.targetHandle === k);
    if (!edge) return null;
    const [src] = realEnd(flow.getNodes(), edge.source, edge.sourceHandle);
    const n = flow.getNode(src);
    const d = n?.data as OpNodeData | undefined;
    const title = d?.name || (d && defs[d.type]?.title) || src;
    return { title, summary: results[src]?.summary || results[src]?.message || "" };
  };
  const status = result?.status ?? "pending";

  return createPortal(
    <div className="modal-backdrop" onMouseDown={close}>
      <div className="modal script-editor" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label="Formula code">
        <div className="modal-head">
          <b>
            {data?.name || "Formula"} <span className="muted">· code</span>
          </b>
          <button className="icon-btn" onClick={close} title="Close (Esc); the code is kept">
            ✕
          </button>
        </div>
        <div className="script-body">
          <div className="script-code">
            <div className="script-gutter" ref={gutter} aria-hidden>
              <div style={{ transform: `translateY(${-scroll}px)` }}>
                {Array.from({ length: lines }, (_, i) => (
                  <div key={i} className={i + 1 === badLine ? "bad" : i === caret.line ? "here" : ""}>
                    {i + 1}
                  </div>
                ))}
              </div>
            </div>
            <div className="script-text">
              <textarea
                ref={area}
                value={draft}
                spellCheck={false}
                autoComplete="off"
                wrap="off"
                aria-label="Formula code"
                placeholder={"peak = np.max(a)\nratio = a / peak\nratio"}
                onChange={(e) => {
                  setDraft(e.target.value);
                  suggest(e.target.value, e.target.selectionStart);
                }}
                onKeyDown={onKeyDown}
                onKeyUp={(e) => !list.length && where(e.currentTarget.value, e.currentTarget.selectionStart)}
                onClick={(e) => (setList([]), where(e.currentTarget.value, e.currentTarget.selectionStart))}
                onScroll={(e) => setScroll(e.currentTarget.scrollTop)}
              />
              <span ref={measure} className="script-measure" aria-hidden>
                0123456789
              </span>
              {list.length > 0 && (
                <ul
                  className="formula-suggest script-suggest"
                  role="listbox"
                  style={{
                    top: (caret.line + 1) * LINE + 8 - scroll,
                    left: Math.max(0, caret.col - wordBefore(draft, area.current?.selectionStart ?? 0).word.length) * charW + 10 -
                      (area.current?.scrollLeft ?? 0),
                  }}
                >
                  {list.map((n, i) => (
                    <li
                      key={n.name}
                      role="option"
                      aria-selected={i === active}
                      className={i === active ? "active" : ""}
                      onMouseDown={(e) => {
                        e.preventDefault();
                        accept(n);
                      }}
                      onMouseEnter={() => setActive(i)}
                    >
                      <b className={`kind-${n.kind}`}>
                        {n.name}
                        {n.kind === "function" ? "()" : ""}
                      </b>
                      {n.doc && <span>{n.doc}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
          <aside className="script-side">
            <h4>Inputs</h4>
            <ul className="script-inputs">
              {INPUTS.map((k) => {
                const d = describe(k);
                return (
                  <li key={k} className={d ? "" : "muted"}>
                    <code>{k}</code>{" "}
                    {d ? (
                      <>
                        {d.title}
                        {d.summary && <div className="muted">{d.summary}</div>}
                      </>
                    ) : (
                      "not connected"
                    )}
                  </li>
                );
              })}
            </ul>
            <h4>More outputs</h4>
            <OutputsField nodeId={nodeId} value={String(data?.params.outputs ?? "")} setParam={setParam} />
            <p className="muted small">
              Variables of the script that become outputs too, after Result (up to 4).
            </p>
            <h4>Result</h4>
            <div className={`script-result status-${status}`}>
              {!committed ? (
                <span className="muted">Run (Ctrl+Enter) to see it</span>
              ) : (
                result?.message || result?.summary || (status === "ok" ? "ok" : status)
              )}
            </div>
            <details className="script-help">
              <summary>How scripts work</summary>
              <ul>
                <li>Inputs are <code>a b c d</code> (<code>t</code>: the curve's x); a field is one value per row.</li>
                <li>The last line is the Result, or set <code>result = …</code>.</li>
                <li><code>if / elif / else</code>, <code>for x in …</code>, <code>while</code>, <code>def f(x):</code>, <code>lambda</code>.</li>
                <li>Lists <code>[1, 2]</code>, dicts <code>{"{'L': 1}"}</code>, <code>[v * 2 for v in a]</code>, f-texts <code>f"{"{peak:.1f}"} MPa"</code>.</li>
                <li>NumPy as <code>np.…</code>; no imports or files.</li>
              </ul>
            </details>
          </aside>
        </div>
        <div className="modal-foot script-foot">
          <span className="muted">Ctrl+Enter runs · Tab indents · Esc closes and keeps the code</span>
          <span style={{ flex: 1 }} />
          <button onClick={onClose} title="Close without the changes since the last run">
            Cancel
          </button>
          <button onClick={run} disabled={committed} title="Ctrl+Enter">
            Run
          </button>
          <button className="primary" onClick={close} title="Ctrl+Shift+Enter">
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function OutputsField({ nodeId, value, setParam }: {
  nodeId: string;
  value: string;
  setParam: (id: string, name: string, v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <input
      className="script-outputs"
      value={draft}
      placeholder="e.g. peak, note"
      spellCheck={false}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft !== value && setParam(nodeId, "outputs", draft)}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}
