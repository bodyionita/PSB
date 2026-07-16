// Wire types for the HTTP contract (03-api.md). Hand-kept in this one module; may later be
// generated from the server's OpenAPI. Nothing else in web imports server code (ADR-006).

export interface MeResponse {
  authenticated: boolean;
  session_created_at: string | null;
}

export interface LoginResponse {
  authenticated: boolean;
}

export interface HealthResponse {
  status: 'ok' | 'degraded';
  db: boolean;
  store: boolean;
  git_remote: boolean;
  backups: boolean;
}

// --- Capture (03-api.md §Capture, M1 / ADR-019) ---
export type CaptureKind = 'text' | 'voice';

// Pipeline lifecycle (02-data-model §3). Terminal = indexed | failed; everything else is
// in-flight and drives the strip's polling.
export type CaptureStatus =
  | 'received'
  | 'transcribing'
  | 'organizing'
  | 'written'
  | 'indexed'
  | 'failed';

export interface CaptureView {
  capture_id: string;
  kind: CaptureKind;
  status: CaptureStatus;
  raw_text: string | null;
  node_paths: string[];
  follow_up_question: string | null;
  follow_up_answer: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// 202 body shared by text/voice/retry/follow-up. The real status is polled via GET /captures.
export interface CaptureAcceptedResponse {
  capture_id: string;
  status: string;
}

// --- Meta (03-api.md §Meta, M2) ---
// The configured plane vocabulary for the Search-tab filter chips. `planes` = the PLANES=
// config list; `inbox` is the always-present system plane (not part of PLANES). ADR-005/006.
export interface PlanesResponse {
  planes: string[];
  inbox: string;
}

// --- Search & graph (03-api.md §Search & graph, M3 / ADR-026/030) ---
export interface SearchRequest {
  query: string;
  top_k?: number;
  // Filters on `nodes.planes` membership (array overlap, not folder — ADR-005). Omit/[] = no filter.
  planes?: string[];
  // Filters on `nodes.type` (M3). Omit/[] = no filter.
  types?: string[];
}

// One node-grouped hit; the best-matching chunk is the snippet, ranked by score. `type` is the
// node's type (memory/person/idea/… — the search card's type icon).
export interface SearchResultItem {
  node_id: string;
  store_path: string;
  type: string;
  title: string | null;
  plane: string | null;
  planes: string[];
  tags: string[];
  snippet: string;
  score: number;
}

// One edge of a node (GET /nodes/{id}): the *other* endpoint + edge metadata. `dir` = out (this
// node → other) | in; `origin` = canonical (typed, labelled by `rel`) | derived (similarity);
// `score` = confidence (canonical) or cosine (derived).
export interface NodeEdgeItem {
  rel: string;
  dir: 'out' | 'in';
  node_id: string;
  type: string | null;
  title: string | null;
  origin: 'canonical' | 'derived';
  score: number | null;
  since: string | null;
  until: string | null;
}

// Read-only node detail (GET /nodes/{id}) — body read live from the graph store, plus the derived
// entity `profile` (null for content nodes) and canonical + derived edges (both directions).
export interface NodeDetailResponse {
  node_id: string;
  store_path: string;
  type: string;
  title: string | null;
  plane: string | null;
  planes: string[];
  tags: string[];
  aliases: string[];
  disambig: string | null;
  occurred: string | null;
  occurred_end: string | null;
  body: string;
  profile: string | null;
  edges: NodeEdgeItem[];
}

// --- Chat (03-api.md §Chat, M4 / ADR-025 / ADR-043) ---
// One cited node backing a chat answer — cited-only, renumbered `[1..m]` to match the answer's
// inline markers. Shape mirrors the persisted source jsonb (GET /chat/sessions/{id}) and the live
// POST /chat response. No `plane` singular field — planes[] carries membership (like search).
export interface ChatSourceItem {
  node_id: string;
  store_path: string;
  type: string;
  title: string | null;
  snippet: string;
  score: number;
  planes: string[];
}

// POST /chat request. `session_id` omitted ⇒ implicit session creation; `model` = the composer's
// per-conversation picker override of the Chat group's active model; `planes` scopes retrieval.
export interface ChatRequest {
  message: string;
  session_id?: string;
  model?: string;
  planes?: string[];
  top_k?: number;
}

// POST /chat response. `sources` is empty for general-knowledge / "not in your memories" answers;
// `fallback_used` flags that a non-primary model answered (ADR-025 transparency banner).
export interface ChatResponse {
  session_id: string;
  answer: string;
  model_used: string;
  fallback_used: boolean;
  // Reasoning effort applied to the answering model (null for effort-less models like Nebius);
  // shown in the per-message "answered by … · effort" caption on a fresh turn (not persisted).
  effort_used: string | null;
  sources: ChatSourceItem[];
}

// GET /chat/models — the composer's picker: registry chat ids + friendly labels, `default` = the
// Chat group's active model. `effort` = the effort the Chat group applies to the model (null for
// effort-less models or one with no configured Chat-group effort), shown as "label · effort".
export interface ChatModelItem {
  id: string;
  label: string;
  effort: string | null;
}
export interface ChatModelsResponse {
  models: ChatModelItem[];
  default: string;
}

// GET /chat/sessions — one thread in the list (newest-first). `title` is null until the best-effort
// `quick`-tier titling lands after the first exchange (ADR-043); the list falls back to the first
// message meanwhile.
export interface ChatSessionItem {
  id: string;
  title: string | null;
  created_at: string | null;
  last_model: string | null;
}

// One persisted turn (GET /chat/sessions/{id}). `sources` carries cited nodes for assistant turns
// (empty otherwise); `model` is the answering model (no persisted `fallback_used` — the banner is a
// live-response transparency signal only).
export interface ChatMessageItem {
  role: 'user' | 'assistant';
  content: string;
  model: string | null;
  sources: ChatSourceItem[];
  created_at: string | null;
}

// GET /chat/sessions/{id} — a session with its full message history.
export interface ChatSessionDetail {
  id: string;
  title: string | null;
  messages: ChatMessageItem[];
}

// POST /chat/sessions/{id}/remember (M6, ADR-048 §6) — the on-demand distill result. Either the pass
// ran (`endorsed`/`to_review` counts, `skipped` null) or it was a no-op (`skipped` = the reason, e.g.
// "nothing new past the watermark", counts null). Endorsed captures organize in the background.
export interface RememberResponse {
  endorsed: number | null;
  to_review: number | null;
  skipped: string | null;
}

// One auto-endorsed chat memory in the chat-scoped "recently auto-recorded" audit list
// (GET /chat/auto-recorded, ADR-048 §12). `node_paths` is empty + `title` null until the background
// organize lands; `snippet` previews the endorsed statement; `source_ref` is the originating
// chat-session id; `salience` is the distiller's coarse triage tag. Feeds the one-tap-remove surface.
export interface AutoRecordedItem {
  capture_id: string;
  node_paths: string[];
  title: string | null;
  snippet: string;
  salience: Salience | null;
  source_ref: string | null;
  created_at: string | null;
}

// --- Settings → Models (03-api.md §Settings, M4 / ADR-025 / ADR-043 / ADR-045) ---
// One pickable chat model for a routing group's dropdowns. `id` is the MODEL id (the raw vendor
// string, e.g. `claude-opus-4-8`) and `provider` is the id of the provider that serves it (derived
// — ADR-045 §1; the routable unit is the model, the provider is an attribute). `effort_levels` is
// empty unless `supports_effort` — the effort selector renders only where it applies, from these
// registry-sourced levels (no hardcoded enums, ADR-025 §6).
export interface RoutingModelItem {
  id: string;
  provider: string;
  label: string;
  supports_effort: boolean;
  effort_levels: string[];
}

// One routing group's editable state (GET /settings): the effective active/fallback + per-model
// effort (saved-over-seed) and the models the dropdowns choose from. `active`/`fallback` and every
// `effort_by_model` key are MODEL ids (ADR-045; carries an entry only for the effort-supporting
// models in {active, fallback}).
export interface GroupRoutingModel {
  group: string;
  active: string;
  fallback: string;
  effort_by_model: Record<string, string>;
  models: RoutingModelItem[];
}

// GET /settings — model routing for all 3 groups (chat/conspect/quick), in fixed group order.
export interface SettingsResponse {
  groups: GroupRoutingModel[];
}

// PUT /settings/models — save one group's routing. `active`/`fallback`/`effort_by_model` keys are
// model ids (ADR-045; was `effort_by_provider`/provider ids). `fallback` "" = none; `effort_by_model`
// must carry a valid level for each effort-supporting model in {active, fallback} (422 otherwise).
export interface ModelRoutingUpdate {
  group: 'chat' | 'conspect' | 'quick';
  active: string;
  fallback: string;
  effort_by_model: Record<string, string>;
}

// --- Types / vocabulary (03-api.md §Search & graph, §Settings, M3 / ADR-027) ---
// A pending `vocab-proposal` the organizer filed; resolve it by its review-item `id`.
export interface VocabProposalItem {
  id: string;
  vocab: string | null; // axis: node_type | entity_type | edge_rel
  value: string | null;
  excerpt: string | null;
  created_at: string;
}

// GET /types — effective vocabulary (config seeds ∪ approved additions) + still-pending proposals.
export interface TypesResponse {
  node_types: string[];
  edge_rels: string[];
  entity_like_types: string[];
  proposals: VocabProposalItem[];
}

// --- Review queue (03-api.md §Review queue, M3 / ADR-030 §3; M6 kinds ADR-048/049) ---
export type ReviewVerdict = 'approve' | 'reject';
// entity-ambiguity: a candidate node id | "new" | "maybe". vocab-proposal: approve | reject.
export type ReviewChoice = string;
// stance-candidate (ADR-048 §7): agree ingests through the organizer, disagree discards, maybe parks.
export type StanceVerdict = 'agree' | 'disagree' | 'maybe';
// dedup-proposal (ADR-049 §6): merge folds the loser into the survivor, keep dismisses, link writes
// a canonical `similar` edge. A batch merge uses the payload's default survivor.
export type DedupAction = 'merge' | 'keep' | 'link';
// The distiller's coarse LLM triage tag (ADR-048 §8) — orders the Review list + ranks feed items.
export type Salience = 'high' | 'med' | 'low';

// One review_queue row. `payload` carries kind-specific data decidable in place —
// entity-ambiguity candidates (`{id,name,disambig,aliases}`) or a proposal's `{vocab,value}`.
export interface ReviewItemResponse {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  excerpt: string | null;
  source: string | null;
  source_ref: string | null;
  status: string;
  resolution: Record<string, unknown> | null;
  created_at: string;
}

// One entity candidate inside an `entity-ambiguity` item's payload.
export interface EntityCandidate {
  id: string;
  name: string | null;
  disambig: string | null;
  aliases: string[];
}

// POST /review/{id} resolution body (ADR-048/049) — the meaningful field is per-kind:
// entity-ambiguity `choice`, stance-candidate/vocab `verdict`, dedup-proposal `action`(+`survivor`).
// The server reads only the field that fits the item's kind (400 otherwise).
export interface ReviewResolveBody {
  choice?: ReviewChoice;
  verdict?: ReviewVerdict | StanceVerdict;
  action?: DedupAction;
  survivor?: string;
}

// One item's outcome in a batch resolve (POST /review/batch, ADR-048 §8): best-effort per item — an
// action that doesn't fit an item's kind fails just that item (`ok=false` + reason), never the batch.
export interface ReviewBatchResultItem {
  id: string;
  ok: boolean;
  error: string | null;
}

// POST /review/batch response — one result per requested id, in request order.
export interface ReviewBatchResponse {
  results: ReviewBatchResultItem[];
}

// --- Activity (03-api.md §Activity feed, M2 pull-forward) ---
export type RunStatus = 'running' | 'succeeded' | 'failed' | 'skipped';

// One agent_runs row (GET /activity/runs/{id}) — the Admin tab polls this for live run status +
// `details` counts. `details` is an opaque JSON blob whose shape depends on the agent.
export interface AgentRunResponse {
  id: string;
  agent: string;
  status: RunStatus;
  started_at: string | null;
  finished_at: string | null;
  model_used: string | null;
  fallback_used: boolean;
  summary: string | null;
  details: Record<string, unknown>;
  error: string | null;
}

// --- Admin (03-api.md §Agents & admin, M2 / ADR-023/024) ---
// 202 bodies carrying the background run's agent_runs id (reindex + tags-apply).
export interface RunAcceptedResponse {
  run_id: string;
}

// POST /admin/backup result.
export interface BackupResponse {
  committed: boolean;
  pushed: boolean;
}

// --- Provider observability (03-api.md §Admin, GET /admin/providers — M4 follow-up / ADR-044) ---
export type ProviderCapability = 'chat' | 'stt' | 'embedding';

// The last runtime failure for a provider — sticky (a later success does not clear it).
export interface ProviderError {
  message: string;
  at: string;
}

// One provider row. `reachable` is a live `health()` probe — config-reachability, NOT a success
// guarantee; the runtime truth is `last_error` (sticky) + `last_success_at` + `consecutive_failures`.
export interface ProviderStatusItem {
  id: string;
  label: string;
  capabilities: ProviderCapability[];
  reachable: boolean;
  last_error: ProviderError | null;
  last_success_at: string | null;
  consecutive_failures: number;
}

export interface ProvidersResponse {
  providers: ProviderStatusItem[];
}

// One tag-merge group: fold `variants` into `canonical` (ADR-024). Shared by propose + apply.
export interface TagMergeItem {
  canonical: string;
  variants: string[];
}

// Propose result — a correlation id + the merges to review (no writes yet).
export interface TagConsolidateProposeResponse {
  plan_id: string;
  merges: TagMergeItem[];
}
