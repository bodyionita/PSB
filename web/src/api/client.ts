// The one place that knows server URLs (ADR-006). All requests send the session cookie
// (credentials: 'include'); non-2xx becomes a typed ApiError carrying the server `detail`.
import { API_BASE } from '../config';
import type {
  CaptureAcceptedResponse,
  CaptureView,
  HealthResponse,
  LoginResponse,
  MeResponse,
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
};
