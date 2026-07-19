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

// --- Media (M9 T4/T5, ADR-057 / ADR-060 §7) ---
// A stored media item's derivation lifecycle: `pending` = still deriving (shimmer tile),
// `derived` = its description/transcript landed, `unavailable` = derivation gave up (explicit
// broken-media tile). The raw file is servable via `GET /media/{id}` at every status.
export type MediaStatus = 'pending' | 'derived' | 'unavailable';

// A media item's kind (ADR-060 §7): the value carried by `node_media` / `media_kinds`. `photo` and
// `voice` are produced now (image/voice captures); `video` is an M9.5 connector kind. Kept a plain
// string on the wire — the renderer treats an unknown kind as a generic attachment.
export type MediaKind = 'photo' | 'voice' | 'video';

// `composite` (M9.6, ADR-061 §2) is the web compose kind: text + 0..N photos + <=1 voice organized
// in one blended pass. `text`/`voice`/`image` remain for legacy captures + internal (mcp/chat/reprocess)
// producers.
export type CaptureKind = 'text' | 'voice' | 'image' | 'composite';

// Pipeline lifecycle (02-data-model §3). Terminal = indexed | failed; everything else is
// in-flight and drives the strip's polling.
export type CaptureStatus =
  | 'received'
  | 'transcribing'
  // Image captures derive their vision description between `received` and `organizing` — the
  // sibling of `transcribing` for the photo leg (M9 T3, ADR-057 §3).
  | 'deriving'
  | 'organizing'
  | 'written'
  | 'indexed'
  | 'failed';

// One of a capture's resulting nodes, id-resolved (M8.1 T4, ADR-054 §5 replan): the server's
// read-time `node_paths -> nodes.id` join. `node_paths` are store *paths*, not identity
// (02-data-model §Identity: "paths are projections"), so a plain path can't open the uuid-keyed
// `NodePreview` — this is what lets a capture's node references render as a clickable `NodeChip`.
// A path with no live node row (not yet indexed, or tombstoned) is simply absent here.
export interface CaptureNodeRef {
  id: string;
  store_path: string;
  type: string | null;
  title: string | null;
}

// The media item backing an image OR voice capture (M9 T3/T4, ADR-057 §6 / ADR-060 §5): the web
// renders the photo / voice player via `GET /media/{id}` and a derivation-status badge, straight off
// the capture. Null for text/mcp/chat captures. `kind` is `photo`/`voice`. Unlike a node's media
// item this carries no `capture_id` — it *is* this capture's media.
export interface CaptureMedia {
  id: string;
  kind: MediaKind;
  status: MediaStatus;
  // Stable 0-based position within the capture (M9.6 T4, ADR-061 §11); null for legacy single-part
  // media. Drives the render order of the media list.
  part_ordinal: number | null;
}

export interface CaptureView {
  capture_id: string;
  kind: CaptureKind;
  status: CaptureStatus;
  raw_text: string | null;
  // The person's typed words on a composite capture (M9.6 T4, ADR-061 §5); null otherwise.
  text_body: string | null;
  node_paths: string[];
  // Id-resolved projection of `node_paths` (M8.1 T4) — see `CaptureNodeRef`. May be shorter than
  // `node_paths` (an unresolved path is simply absent); the client falls back to a plain path pill
  // for those.
  node_refs: CaptureNodeRef[];
  follow_up_question: string | null;
  follow_up_answer: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  // The capture's origin (M8.1, ADR-054 §4): `mcp`/`chat`, or null for a web capture (falls back
  // to `kind` for the source badge).
  source: string | null;
  // The capture's media parts (M9.6 T4, ADR-061 §11 — singular → list): 0..N photos + <=1 voice,
  // ordered by part_ordinal. Empty for text/mcp/chat captures. Each rendered via `GET /media/{id}`.
  media: CaptureMedia[];
  // The capture's most recent processing `agent_runs` id (M9.6 T4, ADR-061 §10) — the Activity-tab
  // deep-link so the user can follow the (per-part) processing. Null until a run starts.
  run_id: string | null;
}

