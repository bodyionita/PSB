import { motion } from 'framer-motion';
import type { CSSProperties, ReactNode } from 'react';

type Variant = 'primary' | 'ghost';

const base: CSSProperties = {
  border: 'none',
  borderRadius: 'var(--radius)',
  padding: '12px 20px',
  fontSize: 15,
  fontWeight: 600,
  letterSpacing: 0.2,
};

// Springy micro-interaction on every touch target (06-web-app.md). framer-motion respects
// prefers-reduced-motion globally, so the tap spring quiets itself when asked.
export function Button({
  children,
  onClick,
  type = 'button',
  variant = 'primary',
  disabled = false,
  style,
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  variant?: Variant;
  disabled?: boolean;
  style?: CSSProperties;
}) {
  const variantStyle: CSSProperties =
    variant === 'primary'
      ? {
          background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
          color: 'var(--on-accent)',
          boxShadow: '0 8px 30px -8px var(--accent)',
        }
      : {
          background: 'var(--surface)',
          color: 'var(--text)',
          border: '1px solid var(--surface-border)',
        };

  return (
    <motion.button
      type={type}
      onClick={onClick}
      disabled={disabled}
      whileTap={{ scale: 0.96 }}
      whileHover={disabled ? undefined : { y: -1 }}
      transition={{ type: 'spring', stiffness: 500, damping: 30 }}
      style={{ ...base, ...variantStyle, opacity: disabled ? 0.55 : 1, ...style }}
    >
      {children}
    </motion.button>
  );
}
