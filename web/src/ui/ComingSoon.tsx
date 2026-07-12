// Small badge marking a stubbed feature — M0 ships the foundation, features land per
// milestone (08-implementation-plan.md).
export function ComingSoon({ milestone }: { milestone: string }) {
  return (
    <span
      style={{
        display: 'inline-block',
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.6,
        textTransform: 'uppercase',
        color: 'var(--accent)',
        background: 'var(--surface)',
        border: '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '4px 10px',
      }}
    >
      {milestone}
    </span>
  );
}
