import { useEffect, useState } from 'react';
import { ApiError } from '../../api/client';
import type { AgentRosterItem, LastRun, PipelineItem } from '../../api/types';
import { Button } from '../../ui/Button';
import { TimeAgo } from '../../ui/TimeAgo';
import { AdminOps } from './AdminOps';
import { DuplicateCandidatesCard } from './DuplicateCandidatesCard';
import { GraphHealthCard } from './GraphHealthCard';
import { RunLogTail } from './RunLogTail';
import { StatusBadge } from './runStatus';
import { FAIL_COLOR } from './statusColors';
import { useAgents, usePipelines, useRunAgent, useRunPipeline } from './useActivity';

// The Ops console (06 §3, invariant 4): the live scheduler as pipelines + a flat agent roster, every
// job runnable on demand with a live log tail, the graph-health card, and the rehomed parameterized
// admin ops. The M2 Admin panel is absorbed here.

const GRAPH_HEALTH = 'graph-health';
const ENTITY_DEDUP = 'entity-dedup';

function triggerError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return 'Already running.';
    if (err.status === 503) return 'The scheduler is not running on this instance.';
    if (err.status === 404) return 'Unknown job.';
    return err.message;
  }
  return 'Couldn’t start the run.';
}

function LastRunLine({ last }: { last: LastRun | null }) {
  if (!last) return <span style={{ fontSize: 12, color: 'var(--muted)' }}>Never run</span>;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <StatusBadge status={last.status} />
      {last.finished_at && (
        <TimeAgo iso={last.finished_at} style={{ fontSize: 12, color: 'var(--muted)' }} />
      )}
    </span>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 600,
        color: 'var(--muted)',
        border: '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '2px 8px',
      }}
    >
      {children}
    </span>
  );
}

// --- Pipelines ----------------------------------------------------------------------------------

// Latch the run id to tail once a run goes live, and keep the tail mounted after it finishes so
// `useRunLogs` can drain the async on-finish flush (ADR-053 §2) — unmounting on `running===false`
// would drop the final lines. Cleared only when a newer run supersedes it.
function useTailRunId(running: boolean, runId: string | null | undefined): string | null {
  const [tailRunId, setTailRunId] = useState<string | null>(null);
  useEffect(() => {
    if (running && runId) setTailRunId(runId);
  }, [running, runId]);
  return tailRunId;
}

function PipelineRow({ pipeline }: { pipeline: PipelineItem }) {
  const run = useRunPipeline();
  const running = pipeline.last_run?.status === 'running';
  const tailRunId = useTailRunId(running, pipeline.last_run?.run_id);
  return (
    <div
      style={{
        padding: 14,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 10,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 15, fontWeight: 700 }}>{pipeline.name}</span>
        <Chip>{pipeline.cron}</Chip>
        {pipeline.next_run && (
          <Chip>
            next <TimeAgo iso={pipeline.next_run} />
          </Chip>
        )}
        <div style={{ marginLeft: 'auto' }}>
          <Button
            variant="ghost"
            onClick={() => run.mutate(pipeline.name)}
            disabled={run.isPending || running}
            style={{ padding: '8px 14px', fontSize: 13 }}
          >
            {running ? 'Running…' : run.isPending ? 'Starting…' : 'Run pipeline'}
          </Button>
        </div>
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {pipeline.steps.map((s, i) => (
          <span key={s} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            {i > 0 && <span style={{ color: 'var(--muted)', fontSize: 11 }}>→</span>}
            <Chip>{s}</Chip>
          </span>
        ))}
      </div>
      <LastRunLine last={pipeline.last_run} />
      {run.isError && <span style={{ fontSize: 12, color: FAIL_COLOR }}>{triggerError(run.error)}</span>}
      {tailRunId && <RunLogTail runId={tailRunId} />}
    </div>
  );
}

function PipelinesSection() {
  const { data, isLoading, isError } = usePipelines();
  return (
    <section>
      <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>Pipelines</h2>
      <p style={{ margin: '0 0 12px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        Each pipeline runs its ordered steps on one schedule. Run a whole pipeline on demand.
      </p>
      {isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>
      ) : isError || !data ? (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load pipelines.</p>
      ) : data.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>No pipelines scheduled.</p>
      ) : (
        <div style={{ display: 'grid', gap: 10 }}>
          {data.map((p) => (
            <PipelineRow key={p.name} pipeline={p} />
          ))}
        </div>
      )}
    </section>
  );
}

// --- Agent roster -------------------------------------------------------------------------------

function AgentRow({ agent }: { agent: AgentRosterItem }) {
  const run = useRunAgent();
  const tailRunId = useTailRunId(agent.running, agent.last_run?.run_id);
  return (
    <div
      style={{
        padding: 14,
        borderRadius: 'var(--radius)',
        border: '1px solid var(--surface-border)',
        display: 'grid',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 14, fontWeight: 700 }}>{agent.name}</span>
        {agent.pipelines.length === 0 ? (
          <Chip>on-demand</Chip>
        ) : (
          agent.pipelines.map((p) => <Chip key={p}>{p}</Chip>)
        )}
        <div style={{ marginLeft: 'auto' }}>
          <Button
            variant="ghost"
            onClick={() => run.mutate(agent.name)}
            disabled={run.isPending || agent.running}
            style={{ padding: '8px 14px', fontSize: 13 }}
          >
            {agent.running ? 'Running…' : run.isPending ? 'Starting…' : 'Run'}
          </Button>
        </div>
      </div>
      <LastRunLine last={agent.last_run} />
      {run.isError && <span style={{ fontSize: 12, color: FAIL_COLOR }}>{triggerError(run.error)}</span>}
      {tailRunId && <RunLogTail runId={tailRunId} />}
    </div>
  );
}

function RosterSection() {
  const { data, isLoading, isError } = useAgents();
  return (
    <section>
      <h2 style={{ margin: '0 0 6px', fontSize: 16 }}>Jobs</h2>
      <p style={{ margin: '0 0 12px', fontSize: 13, color: 'var(--muted)', lineHeight: 1.5 }}>
        Every job, runnable on demand — with its live status and log tail. A job with no pipeline runs
        only when you trigger it.
      </p>
      {isLoading ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>
      ) : isError || !data ? (
        <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load the roster.</p>
      ) : data.length === 0 ? (
        <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>No jobs registered.</p>
      ) : (
        <div style={{ display: 'grid', gap: 10 }}>
          {data.map((a) => (
            <AgentRow key={a.name} agent={a} />
          ))}
        </div>
      )}
    </section>
  );
}

export function OpsView() {
  // The graph-health + duplicate-candidates cards each read the LATEST run of their job off the
  // roster's last_run.run_id (graph-health / entity-dedup — same mechanism, ADR-064 §3/§4).
  const { data: agents } = useAgents();
  const graphHealthRunId =
    agents?.find((a) => a.name === GRAPH_HEALTH)?.last_run?.run_id ?? null;
  const entityDedupRunId =
    agents?.find((a) => a.name === ENTITY_DEDUP)?.last_run?.run_id ?? null;

  return (
    <div style={{ display: 'grid', gap: 20 }}>
      <PipelinesSection />
      <RosterSection />
      <GraphHealthCard runId={graphHealthRunId} />
      <DuplicateCandidatesCard runId={entityDedupRunId} />
      <div>
        <h2 style={{ margin: '0 0 12px', fontSize: 16 }}>Operations</h2>
        <AdminOps />
      </div>
    </div>
  );
}
