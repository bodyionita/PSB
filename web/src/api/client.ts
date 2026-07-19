// The one place that knows server URLs (ADR-006). All requests send the session cookie
// (credentials: 'include'); non-2xx becomes a typed ApiError carrying the server `detail`.
import { API_BASE } from '../config';
import type {
  ActivityCategory,
  ActivityFeedResponse,
  AgentRosterItem,
  AgentRunResponse,
  AutoRecordedItem,
  BackupResponse,
  EdgeRetypeItem,
  EntityMergeProposeResponse,
  CaptureAcceptedResponse,
  CaptureView,
  DraftPart,
  DraftView,
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
  NeighborPageResponse,
  NeighborZonesResponse,
  NodeDateTokenEdit,
  NodeDateTokenEditResponse,
  NodeDetailResponse,
  PipelineItem,
  PlanesResponse,
  ProvidersResponse,
  RememberResponse,
  ReprocessPreview,
  ReviewBatchResponse,
  ReviewItemResponse,
  ReviewResolveBody,
  ReviewVerdict,
  RunAcceptedResponse,
  RunLogsResponse,
  SearchResultItem,
  SettingsResponse,
  TagConsolidateProposeResponse,
  TagMergeItem,
  TypesResponse,
  VocabConsolidateProposeResponse,
} from './types';

