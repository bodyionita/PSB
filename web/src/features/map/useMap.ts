// Server state for the Map tab (TanStack Query, 06 §3c). The grouped first page of a center's
// neighborhood is a read (no LLM), cached by center id so re-centering back to a visited node is
// instant. Per-zone "show more" is an imperative fetch (api.nodeNeighborPage) merged into local
// state by MapScreen, so it isn't a hook here.
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { NeighborZonesResponse } from '../../api/types';

export function useNeighbors(nodeId: string | null) {
  return useQuery<NeighborZonesResponse>({
    queryKey: ['neighbors', nodeId],
    queryFn: () => api.nodeNeighbors(nodeId!),
    enabled: nodeId != null,
    // Keep the previous neighborhood on screen while the next one loads, so a re-center swaps the
    // canvas's graphData in place (plex fade) instead of tearing it down to a loading flash — the
    // canvas stays mounted across hops (ADR-051 §1/§3). Only the very first load has no previous.
    placeholderData: keepPreviousData,
  });
}
