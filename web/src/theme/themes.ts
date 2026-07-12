// Five switchable palettes (06-web-app.md). Themes are pure token swaps: no component knows
// a hard-coded color — everything reads CSS custom properties applied by ThemeProvider.

export type ThemeId = 'nebula' | 'aurora' | 'ember' | 'rose' | 'daylight';

export interface ThemeTokens {
  bg: string; // page background (deep)
  bgGlow: string; // radial accent haze behind everything
  surface: string; // glass surface base
  surfaceBorder: string; // hairline border on surfaces
  text: string;
  muted: string;
  accent: string;
  accent2: string; // gradient partner
  onAccent: string; // text/icon on top of the accent
  scheme: 'dark' | 'light';
}

export interface Theme {
  id: ThemeId;
  label: string;
  tokens: ThemeTokens;
}

export const THEMES: Record<ThemeId, Theme> = {
  nebula: {
    id: 'nebula',
    label: 'Nebula',
    tokens: {
      bg: '#0b0910',
      bgGlow: 'rgba(124, 92, 255, 0.20)',
      surface: 'rgba(255, 255, 255, 0.05)',
      surfaceBorder: 'rgba(255, 255, 255, 0.10)',
      text: '#F4F1FB',
      muted: 'rgba(244, 241, 251, 0.58)',
      accent: '#7C5CFF',
      accent2: '#9C7BFF',
      onAccent: '#0b0910',
      scheme: 'dark',
    },
  },
  aurora: {
    id: 'aurora',
    label: 'Aurora',
    tokens: {
      bg: '#07141a',
      bgGlow: 'rgba(45, 212, 191, 0.20)',
      surface: 'rgba(255, 255, 255, 0.05)',
      surfaceBorder: 'rgba(255, 255, 255, 0.10)',
      text: '#EAFBF7',
      muted: 'rgba(234, 251, 247, 0.58)',
      accent: '#2DD4BF',
      accent2: '#34D399',
      onAccent: '#04120f',
      scheme: 'dark',
    },
  },
  ember: {
    id: 'ember',
    label: 'Ember',
    tokens: {
      bg: '#141210',
      bgGlow: 'rgba(245, 176, 65, 0.18)',
      surface: 'rgba(255, 255, 255, 0.05)',
      surfaceBorder: 'rgba(255, 255, 255, 0.10)',
      text: '#FBF3EA',
      muted: 'rgba(251, 243, 234, 0.58)',
      accent: '#F5B041',
      accent2: '#F59E4B',
      onAccent: '#181206',
      scheme: 'dark',
    },
  },
  rose: {
    id: 'rose',
    label: 'Rose Quartz',
    tokens: {
      bg: '#160b14',
      bgGlow: 'rgba(255, 92, 138, 0.20)',
      surface: 'rgba(255, 255, 255, 0.05)',
      surfaceBorder: 'rgba(255, 255, 255, 0.10)',
      text: '#FBEAF1',
      muted: 'rgba(251, 234, 241, 0.58)',
      accent: '#FF5C8A',
      accent2: '#FF7AA8',
      onAccent: '#1a0710',
      scheme: 'dark',
    },
  },
  daylight: {
    id: 'daylight',
    label: 'Daylight',
    tokens: {
      bg: '#F6F6FB',
      bgGlow: 'rgba(91, 91, 214, 0.12)',
      surface: 'rgba(255, 255, 255, 0.72)',
      surfaceBorder: 'rgba(20, 18, 40, 0.10)',
      text: '#191A2B',
      muted: 'rgba(25, 26, 43, 0.58)',
      accent: '#5B5BD6',
      accent2: '#7573E6',
      onAccent: '#FFFFFF',
      scheme: 'light',
    },
  },
};

export const DEFAULT_THEME: ThemeId = 'nebula';
export const THEME_ORDER: ThemeId[] = ['nebula', 'aurora', 'ember', 'rose', 'daylight'];
