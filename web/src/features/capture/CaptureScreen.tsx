import { motion } from 'framer-motion';
import { ComingSoon } from '../../ui/ComingSoon';

// Capture is the hero action (06-web-app.md). M0 ships the stage + the living record orb;
// the recording pipeline is wired in M1.
export function CaptureScreen() {
  return (
    <div style={{ display: 'grid', placeItems: 'center', gap: 28, padding: '32px 8px' }}>
      <div style={{ textAlign: 'center' }}>
        <h1 style={{ margin: '0 0 6px', fontSize: 26, fontWeight: 700, letterSpacing: -0.4 }}>
          What's on your mind?
        </h1>
        <p style={{ margin: 0, color: 'var(--muted)' }}>Tap to capture a thought.</p>
      </div>

      <div style={{ position: 'relative', width: 220, height: 220, display: 'grid', placeItems: 'center' }}>
        {/* Breathing halo behind the button. */}
        <motion.div
          aria-hidden
          animate={{ scale: [1, 1.15, 1], opacity: [0.5, 0.2, 0.5] }}
          transition={{ duration: 3.4, repeat: Infinity, ease: 'easeInOut' }}
          style={{
            position: 'absolute',
            width: 220,
            height: 220,
            borderRadius: '50%',
            background: 'radial-gradient(circle, var(--accent), transparent 65%)',
          }}
        />
        <motion.button
          whileTap={{ scale: 0.92 }}
          whileHover={{ scale: 1.03 }}
          transition={{ type: 'spring', stiffness: 400, damping: 22 }}
          aria-label="Record a voice capture"
          style={{
            width: 148,
            height: 148,
            borderRadius: '50%',
            border: 'none',
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            boxShadow: '0 20px 60px -18px var(--accent)',
            color: 'var(--on-accent)',
            fontSize: 44,
          }}
        >
          ●
        </motion.button>
      </div>

      <ComingSoon milestone="Recording — M1" />
    </div>
  );
}
