import { useCallback, useLayoutEffect, useMemo, useState, type ReactNode } from 'react';
import { ThemeContext } from './theme-context';
import { DEFAULT_THEME, THEMES, type ThemeId } from './themes';

const STORAGE_KEY = 'braindan.theme';

function readStoredTheme(): ThemeId {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored && stored in THEMES) return stored as ThemeId;
  return DEFAULT_THEME;
}

function applyTheme(id: ThemeId): void {
  const { tokens } = THEMES[id];
  const root = document.documentElement;
  root.style.setProperty('--bg', tokens.bg);
  root.style.setProperty('--bg-glow', tokens.bgGlow);
  root.style.setProperty('--surface', tokens.surface);
  root.style.setProperty('--surface-border', tokens.surfaceBorder);
  root.style.setProperty('--text', tokens.text);
  root.style.setProperty('--muted', tokens.muted);
  root.style.setProperty('--accent', tokens.accent);
  root.style.setProperty('--accent-2', tokens.accent2);
  root.style.setProperty('--on-accent', tokens.onAccent);
  root.style.colorScheme = tokens.scheme;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [themeId, setThemeId] = useState<ThemeId>(readStoredTheme);

  // useLayoutEffect so tokens are set before first paint — no flash of the wrong palette.
  useLayoutEffect(() => {
    applyTheme(themeId);
  }, [themeId]);

  const setTheme = useCallback((id: ThemeId) => {
    localStorage.setItem(STORAGE_KEY, id);
    setThemeId(id);
  }, []);

  const value = useMemo(() => ({ themeId, setTheme }), [themeId, setTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
