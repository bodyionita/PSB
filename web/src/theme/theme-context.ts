import { createContext, useContext } from 'react';
import { DEFAULT_THEME, type ThemeId } from './themes';

export interface ThemeContextValue {
  themeId: ThemeId;
  setTheme: (id: ThemeId) => void;
}

export const ThemeContext = createContext<ThemeContextValue>({
  themeId: DEFAULT_THEME,
  setTheme: () => undefined,
});

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
