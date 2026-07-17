import type { CSSProperties, ReactNode } from 'react';

// Glass surface primitive — the app's building block for cards and panels.
export function Surface({
  children,
  style,
  padding = 16,
}: {
  children: ReactNode;
  style?: CSSProperties;
  padding?: number;
}) {
  return (
    <div
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--surface-border)',
        borderRadius: 'var(--radius-lg)',
        backdropFilter: 'blur(18px)',
        WebkitBackdropFilter: 'blur(18px)',
        padding,
        ...style,
      }}
    >
      {children}
    </div>
  );
}
