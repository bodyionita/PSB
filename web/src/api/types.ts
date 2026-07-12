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
