// Server state for the Chat tab (TanStack Query, 06 §2). Chat is non-streaming: `POST /chat` is a
// mutation whose full response the screen reveals client-side. The session list is a read that polls
// briefly while a just-created thread is still waiting on its best-effort `quick`-tier title
// (ADR-043) — mirroring the capture strip's settle window.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type {
  AutoRecordedItem,
  ChatRequest,
  ChatResponse,
  ChatSessionItem,
  RememberResponse,
} from '../../api/types';

export const CHAT_SESSIONS_KEY = ['chat', 'sessions'] as const;
export const AUTO_RECORDED_KEY = ['chat', 'auto-recorded'] as const;

// Poll cadence + window for a freshly-created thread whose title hasn't landed yet. Titling runs
// after the first exchange and is quick; bound the poll so an untitled-forever session (titling
// model down) doesn't poll indefinitely.
const TITLE_POLL_MS = 3000;
const TITLE_SETTLE_MS = 90_000;

function awaitingTitle(sessions: ChatSessionItem[] | undefined): boolean {
  if (!sessions) return false;
  return sessions.some((s) => {
    if (s.title) return false;
    const created = s.created_at ? Date.parse(s.created_at) : NaN;
    return Number.isFinite(created) && Date.now() - created < TITLE_SETTLE_MS;
  });
}

// The composer's model picker (registry chat ids + labels, `default` = the Chat group's active
// model). The Settings → Models panel can change that default, so it's not pinned forever.
export function useChatModels() {
  return useQuery({
    queryKey: ['chat', 'models'],
    queryFn: () => api.chatModels(),
    staleTime: 60_000,
  });
}

export function useChatSessions() {
  return useQuery({
    queryKey: CHAT_SESSIONS_KEY,
    queryFn: () => api.listChatSessions(),
    refetchInterval: (query) => (awaitingTitle(query.state.data) ? TITLE_POLL_MS : false),
  });
}

export function useChatSession(sessionId: string | null) {
  return useQuery({
    queryKey: ['chat', 'session', sessionId],
    queryFn: () => api.getChatSession(sessionId!),
    enabled: sessionId != null,
  });
}

export function useSendChat() {
  const qc = useQueryClient();
  return useMutation<ChatResponse, Error, ChatRequest>({
    mutationFn: (body: ChatRequest) => api.chat(body),
    // A new session may have been created, and after the first exchange a title starts generating —
    // refresh the list so a new thread appears and its title lands (the poll above catches the flip).
    onSuccess: () => qc.invalidateQueries({ queryKey: CHAT_SESSIONS_KEY }),
  });
}

// "Remember now" (M6, ADR-048 §6): distill the active session on demand. The endorse path organizes
// in the background, so its resulting nodes only surface in the auto-recorded list a moment later —
// refresh it on success so a just-endorsed memory shows up (and stays removable).
export function useRememberSession() {
  const qc = useQueryClient();
  return useMutation<RememberResponse, Error, string>({
    mutationFn: (sessionId: string) => api.rememberSession(sessionId),
    onSuccess: () => qc.invalidateQueries({ queryKey: AUTO_RECORDED_KEY }),
  });
}

// The chat-scoped "recently auto-recorded" audit list (ADR-048 §12) — nightly-endorsed memories,
// newest-first, each removable in one tap.
export function useAutoRecorded() {
  return useQuery<AutoRecordedItem[]>({
    queryKey: AUTO_RECORDED_KEY,
    queryFn: () => api.listAutoRecorded(),
  });
}

// One-tap remove (ADR-048 §11) — git-rm + DB-delete + capture tombstone (soft-delete, replay-
// excluded). Optimistically drop the row so the removal feels instant, then reconcile with the server.
export function useRemoveAutoRecorded() {
  const qc = useQueryClient();
  return useMutation<void, Error, string, { previous: AutoRecordedItem[] | undefined }>({
    mutationFn: (captureId: string) => api.removeAutoRecorded(captureId),
    onMutate: async (captureId) => {
      await qc.cancelQueries({ queryKey: AUTO_RECORDED_KEY });
      const previous = qc.getQueryData<AutoRecordedItem[]>(AUTO_RECORDED_KEY);
      qc.setQueryData<AutoRecordedItem[]>(AUTO_RECORDED_KEY, (old) =>
        (old ?? []).filter((i) => i.capture_id !== captureId),
      );
      return { previous };
    },
    onError: (_err, _id, ctx) => {
      if (ctx?.previous) qc.setQueryData(AUTO_RECORDED_KEY, ctx.previous);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: AUTO_RECORDED_KEY });
      qc.invalidateQueries({ queryKey: ['node'] });
    },
  });
}