// 202 body shared by submit/retry/follow-up. The real status is polled via GET /captures.
export interface CaptureAcceptedResponse {
  capture_id: string;
  status: string;
}

// --- Composite draft (M9.6 T1, ADR-061 §3) ---
// One media part on an open draft — the compose surface renders its thumbnail/player + an 'x'.
export interface DraftPart {
  id: string;
  kind: MediaKind;
  status: MediaStatus;
  part_ordinal: number | null;
  mime_type: string | null;
}

// The active compose draft (POST /capture/draft): the resume payload — text body + ordinal-ordered
// parts — so the compose screen rebuilds after app-close.
export interface DraftView {
  capture_id: string;
  status: string;
  text_body: string | null;
  parts: DraftPart[];
  created_at: string | null;
}

// --- Meta (03-api.md §Meta, M2) ---
// The configured plane vocabulary for the Search-tab filter chips. `planes` = the PLANES=
// config list; `inbox` is the always-present system plane (not part of PLANES). ADR-005/006.
export interface PlanesResponse {
  planes: string[];
  inbox: string;
}

// The user's inner-voice dimension stamped by the organizer (M8.2 T3.5, ADR-055 §3c): `internal`
// = feelings/reflection/self-talk, `external` = a record of the world, `mixed` = both after
// extraction. `null` on unstamped entity hubs. Drives the Map/NodePreview inner-voice marker
// (`internal` = full marker, `mixed` = subtle, `external`/`null` = none).
export type Interiority = 'internal' | 'external' | 'mixed' | null;

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
  // Distinct media kinds the node carries (M9 T4, ADR-060 §7), off the `node_media` link. Drives a
  // tiny 📷/🎙 glyph on the result card — no thumbnails in lists. Empty when the node has no media.
  media_kinds: MediaKind[];
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

// One media item a node carries (GET /nodes/{id}.media[], M9 T4 / ADR-060 §1). Rendered via
// `GET /media/{id}`: `kind` (photo/voice/video) picks the tile/player, `status` the shimmer/broken
// state, and `capture_id` opens the "see raw capture" detail sheet (ADR-060 §7). A media attachment,
// not a graph edge — it never appears in `edges`.
export interface NodeMediaItem {
  id: string;
  kind: MediaKind;
  status: MediaStatus;
  capture_id: string | null;
}

// Read-only node detail (GET /nodes/{id}) — body read live from the graph store, plus the derived
// entity `profile` (null for content nodes), canonical + derived edges (both directions), and the
// node's attached `media` (M9 T4, ADR-060 §1 — the NodePreview media strip).
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
  // Inner-voice dimension (M8.2 T3.5, ADR-055 §3c) — drives the NodePreview marker.
  interiority: Interiority;
  body: string;
  profile: string | null;
  edges: NodeEdgeItem[];
  media: NodeMediaItem[];
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
  // Inner-voice dimension (M8.2 T3.5, ADR-055 §3c) — drives the map node's inner-voice marker.
  interiority: Interiority;
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
  // Inner-voice dimension of the focal node (M8.2 T3.5, ADR-055 §3c) — so the center is markable too.
  interiority: Interiority;
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

// --- Date-token edit (03-api.md §Search & graph, M8.2 / ADR-056 §5) ---
// PUT /nodes/{id}/date-token — the mechanical token edit. `old` is the EXACT `[[t:…]]` token
// string currently in the body (the edit anchor, no text-span bookkeeping); `start` (+ optional
// `end` for a range) are the new partial-ISO date(s) (`2025` / `2025-07` / `2025-07-07`); `label`
// is an optional absolute display label. When `old` is the node's event date, `occurred` moves too.
export interface NodeDateTokenEdit {
  old: string;
  start: string;
  end?: string | null;
  label?: string | null;
}

