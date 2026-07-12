import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '../../api/client';
import type { MeResponse } from '../../api/types';

const ME_KEY = ['auth', 'me'] as const;

// A 401 is a normal "not logged in" state, not a query error — resolve it to a value so the
// UI simply shows the login screen instead of an error boundary.
async function fetchMe(): Promise<MeResponse> {
  try {
    return await api.me();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      return { authenticated: false, session_created_at: null };
    }
    throw err;
  }
}

export function useMe() {
  return useQuery({ queryKey: ME_KEY, queryFn: fetchMe, retry: false });
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (password: string) => api.login(password),
    onSuccess: () => qc.invalidateQueries({ queryKey: ME_KEY }),
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.logout(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ME_KEY }),
  });
}
