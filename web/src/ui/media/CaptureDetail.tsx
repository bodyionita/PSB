// The shared capture-detail surface (M9 T5, ADR-060 §7): raw text (fenced description / transcript),
// derivation status, source badge, the capture's media, and NodeChips to every node it produced — the
// node → capture → sibling-nodes traceability hop. `CaptureDetailBody` is presentational (takes an
// already-fetched capture); `CaptureDetailSheet` fetches by id and shows the body in a bottom sheet,
// opened by the NodePreview strip's "see raw capture". The SAME body renders in the Activity › Captures
// expanded row (FeedView composes it with the anchor editor), so the two surfaces never diverge.
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { CaptureView } from '../../api/types';
import { NodeRefChips } from '../NodeRefChips';
import { CaptureMediaBlock } from './CaptureMediaBlock';

const FAIL_COLOR = '#ff6b6b';

function SourceBadge({ source }: { source: string | null }) {
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.3,
        textTransform: 'uppercase',
        color: 'var(--muted)',
        border: '1px solid var(--surface-border)',
        borderRadius: 999,
        padding: '2px 8px',
      }}
    >
      {source ?? 'web'}
    </span>
  );
}

export function CaptureDetailBody({ capture }: { capture: CaptureView }) {
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <SourceBadge source={capture.source ?? capture.kind} />
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>{capture.status}</span>
      </div>
      {capture.media.map((m) => (
        <CaptureMediaBlock key={m.id} media={m} />
      ))}
      {capture.raw_text && (
        <p
          style={{
            margin: 0,
            minWidth: 0,
            fontSize: 13,
            color: 'var(--text)',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            // Break long unbroken tokens (URLs, ids) so raw text can't run under the card edge.
            overflowWrap: 'anywhere',
            wordBreak: 'break-word',
          }}
        >
          {capture.raw_text}
        </p>
      )}
      <NodeRefChips paths={capture.node_paths} refs={capture.node_refs} />
      {capture.error && <p style={{ margin: 0, fontSize: 12, color: FAIL_COLOR }}>{capture.error}</p>}
    </div>
  );
}

// Shares the exact `['captures','detail',id]` query key with FeedView's `useCapture`, so the sheet and
// the Activity row read one cache entry (no duplicate fetch when both have seen this capture).
function useCaptureDetail(id: string | null) {
  return useQuery({
    queryKey: ['captures', 'detail', id],
    queryFn: () => api.getCapture(id!),
    enabled: id != null,
  });
}

export function CaptureDetailSheet({
  captureId,
  onClose,
}: {
  captureId: string | null;
  onClose: () => void;
}) {
  const reduce = useReducedMotion();
  const { data, isLoading, isError } = useCaptureDetail(captureId);

  useEffect(() => {
    if (!captureId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [captureId, onClose]);

  // Portal to <body>: `position: fixed` sheet mounted inside transformed (framer-motion) capture
  // rows would otherwise be trapped in the row's containing block instead of the viewport.
  return createPortal(
    <AnimatePresence>
      {captureId && (
        <motion.div
          key="capture-sheet-backdrop"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          onClick={onClose}
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 55,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'flex-end',
          }}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Raw capture"
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: 40 }}
            transition={{ type: 'spring', stiffness: 420, damping: 38 }}
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'var(--bg)',
              borderTopLeftRadius: 20,
              borderTopRightRadius: 20,
              borderTop: '1px solid var(--surface-border)',
              maxHeight: '85dvh',
              overflow: 'auto',
              padding: '16px 20px calc(20px + env(safe-area-inset-bottom))',
              margin: '0 auto',
              width: '100%',
              maxWidth: 640,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
              <span style={{ flex: 1, fontSize: 15, fontWeight: 700, letterSpacing: -0.2 }}>
                Raw capture
              </span>
              <button
                type="button"
                aria-label="Close"
                onClick={onClose}
                style={{
                  flexShrink: 0,
                  width: 32,
                  height: 32,
                  borderRadius: 999,
                  border: '1px solid var(--surface-border)',
                  background: 'var(--surface)',
                  color: 'var(--muted)',
                  cursor: 'pointer',
                  fontSize: 14,
                }}
              >
                ✕
              </button>
            </div>

            {isLoading ? (
              <p style={{ margin: 0, fontSize: 13, color: 'var(--muted)' }}>Loading…</p>
            ) : isError || !data ? (
              <p style={{ margin: 0, fontSize: 13, color: FAIL_COLOR }}>Couldn’t load this capture.</p>
            ) : (
              <CaptureDetailBody capture={data} />
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