// The absolute URL of a stored media file (M9 T5, ADR-060 §7). Same-origin under Caddy, so a plain
// `<img src>`/`<audio src>` sends the session cookie automatically — `GET /media/{id}` is
// session-gated and streams Range/206 for voice scrubbing. The one place that knows this URL shape.
export const mediaUrl = (id: string) => `${API_BASE}/media/${encodeURIComponent(id)}`;

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

  // --- Composite capture draft (03-api.md §Capture, M9.6 / ADR-061 §3) ---
  // Every web capture goes through the draft flow (the one-shot text/voice/image endpoints are gone,
  // ADR-061 §8). Open resumes the one active draft; a part attaches raw immediately (derivation runs
  // at submit); submit spawns the blended organize.
  openDraft: () => request<DraftView>('/capture/draft', { method: 'POST' }),
  addDraftPart: (id: string, blob: Blob, filename: string, kind: 'photo' | 'voice') => {
    const form = new FormData();
    form.append('kind', kind);
    form.append('file', blob, filename);
    return request<DraftPart>(`/capture/${encodeURIComponent(id)}/part`, {
      method: 'POST',
      body: form,
    });
  },
  removeDraftPart: (id: string, mediaId: string) =>
    request<void>(
      `/capture/${encodeURIComponent(id)}/part/${encodeURIComponent(mediaId)}`,
      { method: 'DELETE' },
    ),
  editDraftText: (id: string, text: string) =>
    request<DraftView>(`/capture/${encodeURIComponent(id)}/text`, {
      method: 'PUT',
      body: JSON.stringify({ text }),
    }),
  submitDraft: (id: string) =>
    request<CaptureAcceptedResponse>(`/capture/${encodeURIComponent(id)}/submit`, {
      method: 'POST',
    }),
  discardDraft: (id: string) =>
    request<void>(`/capture/${encodeURIComponent(id)}/draft`, { method: 'DELETE' }),

  // --- Capture read (03-api.md §Capture) ---
  listCaptures: (limit = 20) => request<CaptureView[]>(`/captures?limit=${limit}`),
  // Full pipeline state for one capture (03-api.md) — the Activity Captures-tab row expand + any
  // in-place detail fetch (M8.1, ADR-054 §4): raw text, node_refs, status, source badge.
  getCapture: (id: string) => request<CaptureView>(`/captures/${encodeURIComponent(id)}`),
  // General capture remove (ADR-062 §R): entirely delete a submitted capture — nodes (hubs
  // preserved), media, tombstone. 204; 409 = open draft (discard it instead); 404 = unknown/removed.
  removeCapture: (id: string) =>
    request<void>(`/captures/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  retryCapture: (id: string) =>
    request<CaptureAcceptedResponse>(`/captures/${encodeURIComponent(id)}/retry`, {
      method: 'POST',
    }),
  submitFollowUp: (id: string, answer: string) =>
    request<CaptureAcceptedResponse>(`/captures/${encodeURIComponent(id)}/follow-up`, {
      method: 'POST',
      body: JSON.stringify({ answer }),
    }),
  // The anchor edit (M8.2, ADR-056 §5): correct a capture's recorded-at, then re-resolve its
  // relative dates against the new anchor in the background (one-capture reorganize). 202; the
  // `anchor` is an ISO-8601 datetime. 404 = unknown capture.
  editCaptureAnchor: (id: string, anchor: string) =>
    request<CaptureAcceptedResponse>(`/captures/${encodeURIComponent(id)}/anchor`, {
      method: 'PUT',
      body: JSON.stringify({ anchor }),
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
  // The mechanical date-token edit (M8.2, ADR-056 §5): rewrite an exact body `[[t:…]]` token to a
  // new date; when it's the node's event date, `occurred` moves too and chunks re-embed. No LLM,
  // instant. 400 = a bad token/date or a token not in the body; 404 = unknown/merged node.
  editNodeDateToken: (id: string, body: NodeDateTokenEdit) =>
    request<NodeDateTokenEditResponse>(`/nodes/${encodeURIComponent(id)}/date-token`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  // --- Map / neighbors (03-api.md §Search & graph, M7 / ADR-051 + ADR-052) ---
  // No `rel` → the grouped first page (one zone per rel, each capped + total + next_cursor). With
  // `rel` (+ optional cursor) → that single zone's next flat "show more" page.
  nodeNeighbors: (id: string) =>
    request<NeighborZonesResponse>(`/nodes/${encodeURIComponent(id)}/neighbors`),
  nodeNeighborPage: (id: string, rel: string, cursor?: string | null) =>
    request<NeighborPageResponse>(
      `/nodes/${encodeURIComponent(id)}/neighbors?rel=${encodeURIComponent(rel)}` +
        (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''),
    ),

  // --- Chat (03-api.md §Chat, M4 / ADR-025) ---
  chat: (body: ChatRequest) =>
    request<ChatResponse>('/chat', { method: 'POST', body: JSON.stringify(body) }),
  chatModels: () => request<ChatModelsResponse>('/chat/models'),
  listChatSessions: () => request<ChatSessionItem[]>('/chat/sessions'),
  getChatSession: (id: string) =>
    request<ChatSessionDetail>(`/chat/sessions/${encodeURIComponent(id)}`),
  // "Remember now" (M6, ADR-048 §6): distill this session on demand — same salience + stance gate,
  // advancing the same watermark. Endorsed captures organize in the background.
  rememberSession: (id: string) =>
    request<RememberResponse>(`/chat/sessions/${encodeURIComponent(id)}/remember`, {
      method: 'POST',
    }),
  // The chat-scoped "recently auto-recorded" audit list (M6, ADR-048 §12) + its one-tap remove
  // (204 on success; git-rm + DB-delete + capture tombstone, soft-delete).
  listAutoRecorded: (limit = 50) =>
    request<AutoRecordedItem[]>(`/chat/auto-recorded?limit=${limit}`),
  removeAutoRecorded: (captureId: string) =>
    request<void>(`/chat/auto-recorded/${encodeURIComponent(captureId)}/remove`, {
      method: 'POST',
    }),

  // --- Review queue (03-api.md §Review queue, M3; M6 kinds ADR-048/049) ---
  listReview: (status = 'pending', kind?: string) =>
    request<ReviewItemResponse[]>(
      `/review?status=${encodeURIComponent(status)}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`,
    ),
  // One review item by id, any status (M8.1 follow-up) — the Activity "Reviewed" row's expand detail.
  getReview: (id: string) => request<ReviewItemResponse>(`/review/${encodeURIComponent(id)}`),
  resolveReview: (id: string, body: ReviewResolveBody) =>
    request<ReviewItemResponse>(`/review/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // Batch resolve (ADR-048 §8): one `action` string applied to many items, best-effort per item.
  resolveReviewBatch: (ids: string[], action: string) =>
    request<ReviewBatchResponse>('/review/batch', {
      method: 'POST',
      body: JSON.stringify({ ids, action }),
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

  // --- Activity feed + ops console (03-api.md §Activity & ops, M8 / ADR-053) ---
  // The merged categorized feed — one keyset page. `category` narrows to one tab (all when omitted);
  // `before` is the opaque `next_before` cursor from the prior page.
  activityFeed: (category?: ActivityCategory, before?: string | null, limit = 50) =>
    request<ActivityFeedResponse>(
      `/activity?limit=${limit}` +
        (category ? `&category=${encodeURIComponent(category)}` : '') +
        (before ? `&before=${encodeURIComponent(before)}` : ''),
    ),
  getRun: (id: string) =>
    request<AgentRunResponse>(`/activity/runs/${encodeURIComponent(id)}`),
  // The live log tail — lines with seq > afterSeq + a `running` flag. Poll while running (and keep
  // paging past the tail cap until an empty page — ADR-053 §2).
  getRunLogs: (id: string, afterSeq = 0) =>
    request<RunLogsResponse>(
      `/activity/runs/${encodeURIComponent(id)}/logs?after_seq=${afterSeq}`,
    ),
  // Ops roster + pipelines (the live scheduler view).
  listAgents: () => request<AgentRosterItem[]>('/agents'),
  listPipelines: () => request<PipelineItem[]>('/pipelines'),
  // Manual triggers (single-flight via the JobRunner): 202 on accept, 409 already-running, 404
  // unknown, 503 scheduler-off. The run id is discovered via the roster's last_run.run_id.
  runAgent: (name: string) =>
    request<{ agent: string }>(`/agents/${encodeURIComponent(name)}/run`, { method: 'POST' }),
  runPipeline: (name: string) =>
    request<{ pipeline: string }>(`/pipelines/${encodeURIComponent(name)}/run`, { method: 'POST' }),

  // --- Admin (03-api.md §Admin) ---
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
  // reprocess-all-from-raw (ADR-042 / vision P10). Confirm-gated: preview = dry-run counts (no
  // writes); confirm = the destructive replay, returning the background run id.
  reprocessPreview: () =>
    request<ReprocessPreview>('/admin/reprocess', {
      method: 'POST',
      body: JSON.stringify({ confirm: false }),
    }),
  reprocessConfirm: () =>
    request<RunAcceptedResponse>('/admin/reprocess', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    }),
  // entities/merge (ADR-030 §5): two-step propose (inbound-edge inventory, no writes) → apply.
  mergeEntitiesPropose: (loser: string, survivor: string) =>
    request<EntityMergeProposeResponse>('/admin/entities/merge', {
      method: 'POST',
      body: JSON.stringify({ loser, survivor, apply: false }),
    }),
  mergeEntitiesApply: (loser: string, survivor: string) =>
    request<RunAcceptedResponse>('/admin/entities/merge', {
      method: 'POST',
      body: JSON.stringify({ loser, survivor, apply: true }),
    }),
  // vocab/consolidate (ADR-036): two-step edge retro-consolidation for an approved rel.
  consolidateVocabPropose: (rel: string) =>
    request<VocabConsolidateProposeResponse>('/admin/vocab/consolidate', {
      method: 'POST',
      body: JSON.stringify({ rel, apply: false }),
    }),
  consolidateVocabApply: (rel: string, plan: EdgeRetypeItem[]) =>
    request<RunAcceptedResponse>('/admin/vocab/consolidate', {
      method: 'POST',
      body: JSON.stringify({ rel, apply: true, plan }),
    }),
};
