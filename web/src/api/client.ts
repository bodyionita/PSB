// The one place that knows server URLs (ADR-006). All requests send the session cookie
// (credentials: 'include'); non-2xx becomes a typed ApiError carrying the server `detail`.
import { API_BASE } from '../config';
import type {
  AgentRunResponse,
  BackupResponse,
  CaptureAcceptedResponse,
  CaptureView,
  ChatModelsResponse,
  ChatRequest,
  ChatResponse,
  ChatSessionDetail,
  ChatSessionItem,
  GroupRoutingModel,
  HealthResponse,
  LoginResponse,
  MeResponse,
  ModelRoutingUpdate,
  NodeDetailResponse,
  PlanesResponse,
  ProvidersResponse,
  ReviewChoice,
  ReviewItemResponse,
  ReviewVerdict,
  RunAcceptedResponse,
  SearchResultItem,
  SettingsResponse,
  TagConsolidateProposeResponse,
  TagMergeItem,
  TypesResponse,
} from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // FormData sets its own multipart Content-Type (with boundary) — never override it.
  const isForm = init?.body instanceof FormData;
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // non-JSON error body — keep statusText
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  me: () => request<MeResponse>('/auth/me'),
  health: () => request<HealthResponse>('/health'),
  login: (password: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),

  // --- Capture (03-api.md §Capture) ---
  captureText: (text: string, createdAt?: string) =>
    request<CaptureAcceptedResponse>('/capture/text', {
      method: 'POST',
      body: JSON.stringify(createdAt ? { text, created_at: createdAt } : { text }),
    }),
  captureVoice: (blob: Blob, filename: string) => {
    const form = new FormData();
    form.append('file', blob, filename);
    return request<CaptureAcceptedResponse>('/capture/voice', { method: 'POST', body: form });
  },
  listCaptures: (limit = 20) => request<CaptureView[]>(`/captures?limit=${limit}`),
  retryCapture: (id: string) =>
    request<CaptureAcceptedResponse>(`/captures/${encodeURIComponent(id)}/retry`, {
      method: 'POST',
    }),
  submitFollowUp: (id: string, answer: string) =>
    request<CaptureAcceptedResponse>(`/captures/${encodeURIComponent(id)}/follow-up`, {
      method: 'POST',
      body: JSON.stringify({ answer }),
    }),

  // --- Meta / Search & graph (03-api.md §Meta, §Search & graph) ---
  planes: () => request<PlanesResponse>('/planes'),
  types: () => request<TypesResponse>('/types'),
  search: (query: string, planes?: string[], types?: string[], topK?: number) =>
    request<SearchResultItem[]>('/search', {
      method: 'POST',
      body: JSON.stringify({
        query,
        ...(planes && planes.length ? { planes } : {}),
        ...(types && types.length ? { types } : {}),
        ...(topK != null ? { top_k: topK } : {}),
      }),
    }),
  getNode: (id: string) =>
    request<NodeDetailResponse>(`/nodes/${encodeURIComponent(id)}`),

  // --- Chat (03-api.md §Chat, M4 / ADR-025) ---
  chat: (body: ChatRequest) =>
    request<ChatResponse>('/chat', { method: 'POST', body: JSON.stringify(body) }),
  chatModels: () => request<ChatModelsResponse>('/chat/models'),
  listChatSessions: () => request<ChatSessionItem[]>('/chat/sessions'),
  getChatSession: (id: string) =>
    request<ChatSessionDetail>(`/chat/sessions/${encodeURIComponent(id)}`),

  // --- Review queue (03-api.md §Review queue, M3) ---
  listReview: (status = 'pending', kind?: string) =>
    request<ReviewItemResponse[]>(
      `/review?status=${encodeURIComponent(status)}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`,
    ),
  resolveReview: (id: string, body: { choice?: ReviewChoice; verdict?: ReviewVerdict }) =>
    request<ReviewItemResponse>(`/review/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // --- Settings → Models (03-api.md §Settings, M4 / ADR-025 / ADR-043) ---
  getSettings: () => request<SettingsResponse>('/settings'),
  saveModels: (update: ModelRoutingUpdate) =>
    request<GroupRoutingModel>('/settings/models', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),

  // --- Settings → Vocabulary (03-api.md §Settings, M3 / ADR-027) ---
  resolveVocabulary: (reviewId: string, verdict: ReviewVerdict) =>
    request<ReviewItemResponse>('/settings/vocabulary', {
      method: 'PUT',
      body: JSON.stringify({ review_id: reviewId, verdict }),
    }),

  // --- Activity (03-api.md §Activity feed — run-status poll for the Admin tab) ---
  getRun: (id: string) =>
    request<AgentRunResponse>(`/activity/runs/${encodeURIComponent(id)}`),

  // --- Admin (03-api.md §Agents & admin) ---
  providers: () => request<ProvidersResponse>('/admin/providers'),
  reindex: () => request<RunAcceptedResponse>('/admin/reindex', { method: 'POST' }),
  backup: () => request<BackupResponse>('/admin/backup', { method: 'POST' }),
  proposeTags: () =>
    request<TagConsolidateProposeResponse>('/admin/tags/consolidate', {
      method: 'POST',
      body: JSON.stringify({ apply: false }),
    }),
  applyTags: (plan: TagMergeItem[]) =>
    request<RunAcceptedResponse>('/admin/tags/consolidate', {
      method: 'POST',
      body: JSON.stringify({ apply: true, plan }),
    }),
};
