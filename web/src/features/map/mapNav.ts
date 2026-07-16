import { createContext, useContext } from 'react';

// Cross-tree hook to open a node in the Map tab: sets the shell's `mapSeed` and switches to the map
// tab (ADR-051 §8 — entry from Search cards + the NodePreview edge rows; "the edges are the entry
// into the map"). Provided by AppShell. Kept out of ui/ so the shared NodePreview primitive stays
// feature-agnostic — it takes an optional `onOpenNode` callback that consumers wire to this.

export interface MapNav {
  openInMap: (nodeId: string) => void;
}

export const MapNavContext = createContext<MapNav | null>(null);

// Null when there is no provider (e.g. a NodePreview rendered outside the shell) — callers guard.
export function useMapNav(): MapNav | null {
  return useContext(MapNavContext);
}
