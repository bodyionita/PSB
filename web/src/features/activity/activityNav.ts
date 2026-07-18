import { createContext, useContext } from 'react';

// Cross-tab hook to jump into the Activity tab's Captures feed sub-tab (M8.1, ADR-054 §4 — the
// Capture tab Recents' "see all" link). Mirrors mapNav/reviewNav/nodePreviewNav: defined here (the
// destination feature), consumed from anywhere (RecentCaptures' "see all"). The provider is owned
// by AppShell (mirrors ReviewNavContext's `openReviewItem` — sets a one-shot seed + switches tabs);
// AppShell is out of this task's file boundary this batch (ADR-054 §4 replan / M8.1 T4), so the
// provider isn't wired yet — degrades to `null`, and `useActivityNav()` callers must guard (same
// degrade convention as `useReviewNav`/`useNodePreview`). See the M8.1 T4 report for the exact
// (small, isomorphic-to-openReviewItem) wiring AppShell still needs.
export interface ActivityNav {
  openCaptures: () => void;
  // Deep-link to one processing run's Activity view (M9.6 T5, ADR-061 §10) — the capture row's
  // "See processing" link. Optional so a provider that only wires `openCaptures` still type-checks;
  // consumers guard on its presence (same degrade convention as the whole hook being null).
  openRun?: (runId: string) => void;
}

export const ActivityNavContext = createContext<ActivityNav | null>(null);

export function useActivityNav(): ActivityNav | null {
  return useContext(ActivityNavContext);
}
