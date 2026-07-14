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

// --- Review queue (03-api.md §Review queue, M3 / ADR-030 §3) ---
export type ReviewVerdict = 'approve' | 'reject';
// entity-ambiguity: a candidate node id | "new" | "maybe". vocab-proposal: approve | reject.
export type ReviewChoice = string;

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
