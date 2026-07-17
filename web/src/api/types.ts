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

// --- Map / neighbors (03-api.md §Search & graph, M7 / ADR-051 + ADR-052) ---
// One 1-hop neighbor in a map zone or "show more" page. Carries origin/dir/score/since/until plus
// the endpoint's type/title/plane so the canvas renders arrowheads, faint-derived + dashed-superseded
// (until) edges and the node mark (emoji=type, colour=plane) without a second fetch.
export interface MapNeighborItem {
  origin: 'canonical' | 'derived';
  rel: string;
  dir: 'out' | 'in';
  node_id: string;
  type: string | null;
  title: string | null;
  plane: string | null;
  score: number | null;
  since: string | null;
  until: string | null;
}

// One `rel` zone of a center's neighborhood, capped at `map_zone_fanout` (ADR-052: keyed by `rel` —
// the sole dual-origin rel `similar` collapses to one zone; each neighbor's own `origin` carries the
// solid/faint styling, so there is no zone-level origin). `total` drives "show N more of M";
// `next_cursor` pages the single-zone "show more" (null when the zone fit).
export interface MapZone {
  rel: string;
  neighbors: MapNeighborItem[];
  total: number;
  next_cursor: string | null;
}

// The focal node's render header echoed by the grouped neighbors response.
export interface NeighborCenter {
  node_id: string;
  type: string;
  title: string | null;
  plane: string | null;
  planes: string[];
}

// Grouped first page of GET /nodes/{id}/neighbors (no `rel`). `center` is null + `zones` empty when
// the node is unknown (empty neighborhood).
export interface NeighborZonesResponse {
  center: NeighborCenter | null;
  zones: MapZone[];
}

// A single zone's flat "show more" page — GET /nodes/{id}/neighbors?rel=… — thin over the M5
// rel-filtered keyset. `next_cursor` is null at the zone's end.
export interface NeighborPageResponse {
  center_id: string;
  rel: string;
  direction: string;
  neighbors: MapNeighborItem[];
  next_cursor: string | null;
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

// One agent_runs row (GET /activity/runs/{id}) — the ops console polls this for live run status +
// `details` counts. `details` is an opaque JSON blob whose shape depends on the agent. `trigger`
// (M8, ADR-053 §5) is the run's origin (scheduled | manual).
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
  trigger: string;
}

// --- Activity feed + ops console (03-api.md §Activity & ops, M8 / ADR-053) ---
// The three feed categories, by *origin* (a hand-run job → manual_actions via agent_runs.trigger).
export type ActivityCategory = 'agents_jobs' | 'conversations' | 'manual_actions';

// One normalized row of the merged GET /activity feed. `kind` discriminates the source entity
// (agent_run | chat_capture | review_verdict); the specific agent name / review kind rides in
// `title`. `ref` is the drill-down target (a run id → GET /activity/runs/{id}, a chat-session id, a
// review id); `parent_ref` links a pipeline step child to its parent run (null otherwise). For a
// `chat_capture` row `id` is the capture id — the key the Conversations one-tap remove targets.
export interface ActivityFeedItem {
  id: string;
  category: ActivityCategory;
  kind: string;
  ts: string;
  title: string | null;
  snippet: string | null;
  ref: string | null;
  parent_ref: string | null;
}

// One keyset page (GET /activity). `next_before` is the opaque cursor to pass back as `before=` for
// the next (older) page; null at the end of the feed.
export interface ActivityFeedResponse {
  items: ActivityFeedItem[];
  next_before: string | null;
}

// One captured live-log line (GET /activity/runs/{id}/logs). `seq` is the per-run cursor key.
export interface RunLogLine {
  seq: number;
  ts: string | null;
  level: string;
  message: string;
}

// The live log tail (poll, not stream). Lines with `seq > after_seq` in order + a `running` flag.
// `next_after_seq` is the max `seq` returned — pass it back as `after_seq` next poll (unchanged when
// no new lines). Client must keep paging until an empty page even after running flips false (the
// on-finish flush is async + one page is capped — ADR-053 §1/§2).
export interface RunLogsResponse {
  run_id: string;
  running: boolean;
  logs: RunLogLine[];
  next_after_seq: number;
}

// The most recent run for a roster/pipeline entry (null when it has never run).
export interface LastRun {
  status: RunStatus;
  finished_at: string | null;
  run_id: string;
}

// One flat-roster row (GET /agents). A job's schedule is *derived* from its 0..N pipeline
// memberships (many-to-many); `running` is live single-flight status from the JobRunner.
export interface AgentRosterItem {
  name: string;
  category: string;
  pipelines: string[];
  running: boolean;
  last_run: LastRun | null;
}

// One pipeline (GET /pipelines): cadence, live next-run, ordered step names, last-run. A pipeline
// run is a parent agent_runs row, each step a child (parent_run_id).
export interface PipelineItem {
  name: string;
  cron: string;
  next_run: string | null;
  steps: string[];
  last_run: LastRun | null;
}

// POST /admin/reprocess preview ({confirm:false}) — the reusable reprocess-all-from-raw op's dry-run
// counts (no writes); the confirm ({confirm:true}) returns a RunAcceptedResponse (ADR-042 / P10).
export interface ReprocessPreview {
  captures: number;
  nodes: number;
  merges: number;
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

// --- Entity merge (03-api.md §Admin, POST /admin/entities/merge — ADR-030 §5) ---
// A merge side's identity + alias set. `inbound` lists the edges that point at the loser (retargeted
// onto the survivor on apply). Two-step: propose (apply:false) → inventory; apply (apply:true) → run.
export interface MergeSide {
  id: string;
  type: string;
  title: string | null;
  aliases: string[];
}
export interface InboundEdge {
  src_id: string;
  src_store_path: string;
  rel: string;
}
export interface EntityMergeProposeResponse {
  plan_id: string;
  loser: MergeSide;
  survivor: MergeSide;
  inbound_count: number;
  inbound: InboundEdge[];
}

// --- Edge vocab consolidation (03-api.md §Admin, POST /admin/vocab/consolidate — ADR-036) ---
// One edge re-typing: the edge `{rel: from_rel, to}` on node `src_id` becomes `to_rel`. Shared by the
// propose response + the apply request. Two-step: propose (apply:false) → retypings; apply → run.
export interface EdgeRetypeItem {
  src_id: string;
  to: string;
  from_rel: string;
  to_rel: string;
}
export interface VocabConsolidateProposeResponse {
  plan_id: string;
  rel: string;
  retypings: EdgeRetypeItem[];
}
