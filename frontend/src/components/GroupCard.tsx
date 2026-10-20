import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { memo, useState } from "react";
import { useEditor } from "../context";
import { PORT_COLORS, PORT_TYPE_NAMES, type PortType } from "../types";

export interface GroupPort {
  id: string; // "<member id>::<member handle>"
  label: string;
  type: PortType;
}

export interface GroupData extends Record<string, unknown> {
  type: "group";
  name: string;
  color: string;
  collapsed?: boolean;
  members: string[];
  // Filled in for display (not saved): the ports a collapsed group shows, its status and summary.
  ins?: GroupPort[];
  outs?: GroupPort[];
  status?: string;
  summary?: string;
}

export const GROUP_COLORS = ["#4fb3e8", "#9a7bff", "#5fd08a", "#f0a04b", "#ef5f8f", "#9aa6b8"];

/** A frame around nodes that move together; collapsed, it stands in for them as one node. */
function GroupCardImpl({ id, data, selected }: NodeProps<Node<GroupData>>) {
  const { renaming, setRenaming, setGroup, openPortMenu } = useEditor();
  const [hover, setHover] = useState(false);
  const head = (
    <div className="group-head" style={{ background: data.color }}>
      <button
        className="nodrag group-toggle"
        title={data.collapsed ? "Expand: show the nodes inside" : "Collapse into one node"}
        onClick={(e) => {
          e.stopPropagation();
          setGroup(id, { collapsed: !data.collapsed });
        }}
      >
        {data.collapsed ? "▸" : "▾"}
      </button>
      {renaming === id ? (
        <input
          className="nodrag rename-input"
          autoFocus
          defaultValue={data.name}
          onFocus={(e) => e.target.select()}
          onBlur={(e) => (setGroup(id, { name: e.target.value.trim() || "Group" }), setRenaming(null))}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") setRenaming(null);
          }}
        />
      ) : (
        <span className="group-name" title="Double-click to rename" onDoubleClick={() => setRenaming(id)}>
          {data.name} <small>{data.members.length} nodes</small>
        </span>
      )}
      {(hover || selected) && (
        <span className="group-colors nodrag">
          {GROUP_COLORS.map((c) => (
            <button key={c} style={{ background: c }} title="Colour" onClick={(e) => (e.stopPropagation(), setGroup(id, { color: c }))} />
          ))}
        </span>
      )}
      {data.collapsed && <span className={`dot dot-${data.status ?? "pending"}`} />}
    </div>
  );

  if (!data.collapsed) {
    return (
      <div
        className={`group-frame ${selected ? "selected" : ""}`}
        style={{ borderColor: data.color, background: `${data.color}14` }}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
      >
        {head}
      </div>
    );
  }

  const ins = data.ins ?? [];
  const outs = data.outs ?? [];
  const rows = Math.max(ins.length, outs.length);
  return (
    <div
      className={`node group-node status-${data.status ?? "pending"} ${selected ? "selected" : ""}`}
      style={{ borderColor: data.color }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {head}
      <div className="node-ports">
        {Array.from({ length: rows }, (_, i) => {
          const a = ins[i];
          const b = outs[i];
          return (
            <div className="port-row" key={i}>
              {a ? (
                <div className="port port-in" title={`${a.label} — ${PORT_TYPE_NAMES[a.type]}`}>
                  <Handle type="target" position={Position.Left} id={a.id}
                    onClick={(e) => (e.stopPropagation(), openPortMenu(id, a.id, "in", e.clientX, e.clientY))}
                    style={{ background: PORT_COLORS[a.type], color: PORT_COLORS[a.type] }} />
                  <span>{a.label}</span>
                </div>
              ) : <div />}
              {b ? (
                <div className="port port-out" title={`${b.label} — ${PORT_TYPE_NAMES[b.type]}`}>
                  <span>{b.label}</span>
                  <Handle type="source" position={Position.Right} id={b.id}
                    onClick={(e) => (e.stopPropagation(), openPortMenu(id, b.id, "out", e.clientX, e.clientY))}
                    style={{ background: PORT_COLORS[b.type], color: PORT_COLORS[b.type] }} />
                </div>
              ) : <div />}
            </div>
          );
        })}
      </div>
      {data.summary && <div className={`node-foot foot-${data.status}`}>{data.summary}</div>}
    </div>
  );
}

export const GroupCard = memo(GroupCardImpl);
