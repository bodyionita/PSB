import { ComingSoon } from '../../ui/ComingSoon';
import { Surface } from '../../ui/Surface';

// Placeholder — the categorized "what did my brain do" feed + ops console (jobs with schedules,
// live log tail, run-now) is the M8 restructure (08-plan §M8).
export function ActivityScreen() {
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Activity</h1>
        <ComingSoon milestone="M8" />
      </div>
      <Surface>
        <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.6 }}>
          A timeline of everything your brain did on its own — nightly consolidation and
          reflection, reindexing and backups — each entry expandable, with fallback events
          clearly badged, and every job runnable on demand with a live log tail.
        </p>
      </Surface>
    </div>
  );
}
