import { motion } from 'framer-motion';
import { AppShell } from './AppShell';
import { BRAND } from './config';
import { LoginScreen } from './features/auth/LoginScreen';
import { useMe } from './features/auth/useAuth';

function Splash() {
  return (
    <div style={{ position: 'relative', zIndex: 1, minHeight: '100dvh', display: 'grid', placeItems: 'center' }}>
      <motion.div
        animate={{ opacity: [0.4, 1, 0.4] }}
        transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
        style={{ fontSize: 22, fontWeight: 700, letterSpacing: -0.4 }}
      >
        {BRAND}
      </motion.div>
    </div>
  );
}

export function App() {
  const me = useMe();

  if (me.isLoading) return <Splash />;

  const authenticated = me.data?.authenticated === true;

  // Enter-only fade, keyed by auth state. No AnimatePresence exit: the authenticated tree
  // contains infinite (repeat) animations that would stall an exit animation on logout.
  return authenticated ? (
    <motion.div key="app" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
      <AppShell />
    </motion.div>
  ) : (
    <motion.div key="login" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
      <LoginScreen />
    </motion.div>
  );
}
