import { motion } from 'framer-motion';
import { useState, type FormEvent } from 'react';
import { ApiError } from '../../api/client';
import { BRAND } from '../../config';
import { Button } from '../../ui/Button';
import { Surface } from '../../ui/Surface';
import { useLogin } from './useAuth';

export function LoginScreen() {
  const login = useLogin();
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    login.mutate(password, {
      onError: (err) => {
        if (err instanceof ApiError && err.status === 429) {
          setError('Too many attempts. Give it a moment, then try again.');
        } else if (err instanceof ApiError && err.status === 401) {
          setError('That password did not match.');
        } else {
          setError('Could not reach the server.');
        }
      },
    });
  }

  return (
    <div
      style={{
        position: 'relative',
        zIndex: 1,
        minHeight: '100dvh',
        display: 'grid',
        placeItems: 'center',
        padding: 24,
      }}
    >
      <motion.div
        initial={{ opacity: 0, y: 24, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: 'spring', stiffness: 260, damping: 24 }}
        style={{ width: '100%', maxWidth: 380 }}
      >
        <Surface padding={28}>
          <motion.h1
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.1 }}
            style={{ margin: '4px 0 2px', fontSize: 30, fontWeight: 700, letterSpacing: -0.5 }}
          >
            {BRAND}
          </motion.h1>
          <p style={{ margin: '0 0 22px', color: 'var(--muted)', fontSize: 15 }}>
            Your memory, unlocked.
          </p>

          <form onSubmit={onSubmit}>
            <input
              type="password"
              autoComplete="current-password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-label="Password"
              autoFocus
              style={{
                width: '100%',
                padding: '13px 16px',
                marginBottom: 14,
                fontSize: 16,
                color: 'var(--text)',
                background: 'var(--surface)',
                border: '1px solid var(--surface-border)',
                borderRadius: 'var(--radius)',
                outline: 'none',
              }}
            />

            {error && (
              <motion.div
                initial={{ opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                style={{ color: '#ff6b8a', fontSize: 13.5, marginBottom: 14 }}
                role="alert"
              >
                {error}
              </motion.div>
            )}

            <Button
              type="submit"
              disabled={login.isPending || password.length === 0}
              style={{ width: '100%' }}
            >
              {login.isPending ? 'Unlocking…' : 'Unlock'}
            </Button>
          </form>
        </Surface>
      </motion.div>
    </div>
  );
}