// PUT /nodes/{id}/date-token result. `occurred_updated` is true when the edited token was the
// node's event date; `occurred`/`occurred_end` are then the new event date (else null).
export interface NodeDateTokenEditResponse {
  node_id: string;
  occurred_updated: boolean;
  occurred: string | null;
  occurred_end: string | null;
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
  // Distinct media kinds the cited node carries (M9 T4, ADR-060 §7) — the source card's glyph.
  media_kinds: MediaKind[];
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
  group: 'chat' | 'conspect' | 'quick' | 'vision';
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

// POST /review/{id} resolution body (ADR-048/049/056) — the meaningful field is per-kind:
// entity-ambiguity `choice`, stance-candidate/vocab `verdict`, dedup-proposal `action`(+`survivor`),
// occurred-enrichment `answer` (a natural-language date phrase, or `"maybe"` to park / `"skip"` to
// dismiss — ADR-056 §7). The server reads only the field that fits the item's kind (400 otherwise).
export interface ReviewResolveBody {
  choice?: ReviewChoice;
  verdict?: ReviewVerdict | StanceVerdict;
  action?: DedupAction;
  survivor?: string;
  answer?: string;
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

// One node of a run's recursive step tree (GET /activity/runs/{id}, M8.1 ADR-054 §2). A pipeline
// step's own spawned `capture` runs sit one level deeper in `children`; siblings are ordered
// early→late (server-side, no client sort needed).
export interface RunChildItem {
  id: string;
  name: string;
  status: RunStatus;
  ts: string | null;
  summary: string | null;
  children: RunChildItem[];
}

// One agent_runs row (GET /activity/runs/{id}) — the ops console polls this for live run status +
// `details` counts. `details` is an opaque JSON blob whose shape depends on the agent. `trigger`
// (M8, ADR-053 §5) is the run's origin (scheduled | manual). `children` (M8.1, ADR-054 §2) is the
// recursive step subtree — empty for a leaf run.
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
  children: RunChildItem[];
}

// One derived media part inside an agent run's opaque `details.derive.parts[]` (M9.7 C, ADR-061
// §7/§10): the per-part derivation record the openRun RunDetail renders as a structured block after
// a composite capture finishes. `marker_index` is the 1-based part position the organizer attributes
// against; `status` is the media row's terminal derivation state (`derived`/`unavailable`/`pending`).
// All fields optional — `details` is an untyped blob, narrowed defensively at the render seam.
export interface RunDerivePart {
  media_id?: string;
  kind?: string;
  ordinal?: number | null;
  marker_index?: number;
  status?: string;
  model?: string | null;
  attempts?: number;
  error?: string | null;
}

// --- Activity feed + ops console (03-api.md §Activity & ops, M8 / ADR-053; M8.1 ADR-054 §4) ---
// The three feed categories, by *origin* (a hand-run job → manual_actions via agent_runs.trigger).
// `captures` (M8.1, was `conversations`) carries all captures regardless of source, not chat-only.
export type ActivityCategory = 'agents_jobs' | 'captures' | 'manual_actions';

// One normalized row of the merged GET /activity feed. `kind` discriminates the source entity
// (agent_run | capture | review_verdict — M8.1 renamed `chat_capture` → `capture`); the specific
// agent name / review kind rides in `title` (a capture row's `title` is always null — its snippet
// is the raw-text preview). `ref` is the drill-down target (a run id → GET /activity/runs/{id}, a
// chat-session id, a review id); `parent_ref` is always null on the M8.1 parentless feed (kept on
// the wire for compatibility, no longer drives rendering — step children live in the run detail's
// `children[]`). For a `capture` row `id` IS the capture id — the key the Captures one-tap remove
// targets and `GET /captures/{id}` expand fetches. `status`/`source` (M8.1 §4): `status` is the
// source row's lifecycle status; `source` is a capture's origin badge
// (`text`/`voice`/`mcp`/`chat`), null on the non-capture branches.
export interface ActivityFeedItem {
  id: string;
  category: ActivityCategory;
  kind: string;
  ts: string;
  title: string | null;
  snippet: string | null;
  ref: string | null;
  parent_ref: string | null;
  status: string | null;
  source: string | null;
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
