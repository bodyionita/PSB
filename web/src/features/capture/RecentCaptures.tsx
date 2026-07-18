import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useState, type FormEvent } from 'react';
import type { CaptureStatus, CaptureView } from '../../api/types';
import { Surface } from '../../ui/Surface';
import { TimeAgo } from '../../ui/TimeAgo';
import { useActivityNav } from '../activity/activityNav';
import { NodeRefChips } from '../../ui/NodeRefChips';
import { CaptureMediaBlock } from '../../ui/media/CaptureMediaBlock';
import { useCaptures, useRetryCapture, useSubmitFollowUp } from './useCaptures';

// Recent captures strip with live pipeline status (06 §Capture). Polling lives in useCaptures.
// M8.1 (ADR-054 §4): the strip shrinks to ~5 (RECENTS_LIMIT) with in-place expand (full text,
// unclamped) + a "see all → Activity" link into the Captures feed tab.

const RECENTS_LIMIT = 5;

type Tone = 'progress' | 'done' | 'fail';

const STATUS_META: Record<CaptureStatus, { label: string; tone: Tone }> = {
  received: { label: 'Received', tone: 'progress' },
  transcribing: { label: 'Transcribing', tone: 'progress' },
  deriving: { label: 'Reading photo', tone: 'progress' },
  organizing: { label: 'Organizing', tone: 'progress' },
  written: { label: 'Writing nodes', tone: 'progress' },
  indexed: { label: 'Saved', tone: 'done' },
  failed: { label: 'Failed', tone: 'fail' },
};

const FAIL_COLOR = '#ff6b6b';

function metaFor(status: CaptureStatus): { label: string; tone: Tone } {
  return STATUS_META[status] ?? { label: status, tone: 'progress' };
}

function StatusPill({ status }: { status: CaptureStatus }) {
  const { label, tone } = metaFor(status);
  const color = tone === 'fail' ? FAIL_COLOR : tone === 'done' ? 'var(--accent)' : 'var(--muted)';
  // Respect prefers-reduced-motion (06 / CLAUDE.md): the progress dot is an autonomous, infinite
  // pulse — exactly what reduced-motion suppresses. Render it static when reduced motion is on.
  const reduceMotion = useReducedMotion();
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.4,
        textTransform: 'uppercase',
        color,
      }}
    >
      {tone === 'progress' && (
        <motion.span
          aria-hidden
          animate={reduceMotion ? undefined : { opacity: [1, 0.25, 1] }}
          transition={
            reduceMotion ? undefined : { duration: 1.1, repeat: Infinity, ease: 'easeInOut' }
          }
          style={{ width: 7, height: 7, borderRadius: '50%', background: 'currentColor' }}
        />
      )}
      {tone === 'done' && <span aria-hidden>✓</span>}
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={label}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18 }}
        >
          {label}
        </motion.span>
      </AnimatePresence>
    </span>
  );
}

function NudgePrompt({ capture }: { capture: CaptureView }) {
  const [answer, setAnswer] = useState('');
  const followUp = useSubmitFollowUp();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = answer.trim();
    if (!trimmed || followUp.isPending) return;
    followUp.mutate(
      { id: capture.capture_id, answer: trimmed },
      { onSuccess: () => setAnswer('') },
    );
  };

  return (
    <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--surface-border)' }}>
      <p style={{ margin: '0 0 8px', fontSize: 13, color: 'var(--text)', lineHeight: 1.4 }}>
        {capture.follow_up_question}
      </p>
      <form onSubmit={submit} style={{ display: 'flex', gap: 8 }}>
        <input
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          placeholder="Add a little more…"
          disabled={followUp.isPending}
          style={{
            flex: 1,
            padding: '8px 12px',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: 13,
            outline: 'none',
          }}
        />
        <motion.button
          type="submit"
          whileTap={{ scale: 0.94 }}
          disabled={followUp.isPending || answer.trim() === ''}
          style={{
            padding: '8px 14px',
            borderRadius: 'var(--radius)',
            border: 'none',
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: 'var(--on-accent)',
            fontSize: 13,
            fontWeight: 600,
            opacity: followUp.isPending || answer.trim() === '' ? 0.5 : 1,
          }}
        >
          Send
        </motion.button>
      </form>
      {followUp.isError && (
        <p style={{ margin: '8px 0 0', fontSize: 12, color: FAIL_COLOR }}>
          Couldn’t send that — try again.
        </p>
      )}
    </div>
  );
}

// In-place expand threshold: below this the 3-line clamp never actually truncates anything, so no
// "Show more" affordance is offered (M8.1, ADR-054 §4 — expand only where there's more to show).
const EXPAND_THRESHOLD = 180;

