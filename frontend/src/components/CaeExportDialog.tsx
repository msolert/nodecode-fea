import { useEffect, useState } from "react";
import { exportCae, type CaeExport, type GraphPayload } from "../api";

export function CaeExportDialog({ graph, onClose }: { graph: GraphPayload; onClose: () => void }) {
  const [data, setData] = useState<CaeExport | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    exportCae(graph).then(setData).catch((e: Error) => setError(e.message));
  }, [graph]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const download = () => {
    if (!data) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([data.script], { type: "text/x-python" }));
    a.download = data.filename;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const copy = () => {
    if (!data) return;
    navigator.clipboard.writeText(data.script).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal cae-export" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <b>Send to Abaqus/CAE</b>
          <button className="icon-btn" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="cae-body">
          {error && <div className="fb-error">{error}</div>}
          {!data && !error && <div className="empty">Building the script…</div>}
          {data && (
            <>
              <ol className="cae-steps">
                <li>
                  <b>Download</b> the script below.
                </li>
                <li>
                  In Abaqus/CAE: <b>File → Run Script…</b> and pick <code>{data.filename}</code>.
                </li>
                <li>Each output gets its own CAE viewport; values also go to the message area.</li>
              </ol>
              <h4>It will create</h4>
              <ul className="cae-list">
                {data.contents.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
              {data.skipped.length > 0 && (
                <>
                  <h4>Left out</h4>
                  <ul className="cae-list skipped">
                    {data.skipped.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                </>
              )}
              <pre className="cae-script">{data.script}</pre>
            </>
          )}
        </div>
        <div className="modal-foot">
          <span className="muted">Curves and values are embedded; contours are rebuilt from the ODB in CAE.</span>
          <span style={{ flex: 1 }} />
          <button onClick={copy} disabled={!data}>
            {copied ? "Copied" : "Copy"}
          </button>
          <button className="primary" onClick={download} disabled={!data}>
            Download .py
          </button>
        </div>
      </div>
    </div>
  );
}
