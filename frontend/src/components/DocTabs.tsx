import { useState } from "react";
import type { DocMeta } from "../docs";

/** The graphs open in the editor, one tab each: click to switch, double-click to rename, × to close, + for a new one. */
export function DocTabs({ docs, active, dirty, clashes, onSelect, onNew, onClose, onRename }: {
  docs: DocMeta[];
  active: string;
  dirty: Record<string, boolean>;
  clashes: Record<string, string[]>; // per tab: the CAE models another open tab also builds
  onSelect: (id: string) => void;
  onNew: () => void;
  onClose: (id: string) => void;
  onRename: (id: string, name: string) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  return (
    <div className="doc-tabs" role="tablist" aria-label="Open graphs">
      {docs.map((d) => {
        const clash = clashes[d.id] ?? [];
        return (
          <div key={d.id} role="tab" aria-selected={d.id === active} className={`doc-tab ${d.id === active ? "on" : ""}`}
            onClick={() => d.id !== active && onSelect(d.id)}
            onDoubleClick={() => setEditing(d.id)}
            onAuxClick={(e) => e.button === 1 && onClose(d.id)}
            title={clash.length ? `Another open tab also builds ${clash.join(", ")} in CAE: syncing one rebuilds it over the other. Rename the Model in one of them.`
              : "Double-click to rename"}>
            {editing === d.id ? (
              <input autoFocus defaultValue={d.name} onClick={(e) => e.stopPropagation()}
                onBlur={(e) => editing === d.id && (onRename(d.id, e.currentTarget.value.trim() || d.name), setEditing(null))}
                onKeyDown={(e) => {
                  e.stopPropagation();  // not the editor's shortcuts
                  if (e.key === "Enter") (onRename(d.id, e.currentTarget.value.trim() || d.name), setEditing(null));
                  if (e.key === "Escape") setEditing(null);
                }} />
            ) : (
              <span className="doc-name">{d.name}{dirty[d.id] ? " ✱" : ""}</span>
            )}
            {clash.length > 0 && <span className="doc-clash">⚠</span>}
            <button className="doc-close" title="Close this graph" aria-label={`Close ${d.name}`}
              onClick={(e) => (e.stopPropagation(), onClose(d.id))}>×</button>
          </div>
        );
      })}
      <button className="doc-new" onClick={onNew} title="A new, empty graph in its own tab">+</button>
    </div>
  );
}
