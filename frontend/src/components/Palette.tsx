import { useMemo, useState } from "react";
import { CATEGORY_COLORS, PORT_COLORS, PORT_TYPE_NAMES, nodeMatches, oldNames, type Catalog, type PortType } from "../types";

export const DRAG_MIME = "application/x-abaqus-node";

export function Palette({ catalog, onAdd, onCollapse, dock, onDock, library = [], onAddGroup, onRemoveGroup }: {
  catalog: Catalog;
  onAdd: (type: string) => void;
  library?: { name: string; color: string; nodes: unknown[] }[];
  onAddGroup?: (index: number) => void;
  onRemoveGroup?: (index: number) => void;
  onCollapse: () => void;
  dock: "bottom" | "left";
  onDock: () => void;
}) {
  const [query, setQuery] = useState("");
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    return catalog.categories
      .map((cat) => ({
        cat,
        nodes: catalog.nodes.filter(
          (n) =>
            n.category === cat &&
            !n.hidden &&
            nodeMatches(catalog.nodes, n, q),
        ),
      }))
      .filter((g) => g.nodes.length);
  }, [catalog, query]);

  return (
    <aside className={`palette palette-${dock}`}>
      <div className="palette-top">
      <input
        className="palette-search"
        placeholder="Search nodes…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
        <button className="icon-btn" onClick={onDock} title={dock === "bottom" ? "Dock on the left" : "Dock at the bottom"}>
          {dock === "bottom" ? "◧" : "⬓"}
        </button>
        <button className="icon-btn" onClick={onCollapse} title="Hide the nodes (Ctrl+B)">
          {dock === "bottom" ? "˅" : "‹"}
        </button>
      </div>
      <div className="palette-groups">
      {groups.map(({ cat, nodes }) => (
        <section key={cat}>
          <h3 style={{ color: CATEGORY_COLORS[cat] }}>{cat}</h3>
          {nodes.map((n) => (
            <button
              key={n.type}
              className="palette-item"
              title={n.description + (oldNames(catalog.nodes, n.type).length ? `\n(Replaces ${oldNames(catalog.nodes, n.type).join(", ")})` : "")}
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData(DRAG_MIME, n.type);
                e.dataTransfer.effectAllowed = "move";
              }}
              onClick={() => onAdd(n.type)}
            >
              <span className="swatch" style={{ background: CATEGORY_COLORS[cat] }} />
              {n.title}
            </button>
          ))}
        </section>
      ))}
      {library.some((l) => !query.trim() || l.name.toLowerCase().includes(query.trim().toLowerCase())) && (
        <section>
          <h3 style={{ color: "#c9d1dc" }}>My groups</h3>
          {library.map((l, i) =>
            query.trim() && !l.name.toLowerCase().includes(query.trim().toLowerCase()) ? null : (
              <button
                key={l.name}
                className="palette-item"
                title={`Your saved group: ${l.nodes.length} nodes. Click or drag to add (collapsed); right-click to remove.`}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_MIME + "-group", String(i));
                  e.dataTransfer.effectAllowed = "move";
                }}
                onClick={() => onAddGroup?.(i)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  if (confirm(`Remove "${l.name}" from My groups?`)) onRemoveGroup?.(i);
                }}
              >
                <span className="swatch" style={{ background: l.color }} />
                {l.name}
              </button>
            ),
          )}
        </section>
      )}
      <section className="type-legend">
        <h3>Wire colours</h3>
        {(Object.keys(PORT_COLORS) as PortType[]).map((t) => (
          <div key={t}>
            <span className="help-dot" style={{ background: PORT_COLORS[t] }} />
            {PORT_TYPE_NAMES[t]}
          </div>
        ))}
      </section>
      </div>
      <p className="palette-tip">
        Click or drag to add. Drag from a port to connect: ports that fit light up; drop on empty canvas to pick a
        node that fits. Press <b>?</b> on a node to see what
        it needs. Drag on empty canvas to select a rectangle (middle mouse or Space+drag pans), Ctrl+click to add to the selection, Ctrl+D to duplicate, right-click for more.
        Ctrl+Z undoes. Ctrl+B hides this list, Ctrl+J the data panel.
      </p>
    </aside>
  );
}
