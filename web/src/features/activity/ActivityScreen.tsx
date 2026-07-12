import { ComingSoon } from '../../ui/ComingSoon';
import { Surface } from '../../ui/Surface';

// Placeholder — the "what did my brain do" feed of agent runs, captures and errors
// lands in M4 (08-plan).
export function ActivityScreen() {
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Activity</h1>
        <ComingSoon milestone="M4" />
      </div>
      <Surface>
        <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.6 }}>
          A timeline of everything your brain did on its own — nightly Slack distillation,
          daily summaries, reindexing and backups — each entry expandable, with fallback
          events clearly badged.
        </p>
      </Surface>
    </div>
  );
}
