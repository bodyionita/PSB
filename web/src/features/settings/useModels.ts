// Server state for Settings → Models (TanStack Query, 06 §4 / ADR-025 / ADR-043). `GET /settings`
// is the one authoritative view of the 3 routing groups (effective saved-over-seed routing + the
// registry's pickable models with effort capability/levels); `PUT /settings/models` saves one group
// and busts the routing cache forward-live. Saving the Chat group changes the composer's default
// model (`GET /chat/models`), so that key is invalidated too.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { ModelRoutingUpdate, SettingsResponse } from '../../api/types';

export const SETTINGS_KEY = ['settings'] as const;

export function useSettings() {
  return useQuery<SettingsResponse>({
    queryKey: SETTINGS_KEY,
    queryFn: () => api.getSettings(),
  });
}

export function useSaveModels() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (update: ModelRoutingUpdate) => api.saveModels(update),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SETTINGS_KEY });
      // The Chat group's active model is the chat composer's default — keep the picker in sync.
      qc.invalidateQueries({ queryKey: ['chat', 'models'] });
    },
  });
}
