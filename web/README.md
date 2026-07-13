# Braindan web (PWA)

React + Vite + TypeScript (strict). Talks **only** to the HTTP API (ADR-006); the single
fact it knows about the server is the API base path in `src/config.ts`.

## M0 scope (as-built snapshot — pre-pivot vocabulary; see the root README's pivot note)

Premium, animated foundation with stubbed features (ADR-012):

- Themed **auth screen** wired to real `/auth/login` + `/auth/me`.
- Animated **app shell** with bottom nav + page transitions (framer-motion).
- Four screens: **Capture** (hero record orb), **Chat**, **Activity**, **Settings**
  (real theme switcher over 5 palettes, session info, sign out).
- 5 switchable themes as pure CSS-variable swaps; choice persisted in `localStorage`.
  Default **Nebula**. `prefers-reduced-motion` respected globally.
- Installable manifest + icon. (Service-worker offline shell is backlog polish —
  see the docs repo's 08 backlog.)

## Structure

```
src/
├── config.ts            BRAND + API_BASE (the only server fact)
├── api/                 client.ts (fetch, cookies) + types.ts (hand-kept wire types)
├── theme/               5 palettes, ThemeProvider, global.css
├── ui/                  design-system primitives (Surface, Button, ComingSoon)
├── features/            auth, capture, chat, activity, settings
├── App.tsx              auth gate: splash → login | app shell
└── AppShell.tsx         nav + animated screen transitions
```

## Dev

Requires **Node 24** (`.nvmrc`) and **pnpm 9**. pnpm is pinned via the `packageManager`
field, so `corepack enable` (bundled with Node) gives you the right version automatically.

```bash
corepack enable   # one-time; provisions the pinned pnpm 9
pnpm install
pnpm dev        # http://localhost:5173 ; /api proxied to the server (vite.config.ts)
pnpm build      # tsc -b (strict) + vite production build
pnpm lint
```
