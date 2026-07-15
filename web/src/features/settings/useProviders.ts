// Server state for Settings → Providers (TanStack Query, 06 §4 / ADR-044). A thin read over
// `GET /admin/providers` — one row per registered provider with its sticky `last_error`,
// `last_success_at` and `consecutive_failures`. Read-only (no mutations, ADR-006). Status is
// in-memory on the server and inherently live, so we poll gently so the card stays a current
// 30-second glance (and a forced-failure→recovery shows up without a manual reload).
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { ProvidersResponse } from '../../api/types';

export const PROVIDERS_KEY = ['providers'] as const;

export function useProviders() {
  return useQuery<ProvidersResponse>({
    queryKey: PROVIDERS_KEY,
    queryFn: () => api.providers(),
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  });
}
