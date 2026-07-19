import { createContext, useContext } from 'react';

// Cross-tree hook to deep-link into the Review tab and highlight one queue item (ADR-054 §5 replan) —
// a graph-health `pending-review-aging` offender is a review-queue id (not a node), so its chip jumps
// here instead of opening a NodePreview. Provided by AppShell (sets a one-shot `reviewSeed` + switches
// to the Review tab); ReviewScreen consumes the seed to scroll-to + transiently highlight the item.
// Mirrors mapNav's MapNavContext.

export interface ReviewNav {
  openReviewItem: (reviewItemId: string) => void;
  // Open the Review tab without seeding a specific item (M9.8 T6, ADR-064 §4) — the graph-health
  // duplicate-candidates section links here for the lower-confidence pairs T4 filed to Review (no
  // single id to highlight). Optional so a provider that only wires `openReviewItem` still
  // type-checks; consumers guard on its presence.
  openReview?: () => void;
}

export const ReviewNavContext = createContext<ReviewNav | null>(null);

// Null when there is no provider — callers guard.
export function useReviewNav(): ReviewNav | null {
  return useContext(ReviewNavContext);
}
