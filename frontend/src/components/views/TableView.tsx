import { downloadCsv, fmt } from "../../format";
import type { Display } from "../../types";

type TableDisplay = Extract<Display, { kind: "table" }>;

export function TableView({ display }: { display: TableDisplay }) {
  return (
    <div className="table-view">
      <div className="view-toolbar">
        <div className="stats">
          {display.stats.map((s) => (
            <span key={s.name} className="stat">
              <b>{s.name}</b> {fmt(s.min)} … {fmt(s.max)}
            </span>
          ))}
        </div>
        <button
          onClick={() => downloadCsv(`${display.title}.csv`, display.columns, display.allRows)}
          title={display.allRows.length < display.total ? `First ${display.allRows.length} rows` : undefined}
        >
          CSV
        </button>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {display.indexed && <th className="muted" title="Row number in a Formula: a[row]">row</th>}
              {display.columns.map((c, j) => (
                <th key={c} title={display.indexed ? `In a Formula: a[:, ${j}]` : undefined}>
                  {display.indexed && <span className="col-index">{j}</span>}
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {display.rows.map((r, i) => (
              <tr key={i}>
                {display.indexed && <td className="muted">{display.rowIndex?.[i] ?? i}</td>}
                {r.map((v, j) => {
                  // the first two columns of a field table are labels (node/element, instance); a list or matrix
                  // (indexed) holds numbers throughout
                  const number = typeof v === "number" && (display.indexed || j > 1 || !Number.isInteger(v));
                  return (
                    <td key={j} className={number ? "num" : ""}>
                      {number ? fmt(v as number, 5) : v ?? "—"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="muted">
        Showing {display.rows.length} of {display.total} rows
      </div>
    </div>
  );
}
