import { useEffect, useRef, useState } from "react";
import { ScriptEditor } from "./ScriptEditor";

/** What a Formula accepts (from the server's whitelist), loaded once and shared by every Formula node. */
export interface FormulaName {
  name: string;
  kind: "input" | "function" | "const";
  doc: string;
}

let names: FormulaName[] | null = null;
let loading: Promise<FormulaName[]> | null = null;
export function loadNames(): Promise<FormulaName[]> {
  if (names) return Promise.resolve(names);
  loading ??= fetch("/api/formula-names")
    .then((r) => r.json())
    .then((j: { names: FormulaName[] }) => (names = j.names))
    .catch(() => (names = []));
  return loading;
}

const MAX_SHOWN = 12;

/** The word being typed just before the caret: a name, possibly dotted (np.lin…). */
export function wordBefore(text: string, caret: number): { start: number; word: string } {
  const m = /[A-Za-z_][\w.]*$/.exec(text.slice(0, caret));
  return m ? { start: caret - m[0].length, word: m[0] } : { start: caret, word: "" };
}

export function matches(all: FormulaName[], word: string): FormulaName[] {
  if (!word) return [];
  const w = word.toLowerCase();
  // Prefix matches first (np.me → np.mean, np.median), then names containing the word (max → np.argmax).
  const starts = all.filter((n) => n.name.toLowerCase().startsWith(w));
  const tail = w.includes(".") ? w : `.${w}`;
  const inside = all.filter((n) => !starts.includes(n) && n.name.toLowerCase().includes(tail));
  return [...starts, ...inside].filter((n) => n.name !== word).slice(0, MAX_SHOWN);
}

/** A Formula's text box with autocomplete of inputs, functions (sqrt, np.max, np.linalg.inv…) and constants, and
 * a button (⤢) that opens the code window for scripts of several lines. A script shows as its first lines here. */
export function FormulaInput({ value, onCommit, placeholder, nodeId }: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  nodeId?: string;
}) {
  const [open, setOpen] = useState(false);
  const editor = open && nodeId && (
    <ScriptEditor nodeId={nodeId} value={value} onCommit={onCommit} onClose={() => setOpen(false)} />
  );
  const expand = nodeId && (
    <button className="nodrag icon-btn script-open" title="Open the code window: a script of several lines" onClick={() => setOpen(true)}>
      ⤢
    </button>
  );
  if (value.includes("\n")) {
    const lines = value.split("\n");
    return (
      <div className="formula-input nodrag">
        <button className="nodrag formula-preview" title="Edit the script" onClick={() => setOpen(true)}>
          <pre>
            {lines.slice(0, 6).join("\n")}
            {lines.length > 6 && <span className="muted">{`\n… ${lines.length - 6} more lines`}</span>}
          </pre>
        </button>
        {editor}
      </div>
    );
  }
  return <OneLine value={value} onCommit={onCommit} placeholder={placeholder} extra={<>{expand}{editor}</>} />;
}

function OneLine({ value, onCommit, placeholder, extra }: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  extra: React.ReactNode;
}) {
  const [draft, setDraft] = useState(value);
  const [list, setList] = useState<FormulaName[]>([]);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => setDraft(value), [value]);
  useEffect(() => void loadNames(), []);

  const suggest = (text: string, caret: number) => {
    loadNames().then((all) => {
      setList(matches(all, wordBefore(text, caret).word));
      setActive(0);
    });
  };

  const accept = (n: FormulaName) => {
    const el = input.current;
    if (!el) return;
    const caret = el.selectionStart ?? draft.length;
    const { start } = wordBefore(draft, caret);
    const call = n.kind === "function" && draft[caret] !== "(";
    const insert = n.name + (call ? "(" : "");
    const next = draft.slice(0, start) + insert + draft.slice(caret);
    setDraft(next);
    setList([]);
    requestAnimationFrame(() => {
      el.focus();
      const at = start + insert.length;
      el.setSelectionRange(at, at);
      // After "np." keep suggesting (np.linalg.); after a full name, stop.
      if (n.name.endsWith(".")) suggest(next, at);
    });
  };

  return (
    <div className="formula-input nodrag">
      <input
        ref={input}
        className="nodrag"
        value={draft}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        onChange={(e) => {
          setDraft(e.target.value);
          suggest(e.target.value, e.target.selectionStart ?? e.target.value.length);
        }}
        onBlur={() => {
          setList([]);
          if (draft !== value) onCommit(draft);
        }}
        onKeyDown={(e) => {
          if (list.length) {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => (i + (e.key === "ArrowDown" ? 1 : list.length - 1)) % list.length);
              return;
            }
            if (e.key === "Enter" || e.key === "Tab") {
              e.preventDefault();
              accept(list[active]);
              return;
            }
            if (e.key === "Escape") {
              e.stopPropagation();
              setList([]);
              return;
            }
          }
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
      {list.length > 0 && (
        <ul className="formula-suggest nowheel" role="listbox">
          {list.map((n, i) => (
            <li
              key={n.name}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : ""}
              // mousedown, not click: keep the input focused so blur doesn't commit first.
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
      {extra}
    </div>
  );
}
