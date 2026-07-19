// Durable per-run "resolved" state for cards that render a FROZEN past-run snapshot (M9.8 T7 fix,
// ADR-064 §3). The graph-health + duplicate-candidates cards read the latest agent_run's `details`
// — an immutable sample — so an acted-on offender (deleted / kept / merged) can't drop from the
// list; T6 settled it to a *local* resolved state that was lost on any remount/refetch, so the item
// "came back" until the next run re-ran. This persists the resolved state keyed by `runId` in
// localStorage: it survives remounts and reloads, and auto-clears when a fresh run replaces the
// snapshot (a new `runId` → a different key → an empty set). Not server truth — a lightweight client
// memory of what the operator already handled against one specific run.
import { useCallback, useEffect, useState } from 'react';

export type ResolvedStatus = 'deleted' | 'kept' | 'merged';

type ResolvedMap = Record<string, ResolvedStatus>;

function storageKey(scope: string, runId: string): string {
  return `psb:resolved:${scope}:${runId}`;
}

function read(key: string | null): ResolvedMap {
  if (!key) return {};
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === 'object' ? (parsed as ResolvedMap) : {};
  } catch {
    // A private-mode / quota / parse failure degrades to no-memory (the card still works, it just
    // loses the durability this hook adds) — never throw into render.
    return {};
  }
}

function write(key: string, map: ResolvedMap): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(map));
  } catch {
    /* best-effort — see read() */
  }
}

export interface ResolvedRunItems {
  statusOf: (id: string) => ResolvedStatus | undefined;
  mark: (id: string, status: ResolvedStatus) => void;
}

// `scope` namespaces the store per card (e.g. 'graph-health-orphan', 'dedup-candidates'); `runId` is
// the snapshot the resolutions belong to (null while no run exists → no persistence).
export function useResolvedRunItems(scope: string, runId: string | null): ResolvedRunItems {
  const key = runId ? storageKey(scope, runId) : null;
  const [map, setMap] = useState<ResolvedMap>(() => read(key));

  // Reload the set whenever the run (key) changes — a new run is a fresh, empty snapshot.
  useEffect(() => {
    setMap(read(key));
  }, [key]);

  const mark = useCallback(
    (id: string, status: ResolvedStatus) => {
      if (!key) return;
      setMap((prev) => {
        const next = { ...prev, [id]: status };
        write(key, next);
        return next;
      });
    },
    [key],
  );

  return { statusOf: (id) => map[id], mark };
}
