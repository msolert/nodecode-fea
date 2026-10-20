import { useEffect, useState } from "react";
import { browseDir, type BrowseResult } from "../api";

const LAST_DIR_KEY = "nodecode-fea.lastDir";

function readLastDir(): string {
  try {
    return localStorage.getItem(LAST_DIR_KEY) ?? "";
  } catch {
    return "";
  }
}

function startDir(current: string): string {
  if (current && !current.startsWith("mock://")) {
    const cut = Math.max(current.lastIndexOf("/"), current.lastIndexOf("\\"));
    if (cut > 0) return current.slice(0, cut + 1);
  }
  return readLastDir();
}

function size(bytes: number) {
  if (bytes > 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes > 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

/** A file picker served by the backend: the browser itself can't reveal full local paths. */
export function FileBrowser({
  current,
  extensions,
  onPick,
  onClose,
}: {
  current: string;
  extensions?: string[];
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const exts = extensions?.length ? extensions : [".odb"];
  const what = exts.length === 1 && exts[0] === ".odb" ? "ODB" : exts.map((e) => e.replace(".", "").toUpperCase()).join(", ");
  const [dir, setDir] = useState(() => startDir(current));
  const [typed, setTyped] = useState("");
  const [data, setData] = useState<BrowseResult | null>(null);
  const [error, setError] = useState("");
  const [chosen, setChosen] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    browseDir(dir, exts)
      .then((r) => {
        if (!live) return;
        setData(r);
        setTyped(r.path);
        setError("");
        setChosen(null);
        try {
          localStorage.setItem(LAST_DIR_KEY, r.path);
        } catch {
          /* ignore */
        }
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [dir, exts.join(",")]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const join = (name: string) => {
    const base = data?.path ?? "";
    const sep = base.includes("\\") && !base.includes("/") ? "\\" : "/";
    return base.endsWith(sep) ? base + name : base + sep + name;
  };

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal file-browser" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <b>Open {what}</b>
          <button className="icon-btn" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="fb-bar">
          <button disabled={!data?.parent} onClick={() => data?.parent && setDir(data.parent)} title="Up one folder">
            ↑
          </button>
          <button onClick={() => data && setDir(data.home)} title="Home folder">
            ⌂
          </button>
          {data && data.roots.length > 1 && (
            <select value="" onChange={(e) => e.target.value && setDir(e.target.value)}>
              <option value="">Drive…</option>
              {data.roots.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          )}
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && setDir(typed)}
            spellCheck={false}
          />
        </div>
        {error && <div className="fb-error">{error}</div>}
        <div className="fb-list">
          {data?.dirs.map((d) => (
            <div key={`d-${d}`} className="fb-item" onClick={() => setDir(join(d))}>
              <span className="fb-icon">📁</span>
              {d}
            </div>
          ))}
          {data?.files.map((f) => {
            const full = join(f.name);
            return (
              <div
                key={`f-${f.name}`}
                className={`fb-item fb-file ${chosen === full ? "chosen" : ""}`}
                onClick={() => setChosen(full)}
                onDoubleClick={() => onPick(full)}
              >
                <span className="fb-icon">◆</span>
                {f.name}
                <span className="fb-meta">
                  {size(f.size)} · {new Date(f.modified * 1000).toLocaleString()}
                </span>
              </div>
            );
          })}
          {data && !data.dirs.length && !data.files.length && <div className="empty">No folders or {exts.join(" / ")} files here.</div>}
        </div>
        <div className="modal-foot">
          <button className="link-btn" onClick={() => onPick("mock://cantilever?load=700")}>
            Use the demo beam instead
          </button>
          <span style={{ flex: 1 }} />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!chosen} onClick={() => chosen && onPick(chosen)}>
            Open
          </button>
        </div>
      </div>
    </div>
  );
}
