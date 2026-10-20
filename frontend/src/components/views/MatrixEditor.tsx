import { useEffect, useRef, useState } from "react";

/** An Input's list or matrix as a grid: cells as text, a 1D list shown as one column. */
type Grid = { cells: string[][]; matrix: boolean };

/** Parse what the Input's Value holds: [[1, 2], [3, 4]], [1, 2], ["A", "B"], 1, 2, 3 or 1, 2; 3, 4. Null when it
 * is deeper than a matrix (lists of point tables…), which the grid can't show. */
export function parseGrid(raw: string): Grid | null {
  const text = raw.trim();
  if (!text) return { cells: [[""]], matrix: false };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    if (/[[\]]/.test(text)) return null;
    const rows = text.split(/[;\n]+/).map((r) => r.split(/,/).map((c) => c.trim())).filter((r) => r.some((c) => c));
    if (rows.length === 1) return { cells: rows[0].map((c) => [c]), matrix: false };
    return { cells: rows, matrix: true };
  }
  const cell = (v: unknown) => (typeof v === "number" || typeof v === "string" ? String(v) : null);
  if (!Array.isArray(value)) return cell(value) === null ? null : { cells: [[cell(value)!]], matrix: false };
  if (value.every((v) => !Array.isArray(v))) {
    const cells = value.map(cell);
    return cells.some((c) => c === null) ? null : { cells: cells.map((c) => [c!]), matrix: false };
  }
  if (!value.every((r) => Array.isArray(r) && r.every((v) => !Array.isArray(v)))) return null;
  const cells = (value as unknown[][]).map((r) => r.map(cell));
  if (cells.some((r) => r.some((c) => c === null))) return null;
  return { cells: cells as string[][], matrix: true };
}

const isNumber = (c: string) => c.trim() !== "" && Number.isFinite(Number(c));

/** A list of texts (names…): no cell holds a number. */
const textGrid = (grid: Grid) =>
  grid.cells.some((r) => r.some((c) => c.trim() !== "")) && !grid.cells.some((r) => r.some(isNumber));

/** Back to the Input's Value: numbers stay numbers (empty cells count as 0), a list of names is quoted text; a
 * cell that isn't a number in a numeric grid is written quoted, so the Input reports it. */
export function writeGrid(grid: Grid): string {
  const texts = textGrid(grid);
  const item = (c: string) =>
    texts || (c.trim() !== "" && !isNumber(c)) ? JSON.stringify(c.trim()) : isNumber(c) ? String(Number(c)) : "0";
  if (!grid.matrix) {
    const items = grid.cells.map((r) => item(r[0] ?? ""));
    return texts ? `[${items.join(", ")}]` : items.join(", ");
  }
  return `[${grid.cells.map((r) => `[${r.map(item).join(", ")}]`).join(", ")}]`;
}

export function MatrixEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [grid, setGrid] = useState<Grid | null>(() => parseGrid(value));
  const written = useRef(value);
  const table = useRef<HTMLTableElement>(null);
  const [focusCell, setFocusCell] = useState<string | null>(null);
  // move the cursor once the cell exists (Enter on the last row adds it first)
  useEffect(() => {
    if (!focusCell) return;
    const input = table.current?.querySelector<HTMLInputElement>(`input[data-cell="${focusCell}"]`);
    if (input) {
      input.focus();
      input.select();
      setFocusCell(null);
    }
  }, [focusCell, grid]);
  // the Value changed elsewhere (typed in the node, undo): show that
  useEffect(() => {
    if (value !== written.current) {
      written.current = value;
      setGrid(parseGrid(value));
    }
  }, [value]);
  if (!grid) return null;
  const columns = Math.max(1, ...grid.cells.map((r) => r.length));

  const update = (cells: string[][], matrix = grid.matrix) => {
    const next = { cells, matrix: matrix || cells.some((r) => r.length > 1) };
    setGrid(next);
    written.current = writeGrid(next);
    onChange(written.current);
  };
  const focus = (i: number, j: number) => i >= 0 && setFocusCell(`${i}:${j}`);
  const texts = textGrid(grid);
  const blankRow = () => Array.from({ length: columns }, () => "");
  const set = (i: number, j: number, text: string) =>
    update(grid.cells.map((r, a) => (a === i ? Array.from({ length: columns }, (_, b) => (b === j ? text : r[b] ?? "")) : r)));
  const paste = (i: number, j: number, text: string) => {
    // a block copied from a spreadsheet (tabs and lines) or comma/semicolon text fills from this cell on
    const rows = text.replace(/\r/g, "").split(/[\n;]/).filter((r) => r.trim()).map((r) => r.split(/[\t,]/).map((c) => c.trim()));
    const width = Math.max(columns, j + Math.max(...rows.map((r) => r.length)));
    const cells = grid.cells.map((r) => Array.from({ length: width }, (_, b) => r[b] ?? ""));
    rows.forEach((r, a) => {
      while (cells.length <= i + a) cells.push(Array.from({ length: width }, () => ""));
      r.forEach((c, b) => (cells[i + a][j + b] = c));
    });
    update(cells);
  };

  return (
    <div className="matrix-editor">
      <div className="view-toolbar">
        <span className="muted">
          {grid.matrix ? `${grid.cells.length} × ${columns} matrix` : `list of ${grid.cells.length}`} · edit the cells,
          or paste a block from a spreadsheet · Enter: next row
        </span>
        <span className="matrix-buttons">
          <button onClick={() => (update([...grid.cells, blankRow()]), focus(grid.cells.length, 0))}>+ Row</button>
          <button onClick={() => update(grid.cells.map((r) => [...r, ...Array(columns - r.length + 1).fill("")]), true)}>
            + Column
          </button>
          <button disabled={columns < 2} onClick={() => update(grid.cells.map((r) => r.slice(0, columns - 1)))}>
            − Column
          </button>
        </span>
      </div>
      <div className="table-scroll">
        <table ref={table}>
          <thead>
            <tr>
              <th className="muted" title="Row number in a Formula: a[row]">row</th>
              {Array.from({ length: columns }, (_, j) => (
                <th key={j} title={grid.matrix ? `In a Formula: a[:, ${j}]` : undefined}>
                  {grid.matrix ? j : "value"}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {grid.cells.map((r, i) => (
              <tr key={i}>
                <td className="muted">{i}</td>
                {Array.from({ length: columns }, (_, j) => (
                  <td key={j} className="matrix-cell">
                    <input
                      data-cell={`${i}:${j}`}
                      className={isNumber(r[j] ?? "") ? "num" : !texts && (r[j] ?? "").trim() ? "bad" : ""}
                      title={!texts && (r[j] ?? "").trim() && !isNumber(r[j] ?? "") ? "Not a number" : undefined}
                      value={r[j] ?? ""}
                      placeholder="0"
                      onChange={(e) => set(i, j, e.target.value)}
                      onPaste={(e) => {
                        const text = e.clipboardData.getData("text");
                        if (/[\t\n,;]/.test(text.trim())) {
                          e.preventDefault();
                          paste(i, j, text);
                        }
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          if (i === grid.cells.length - 1) update([...grid.cells, blankRow()]);
                          focus(i + 1, j);
                        } else if (e.key === "ArrowDown") focus(i + 1, j);
                        else if (e.key === "ArrowUp") focus(i - 1, j);
                      }}
                    />
                  </td>
                ))}
                <td>
                  <button
                    className="icon-btn"
                    title="Delete this row"
                    disabled={grid.cells.length < 2}
                    onClick={() => update(grid.cells.filter((_, a) => a !== i))}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
