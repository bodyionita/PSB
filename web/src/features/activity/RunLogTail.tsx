import { useEffect, useRef } from 'react';
import type { RunLogLine } from '../../api/types';
import { useRunLogs } from './useActivity';
import { FAIL_COLOR, WARN_COLOR } from './statusColors';

// The live log tail (06 §3, ADR-053 §1/§2): a monospace, auto-scrolling readout of a run's captured
// `app.*`/INFO+ lines, polled ~1s while the run is live (and drained past the tail cap after it
// finishes). Given a run id, it owns its own polling via `useRunLogs`.

function levelColor(level: string): string {
  const l = level.toUpperCase();
  if (l === 'ERROR' || l === 'CRITICAL') return FAIL_COLOR;
  if (l === 'WARNING' || l === 'WARN') return WARN_COLOR;
  return 'var(--muted)';
}

function LogLine({ line }: { line: RunLogLine }) {
  return (
    <div style={{ display: 'flex', gap: 8, padding: '1px 0' }}>
      <span style={{ color: levelColor(line.level), flex: 'none', fontWeight: 600 }}>
        {line.level.toUpperCase().slice(0, 4)}
      </span>
      <span
        style={{
          minWidth: 0,
          color: 'var(--text)',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
          wordBreak: 'break-word',
        }}
      >
        {line.message}
      </span>
    </div>
  );
}

export function RunLogTail({ runId }: { runId: string }) {
  const { lines, running } = useRunLogs(runId);
  const boxRef = useRef<HTMLDivElement>(null);

  // Keep the newest line in view as the tail grows.
  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines.length]);

  return (
    <div
      ref={boxRef}
      style={{
        marginTop: 12,
        maxHeight: 260,
        overflowY: 'auto',
        padding: 12,
        borderRadius: 'var(--radius)',
        background: 'var(--bg, rgba(0,0,0,0.25))',
        border: '1px solid var(--surface-border)',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 12,
        lineHeight: 1.5,
      }}
    >
      {lines.length === 0 ? (
        <span style={{ color: 'var(--muted)' }}>
          {running ? 'Waiting for log output…' : 'No log output was captured for this run.'}
        </span>
      ) : (
        lines.map((line) => <LogLine key={line.seq} line={line} />)
      )}
    </div>
  );
}
