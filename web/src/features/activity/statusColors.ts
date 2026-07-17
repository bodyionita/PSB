import type { RunStatus } from '../../api/types';

// Shared run-status color vocabulary for the ops console + feed. Pure module (no components) so it
// stays fast-refresh-clean; the visual badge/dot components live in runStatus.tsx.
export const OK_COLOR = '#3ecf8e';
export const FAIL_COLOR = '#ff6b6b';
export const WARN_COLOR = '#f5a623';

export function statusColor(status: RunStatus): string {
  if (status === 'failed') return FAIL_COLOR;
  if (status === 'succeeded') return OK_COLOR;
  return 'var(--muted)'; // running / skipped
}
