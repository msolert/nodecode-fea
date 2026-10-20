import { useCallback, useEffect, useState } from "react";

/** Panel layout: palette and viewer visibility, where the viewer is docked, and its size. */
export interface Layout {
  paletteOpen: boolean;
  paletteDock: "bottom" | "left";
  paletteHeight: number; // when docked at the bottom
  viewerOpen: boolean;
  dock: "bottom" | "right";
  viewerWidth: number; // when docked right
  viewerHeight: number; // when docked at the bottom
}

const KEY = "nodecode-fea.layout.v1";

function defaults(): Layout {
  return {
    paletteOpen: true,
    paletteDock: "bottom",
    paletteHeight: 170,
    viewerOpen: true,
    dock: "bottom", // leaves the full width for the graph
    viewerWidth: Math.round(window.innerWidth * 0.4),
    viewerHeight: Math.round(window.innerHeight * 0.32),
  };
}

function load(): Layout {
  const base = defaults();
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return base;
    const saved = JSON.parse(raw) as Partial<Layout>;
    return {
      paletteOpen: typeof saved.paletteOpen === "boolean" ? saved.paletteOpen : base.paletteOpen,
      paletteDock: saved.paletteDock === "left" || saved.paletteDock === "bottom" ? saved.paletteDock : base.paletteDock,
      paletteHeight: Number.isFinite(saved.paletteHeight) ? Number(saved.paletteHeight) : base.paletteHeight,
      viewerOpen: typeof saved.viewerOpen === "boolean" ? saved.viewerOpen : base.viewerOpen,
      dock: saved.dock === "right" || saved.dock === "bottom" ? saved.dock : base.dock,
      viewerWidth: Number.isFinite(saved.viewerWidth) ? Number(saved.viewerWidth) : base.viewerWidth,
      viewerHeight: Number.isFinite(saved.viewerHeight) ? Number(saved.viewerHeight) : base.viewerHeight,
    };
  } catch {
    return base; // unreadable or blocked storage
  }
}

export function useLayout() {
  const [layout, setLayout] = useState<Layout>(load);
  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(layout));
    } catch {
      /* storage full or unavailable: the layout just isn't remembered */
    }
  }, [layout]);
  const update = useCallback((patch: Partial<Layout>) => setLayout((l) => ({ ...l, ...patch })), []);
  return [layout, update] as const;
}
