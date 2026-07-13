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
  vault: boolean;
  git_remote: boolean;
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
  note_paths: string[];
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

// --- Search & notes (03-api.md §Search & notes, M2 / ADR-022/023) ---
export interface SearchRequest {
  query: string;
  top_k?: number;
  // Filters on `notes.planes` membership (array overlap, not folder — ADR-005). Omit/[] = no filter.
  planes?: string[];
}

// One note-grouped hit; the best-matching chunk is the snippet, ranked by score.
export interface SearchResultItem {
  note_id: string;
  vault_path: string;
  title: string | null;
  plane: string | null;
  planes: string[];
  tags: string[];
  snippet: string;
  score: number;
}

// A semantic neighbour from the `note_links` relatedness graph (ADR-023).
export interface RelatedNoteItem {
  note_id: string;
  vault_path: string;
  title: string | null;
  score: number;
}

// Read-only note preview (GET /notes/{id}) — body read live from the vault file + neighbours.
export interface NotePreviewResponse {
  note_id: string;
  vault_path: string;
  title: string | null;
  plane: string | null;
  planes: string[];
  tags: string[];
  body: string;
  related: RelatedNoteItem[];
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
