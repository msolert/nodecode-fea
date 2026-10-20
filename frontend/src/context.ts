import { createContext, useContext } from "react";
import type { GroupData } from "./components/GroupCard";
import type { NodeDef, NodeResult, ParamValue } from "./types";

export interface EditorContextValue {
  defs: Record<string, NodeDef>;
  results: Record<string, NodeResult>;
  setParam: (nodeId: string, name: string, value: ParamValue) => void;
  browse: (nodeId: string, param: string, current: string, extensions?: string[]) => void;
  caeConnected: boolean;
  pickInCae: (nodeId: string) => void;
  picking: string | null; // node id currently waiting for a pick in CAE
  nodeAction: (nodeId: string, action: string) => void; // a node's button, e.g. Run / Kill on Job
  wiredParams: Set<string>; // "<nodeId>|<param>" for settings fed by a wire, "<nodeId>|><input>" for wired inputs
  openPortMenu: (nodeId: string, handle: string, side: "in" | "out", x: number, y: number) => void;
  setName: (nodeId: string, name: string) => void;
  renaming: string | null; // node whose name is being edited
  setRenaming: (nodeId: string | null) => void;
  toggleDisabled: (nodeId: string) => void;
  setGroup: (groupId: string, patch: Partial<GroupData>) => void;
  cycleSize: (nodeId: string) => void;
  setWidth: (nodeId: string, width: number | undefined) => void;
}

export const EditorContext = createContext<EditorContextValue>({
  defs: {},
  results: {},
  setParam: () => {},
  browse: () => {},
  caeConnected: false,
  pickInCae: () => {},
  picking: null,
  nodeAction: () => {},
  wiredParams: new Set(),
  openPortMenu: () => {},
  setName: () => {},
  renaming: null,
  setRenaming: () => {},
  toggleDisabled: () => {},
  setGroup: () => {},
  cycleSize: () => {},
  setWidth: () => {},
});

export const useEditor = () => useContext(EditorContext);
