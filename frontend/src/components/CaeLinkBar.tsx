import { useEffect, useRef, useState } from "react";
import type { CaeProduct, CaeStatus } from "../api";

const LABELS: Record<CaeStatus["state"], string> = {
  off: "",
  starting: "{} starting…",
  connected: "{} linked",
  "no-link": "{} running, link not answering",
  closed: "{} closed",
};

export function CaeLinkBar({ abaqus, status, live, syncing, lastSync, onLaunch, onSync, onLive, product, onProduct }: {
  abaqus: string; // "Abaqus: <command>" or why it wasn't found
  product: CaeProduct;
  onProduct: (p: CaeProduct) => void;
  status: CaeStatus;
  live: boolean;
  syncing: boolean;
  lastSync: string;
  onLaunch: () => void;
  onSync: () => void;
  onLive: (on: boolean) => void;
}) {
  const [showLog, setShowLog] = useState(false);
  const state = status.state;
  // The log opens (and stays, as it updates) at its newest lines, unless you scrolled up to read.
  const logBox = useRef<HTMLPreElement>(null);
  const pinned = useRef(true);
  const logText = (status.log ?? []).join("\n");
  useEffect(() => {
    const el = logBox.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [logText, showLog]);
  return (
    <div className="cae-bar">
      {(state === "off" || state === "closed") && (
        <span className="launch-group">
          <button onClick={onLaunch} title={`Start Abaqus linked to this editor (nothing is installed in Abaqus)${abaqus ? `\n${abaqus}` : ""}`}>
            Launch {product === "viewer" ? "Viewer" : product === "cae" ? "CAE" : "Abaqus"}
          </button>
          <select value={product} onChange={(e) => onProduct(e.target.value as CaeProduct)}
            title="Which Abaqus program to start. Auto: Abaqus/Viewer (results only, lighter) when the graph only post-processes, Abaqus/CAE when it builds models">
            <option value="auto">Auto</option>
            <option value="viewer">Viewer</option>
            <option value="cae">CAE</option>
          </select>
        </span>
      )}
      {state !== "off" && (
        <span className={`pill cae-${state}`} title={status.session}>
          {LABELS[state].replace("{}", status.product === "viewer" ? "Viewer" : "CAE")}
        </span>
      )}
      {state === "connected" && (
        <>
          <label className="live-toggle" title="Update CAE automatically whenever the graph changes">
            <input type="checkbox" checked={live} onChange={(e) => onLive(e.target.checked)} /> Live
          </label>
          <button onClick={onSync} disabled={syncing} title="Show the graph's outputs in CAE now">
            {syncing ? "Syncing…" : "Sync"}
          </button>
          {lastSync && <span className="muted sync-time">{lastSync}</span>}
        </>
      )}
      {state !== "off" && (
        <button className="icon-btn" onClick={() => ((pinned.current = true), setShowLog(true))}
          title="What CAE did on each update (built, unchanged, deleted, what failed) and what the link is doing">
          Log
        </button>
      )}
      {showLog && (
        <div className="modal-backdrop" onMouseDown={() => setShowLog(false)}>
          <div className="modal cae-log" onMouseDown={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <b>CAE log</b>
              <button className="icon-btn" onClick={() => setShowLog(false)}>
                ✕
              </button>
            </div>
            <div className="cae-body">
              <p className="muted">
                Each update, with what CAE reported under it (what was built, unchanged or deleted, and anything that
                failed). Session folder: <code>{status.session}</code>; if the link doesn't work, send the contents
                below (or <code>link.log</code> from that folder).
              </p>
              <pre className="cae-script" ref={logBox}
                onScroll={(e) => {
                  const el = e.currentTarget;
                  pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 20;
                }}>
                {logText || "(nothing logged yet)"}
              </pre>
            </div>
            <div className="modal-foot" style={{ padding: "10px 14px" }}>
              <span style={{ flex: 1 }} />
              <button onClick={() => navigator.clipboard.writeText(logText)}>Copy</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
