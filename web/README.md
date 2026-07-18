# Braindan web (PWA)

React + Vite + TypeScript (strict). Talks **only** to the HTTP API (ADR-006); the single
fact it knows about the server is the API base path in `src/config.ts`.

## As built (through M8.2)

Premium, animated PWA over the full contract (see `../../second-brain-docs/06-web-app.md`):

- Themed **auth screen** (`/auth/login` + `/auth/me`); animated **app shell**, 6-tab nav
  (M8.1): **Capture** (hero record orb + text strip), **Chat** (grounded `[n]`-cited,
  per-conversation model picker, plane chips), **Review** (stance/entity/vocab/dedup/
  occurred-enrichment cards, batch actions), **Explore** (search ⇄ constellation map),
  **Activity** (captures feed + agents/jobs + Ops console with live log tail),
  **Settings** (Models routing groups, Providers status, Vocabulary, themes, sign out).
- Temporal rendering (M8.2): `[[t:…]]` tokens → live relative phrases + exact-date tooltips
  (`ui/dateToken.ts` — a byte-identical mirror of the server temporal engine); interiority
  markers; tap-to-edit date tokens + capture anchor editing.
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
├── ui/                  design-system primitives (Surface, Button, NodePreview, NodeChip,
│                        TokenizedBody, HoverTip, dateToken/nodeDetail helpers, …)
├── features/            auth, capture, chat, review, search + map (= the Explore tab),
│                        activity, settings
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
