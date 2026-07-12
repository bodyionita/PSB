import { ComingSoon } from '../../ui/ComingSoon';
import { Surface } from '../../ui/Surface';

// Placeholder — chat, streaming, model picker and source cards land in M3 (08-plan).
export function ChatScreen() {
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Chat</h1>
        <ComingSoon milestone="M3" />
      </div>
      <Surface>
        <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.6 }}>
          Ask questions across your notes and get answers with citations. A per-conversation
          model picker (Claude, with Nebius fallback) lives here, and a discreet banner shows
          when a fallback answered.
        </p>
      </Surface>
    </div>
  );
}
