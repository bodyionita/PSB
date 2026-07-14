import { ComingSoon } from '../../ui/ComingSoon';
import { Surface } from '../../ui/Surface';

// Placeholder — chat, the client-side reveal, model picker and source cards land in M4 (08-plan;
// the pivot moved chat from the old M3 to M4, retargeted to nodes).
export function ChatScreen() {
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Chat</h1>
        <ComingSoon milestone="M4" />
      </div>
      <Surface>
        <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.6 }}>
          Ask questions across your memories and get answers with citations. A per-conversation
          model picker (Claude, with Nebius fallback) lives here, and a discreet banner shows
          when a fallback answered.
        </p>
      </Surface>
    </div>
  );
}
