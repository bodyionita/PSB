import type { CSSProperties } from 'react';
import type { ProviderCapability, ProviderStatusItem } from '../../api/types';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { useProviders } from './useProviders';

// Settings → Providers (06 §4, M4 follow-up / ADR-044): a read-only glance at each model provider's
// runtime health. It closes the M4 silent-fallback gap (vision P8 / rule 7) — when a provider quietly
// falls back, its sticky `last_error` and failure count show *why*, here on the phone. No actions, no
// editing — a thin TanStack read over `GET /admin/providers` (ADR-006). The dot follows
// `consecutive_failures` (green at 0, amber above); `reachable` is a config-reachability probe, NOT a
// success guarantee, so it's a quiet secondary tag — the error line is the real diagnostic.

const OK_COLOR = '#3ecf8e';
const WARN_COLOR = '#f5a623';
const FAIL_COLOR = '#ff6b6b';

const CAPABILITY_LABEL: Record<ProviderCapability, string> = {
  chat: 'Chat',
  stt: 'Speech',
  embedding: 'Embedding',
};

const chipStyle: CSSProperties = {
  padding: '2px 8px',
  borderRadius: 999,
  border: '1px solid var(--surface-border)',
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--muted)',
};

function ProviderRow({ provider }: { provider: ProviderStatusItem }) {
  const failing = provider.consecutive_failures > 0;
  const dot = failing ? WARN_COLOR : OK_COLOR;

  return (
    <div
      style={{
        padding: 14,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span
          aria-hidden
          style={{
            width: 10,
            height: 10,
            borderRadius: '50%',
            background: dot,
            boxShadow: `0 0 8px -1px ${dot}`,
            flex: 'none',
          }}
        />
        {/* Friendly provider name only — one row per provider, no raw id in the UI (ADR-045 §6). */}
        <span style={{ fontSize: 15, fontWeight: 700 }}>{provider.label}</span>
        <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', flexWrap: 'wrap' }}>
          {provider.capabilities.map((c) => (
            <span key={c} style={chipStyle}>
              {CAPABILITY_LABEL[c] ?? c}
            </span>
          ))}
          {!provider.reachable && (
            <span
              style={{ ...chipStyle, color: FAIL_COLOR, borderColor: FAIL_COLOR }}
              title="The provider's health probe is not reachable (configuration check — not a success guarantee)."
            >
              unreachable
            </span>
          )}
        </div>
      </div>

      {provider.last_error && (
        // Sticky error stays visible after recovery (ADR-044) — but mute it once the provider is
        // healthy again so a green dot never sits beside a red line that reads as a live failure.
        <div
          style={{
            fontSize: 12.5,
            color: failing ? FAIL_COLOR : 'var(--muted)',
            lineHeight: 1.45,
            wordBreak: 'break-word',
          }}
        >
          {provider.last_error.message}
          <span style={{ color: 'var(--muted)' }}>
            {' · '}
            <TimeAgo iso={provider.last_error.at} />
          </span>
        </div>
      )}

      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, color: 'var(--muted)' }}>
        <span>
          {provider.last_success_at ? (
            <>
              Last success <TimeAgo iso={provider.last_success_at} />
            </>
          ) : (
            'No successful call yet'
          )}
        </span>
        {failing && (
          <span style={{ color: WARN_COLOR }}>
            {provider.consecutive_failures} consecutive{' '}
            {provider.consecutive_failures === 1 ? 'failure' : 'failures'}
          </span>
        )}
      </div>
    </div>
  );
}

export function ProvidersPanel() {
  const { data, isLoading, isError } = useProviders();

  return (
    <Surface>
      <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>Providers</h2>
      <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        Live health of each model provider. A green dot means recent calls succeeded; amber means the
        last calls failed — the error line shows why a provider fell back.
      </p>

      {isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>
      ) : isError || !data ? (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load provider status.</p>
      ) : data.providers.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>No providers registered.</p>
      ) : (
        <div style={{ display: 'grid', gap: 12 }}>
          {data.providers.map((p) => (
            <ProviderRow key={p.id} provider={p} />
          ))}
        </div>
      )}
    </Surface>
  );
}