function CaptureRow({ capture }: { capture: CaptureView }) {
  const retry = useRetryCapture();
  const [expanded, setExpanded] = useState(false);
  const isVoice = capture.kind === 'voice';
  const isImage = capture.kind === 'image';
  const hasText = capture.raw_text != null && capture.raw_text.trim() !== '';
  // Status is conveyed by the pill; the snippet just labels a not-yet-derived voice/photo capture.
  const snippet = hasText
    ? capture.raw_text
    : isVoice
      ? 'Voice note'
      : isImage
        ? 'Photo'
        : '…';
  const expandable = hasText && (capture.raw_text as string).length > EXPAND_THRESHOLD;
  const showNudge =
    capture.follow_up_question != null && capture.follow_up_answer == null;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ type: 'spring', stiffness: 420, damping: 34 }}
    >
      <Surface padding={16} style={{ borderRadius: 'var(--radius)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
            <span aria-hidden style={{ fontSize: 15, color: 'var(--muted)' }}>
              {isVoice ? '◉' : isImage ? '❏' : '✎'}
            </span>
            <StatusPill status={capture.status} />
          </div>
          <TimeAgo
            iso={capture.created_at}
            style={{ fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}
          />
        </div>

        {/* The capture's photo/voice (M9 T5, ADR-060 §7) — thumbnail → lightbox, or inline player. */}
        {capture.media && (
          <div style={{ marginTop: 12 }}>
            <CaptureMediaBlock media={capture.media} />
          </div>
        )}

        <p
          style={{
            margin: '10px 0 0',
            minWidth: 0,
            fontSize: 14,
            lineHeight: 1.45,
            color: hasText ? 'var(--text)' : 'var(--muted)',
            whiteSpace: 'pre-wrap',
            // Break long unbroken tokens so raw text never runs under the card edge on a phone.
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
            ...(expanded
              ? {}
              : {
                  display: '-webkit-box',
                  WebkitLineClamp: 3,
                  WebkitBoxOrient: 'vertical' as const,
                  overflow: 'hidden',
                }),
          }}
        >
          {snippet}
        </p>
        {expandable && (
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            style={{
              marginTop: 4,
              background: 'none',
              border: 'none',
              padding: 0,
              fontSize: 12,
              fontWeight: 600,
              color: 'var(--accent)',
              cursor: 'pointer',
            }}
          >
            {expanded ? 'Show less' : 'Show more'}
          </button>
        )}

        {capture.node_paths.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <NodeRefChips paths={capture.node_paths} refs={capture.node_refs} />
          </div>
        )}

        {capture.status === 'failed' && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginTop: 12 }}>
            <span style={{ fontSize: 12, color: FAIL_COLOR, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {capture.error ?? 'Something went wrong.'}
            </span>
            <motion.button
              onClick={() => retry.mutate(capture.capture_id)}
              whileTap={{ scale: 0.94 }}
              disabled={retry.isPending}
              style={{
                padding: '7px 14px',
                borderRadius: 'var(--radius)',
                border: '1px solid var(--surface-border)',
                background: 'var(--surface)',
                color: 'var(--text)',
                fontSize: 13,
                fontWeight: 600,
                whiteSpace: 'nowrap',
                opacity: retry.isPending ? 0.6 : 1,
              }}
            >
              Retry
            </motion.button>
          </div>
        )}

        {showNudge && <NudgePrompt capture={capture} />}
      </Surface>
    </motion.div>
  );
}

export function RecentCaptures() {
  const { data, isLoading, isError } = useCaptures(RECENTS_LIMIT);
  const activityNav = useActivityNav();

  return (
    <section style={{ width: '100%' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 10,
          margin: '0 0 12px',
        }}
      >
        <h2
          style={{
            margin: 0,
            fontSize: 13,
            fontWeight: 700,
            letterSpacing: 0.6,
            textTransform: 'uppercase',
            color: 'var(--muted)',
          }}
        >
          Recent
        </h2>
        {/* "see all → Activity" (M8.1, ADR-054 §4): degrades to an inert label until AppShell wires
            ActivityNavContext (see the M8.1 T4 report) — mirrors the NodeChip/ReviewOffenderChip
            no-provider degrade rather than rendering a dead-looking click target. */}
        {activityNav ? (
          <button
            type="button"
            onClick={() => activityNav.openCaptures()}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              fontSize: 12,
              fontWeight: 600,
              color: 'var(--accent)',
              cursor: 'pointer',
            }}
          >
            See all →
          </button>
        ) : (
          data &&
          data.length >= RECENTS_LIMIT && (
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>See all in Activity</span>
          )
        )}
      </div>

      {isLoading ? (
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>Loading…</p>
      ) : isError ? (
        <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>Couldn’t load recent captures.</p>
      ) : !data || data.length === 0 ? (
        <p style={{ margin: 0, fontSize: 14, color: 'var(--muted)' }}>
          Nothing yet. Your captures will appear here.
        </p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <AnimatePresence initial={false}>
            {data.map((c) => (
              <CaptureRow key={c.capture_id} capture={c} />
            ))}
          </AnimatePresence>
        </div>
      )}
    </section>
  );
}
