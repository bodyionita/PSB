// Single source of app-level constants (06-web-app.md). Product name is one constant so
// it's changeable at zero cost.
export const BRAND = 'Braindan';

// The only server fact the web knows (ADR-006): the API base path. Same-origin in prod
// (Caddy), proxied in dev (vite.config.ts). Kept relative so no host is ever hardcoded.
export const API_BASE = '/api/v1';
