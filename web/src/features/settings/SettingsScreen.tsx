import { motion } from 'framer-motion';
import { Button } from '../../ui/Button';
import { ComingSoon } from '../../ui/ComingSoon';
import { Surface } from '../../ui/Surface';
import { useTheme } from '../../theme/theme-context';
import { THEME_ORDER, THEMES } from '../../theme/themes';
import { useLogout, useMe } from '../auth/useAuth';

function ThemeSwitcher() {
  const { themeId, setTheme } = useTheme();
  return (
    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
      {THEME_ORDER.map((id) => {
        const theme = THEMES[id];
        const selected = id === themeId;
        return (
          <motion.button
            key={id}
            onClick={() => setTheme(id)}
            whileTap={{ scale: 0.94 }}
            aria-pressed={selected}
            aria-label={`${theme.label} theme`}
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 8,
              padding: 10,
              borderRadius: 'var(--radius)',
              background: 'transparent',
              border: selected
                ? '2px solid var(--accent)'
                : '1px solid var(--surface-border)',
            }}
          >
            <span
              style={{
                width: 40,
                height: 40,
                borderRadius: '50%',
                background: `linear-gradient(135deg, ${theme.tokens.accent}, ${theme.tokens.accent2})`,
                boxShadow: `0 6px 18px -6px ${theme.tokens.accent}`,
              }}
            />
            <span style={{ fontSize: 12, color: selected ? 'var(--text)' : 'var(--muted)' }}>
              {theme.label}
            </span>
          </motion.button>
        );
      })}
    </div>
  );
}

export function SettingsScreen() {
  const me = useMe();
  const logout = useLogout();
  const created = me.data?.session_created_at
    ? new Date(me.data.session_created_at).toLocaleString()
    : '—';

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4 }}>Settings</h1>

      <Surface>
        <h2 style={{ margin: '0 0 14px', fontSize: 16 }}>Theme</h2>
        <ThemeSwitcher />
        <p style={{ margin: '14px 0 0', fontSize: 13, color: 'var(--muted)' }}>
          Motion follows your device's reduced-motion setting automatically.
        </p>
      </Surface>

      <Surface>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
          <h2 style={{ margin: 0, fontSize: 16 }}>Agents</h2>
          <ComingSoon milestone="M4" />
        </div>
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)', lineHeight: 1.6 }}>
          Choose the distillation model and its fallback, and run connectors on demand.
        </p>
      </Surface>

      <Surface>
        <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>Session</h2>
        <p style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--muted)' }}>
          Signed in since {created}.
        </p>
        <Button variant="ghost" onClick={() => logout.mutate()} disabled={logout.isPending}>
          {logout.isPending ? 'Signing out…' : 'Sign out'}
        </Button>
      </Surface>
    </div>
  );
}
