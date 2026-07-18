import { AnimatePresence, motion, useReducedMotion, useTransform } from 'framer-motion';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from 'react';
import { mediaUrl } from '../../api/client';
import type { DraftPart } from '../../api/types';
import { RecentCaptures } from './RecentCaptures';
import { toUploadable } from './heicConvert';
import {
  useAddDraftPart,
  useDiscardDraft,
  useDraft,
  useEditDraftText,
  useRemoveDraftPart,
  useSubmitDraft,
} from './useCaptures';
import { useRecorder } from './useRecorder';

const FAIL_COLOR = '#ff6b6b';

// Capture is the hero action (06-web-app.md), now a **composite compose** surface (M9.6, ADR-061):
// one capture carries a typed note + 0..N photos + <=1 voice, attached to a server-side draft
// (resumable across app-close) and organized together on Send. The record orb, text field, and
// photo affordance all feed the SAME draft; Send commits it.
export function CaptureScreen() {
  const reduced = useReducedMotion();
  const recorder = useRecorder();

  const draftQuery = useDraft(true);
  const draft = draftQuery.data;
  const draftId = draft?.capture_id ?? null;
  const parts = draft?.parts ?? [];
  const hasVoice = parts.some((p) => p.kind === 'voice');

  const addPart = useAddDraftPart();
  const removePart = useRemoveDraftPart();
  const editText = useEditDraftText();
  const submit = useSubmitDraft();
  const discard = useDiscardDraft();

  // Local text mirror of the draft's text_body, seeded once the draft loads (resume). Edits are
  // debounce-saved to the draft (never-lose + resume), and flushed before Send.
  const [text, setText] = useState('');
  const [seeded, setSeeded] = useState(false);
  useEffect(() => {
    if (draft && !seeded) {
      setText(draft.text_body ?? '');
      setSeeded(true);
    }
  }, [draft, seeded]);
  useEffect(() => {
    if (!draft || !seeded) return;
    if ((draft.text_body ?? '') === text) return;
    const t = window.setTimeout(() => editText.mutate({ id: draft.capture_id, text }), 700);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, seeded]);

  // Photo picking (multi): normalize each pick (HEIC→JPEG) then attach as a draft part.
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [converting, setConverting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pickImages = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files ?? []);
      e.target.value = '';
      if (!files.length || !draftId) return;
      setError(null);
      setConverting(true);
      try {
        for (const file of files) {
          const upload = await toUploadable(file);
          await addPart.mutateAsync({
            id: draftId,
            blob: upload.blob,
            filename: upload.filename,
            kind: 'photo',
          });
        }
      } catch {
        setError('Couldn’t add that photo — try again.');
      } finally {
        setConverting(false);
      }
    },
    [addPart, draftId],
  );

  const recording = recorder.state === 'recording';
  const orbScale = useTransform(recorder.level, (l) => 1 + l * 0.42);
  const glowOpacity = useTransform(recorder.level, (l) => 0.35 + l * 0.5);

  const toggleRecord = useCallback(async () => {
    if (!draftId || hasVoice || addPart.isPending) return;
    if (recording) {
      const result = await recorder.stop();
      if (result) {
        setError(null);
        addPart.mutate(
          { id: draftId, blob: result.blob, filename: result.filename, kind: 'voice' },
          { onError: () => setError('Couldn’t add that voice note — try again.') },
        );
      }
      return;
    }
    void recorder.start();
  }, [addPart, draftId, hasVoice, recorder, recording]);

  const [confirming, setConfirming] = useState(false);
  const confirmTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (confirmTimer.current !== null) clearTimeout(confirmTimer.current);
    },
    [],
  );

  const canSend = !!(text.trim() || parts.length);
  const busy = submit.isPending || addPart.isPending || converting;

  const onSend = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      if (!draftId || !canSend || busy) return;
      try {
        // Flush any un-saved text, then commit the draft in one blended organize.
        if ((draft?.text_body ?? '') !== text) {
          await editText.mutateAsync({ id: draftId, text });
        }
        await submit.mutateAsync(draftId);
        setText('');
        setSeeded(false); // a fresh draft opens on the next fetch; re-seed from it
        setConfirming(true);
        if (confirmTimer.current !== null) clearTimeout(confirmTimer.current);
        confirmTimer.current = window.setTimeout(() => setConfirming(false), 1600);
      } catch {
        setError('Couldn’t send that — try again.');
      }
    },
    [busy, canSend, draft, draftId, editText, submit, text],
  );

  const onDiscard = useCallback(() => {
    if (!draftId) return;
    discard.mutate(draftId, {
      onSuccess: () => {
        setText('');
        setSeeded(false);
      },
    });
  }, [discard, draftId]);

  const heading = recording ? 'Listening…' : confirming ? 'Captured' : "What's on your mind?";
  const subtitle = recording
    ? 'Tap to finish.'
    : hasVoice
      ? 'Add a note or photos, then Send.'
      : 'Tap to speak, type, or attach photos.';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 24 }}>
      <div style={{ textAlign: 'center' }}>
        <h1 style={{ margin: '0 0 6px', fontSize: 26, fontWeight: 700, letterSpacing: -0.4 }}>
          {heading}
        </h1>
        <p style={{ margin: 0, color: 'var(--muted)' }}>{subtitle}</p>
      </div>

      {/* Record orb — attaches a voice part to the draft. Disabled once one voice is attached. */}
      <div style={{ position: 'relative', width: 220, height: 220, display: 'grid', placeItems: 'center' }}>
        {!reduced && !hasVoice && (
          <motion.div
            aria-hidden
            animate={
              recording ? { scale: 1, opacity: 0.6 } : { scale: [1, 1.12, 1], opacity: [0.5, 0.22, 0.5] }
            }
            transition={recording ? { duration: 0.3 } : { duration: 3.4, repeat: Infinity, ease: 'easeInOut' }}
            style={{
              position: 'absolute',
              width: 220,
              height: 220,
              borderRadius: '50%',
              background: 'radial-gradient(circle, var(--accent), transparent 65%)',
            }}
          />
        )}
        {recording && (
          <motion.div
            aria-hidden
            style={{
              position: 'absolute',
              width: 186,
              height: 186,
              borderRadius: '50%',
              background: 'radial-gradient(circle, var(--accent), transparent 70%)',
              scale: orbScale,
              opacity: glowOpacity,
            }}
          />
        )}
        <motion.button
          onClick={toggleRecord}
          whileTap={{ scale: 0.92 }}
          whileHover={{ scale: hasVoice ? 1 : 1.03 }}
          transition={{ type: 'spring', stiffness: 400, damping: 22 }}
          aria-label={recording ? 'Stop recording' : 'Record a voice note'}
          aria-pressed={recording}
          disabled={!draftId || hasVoice}
          style={{
            position: 'relative',
            width: 136,
            height: 136,
            borderRadius: '50%',
            border: hasVoice ? '1px dashed var(--surface-border)' : 'none',
            background: hasVoice
              ? 'var(--surface)'
              : 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            boxShadow: hasVoice ? 'none' : '0 20px 60px -18px var(--accent)',
            color: hasVoice ? 'var(--muted)' : 'var(--on-accent)',
            fontSize: recording ? 28 : 40,
            display: 'grid',
            placeItems: 'center',
            cursor: !draftId || hasVoice ? 'default' : 'pointer',
          }}
        >
          <AnimatePresence mode="wait" initial={false}>
            {confirming ? (
              <motion.span key="check" initial={{ scale: 0.4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.4, opacity: 0 }} transition={{ type: 'spring', stiffness: 500, damping: 20 }} style={{ fontSize: 48 }}>
                ✓
              </motion.span>
            ) : hasVoice ? (
              <motion.span key="hasvoice" initial={{ opacity: 0 }} animate={{ opacity: 1 }} style={{ fontSize: 15, fontWeight: 600 }}>
                Voice added
              </motion.span>
            ) : (
              <motion.span key={recording ? 'stop' : 'mic'} initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.6, opacity: 0 }} transition={{ duration: 0.15 }}>
                {recording ? '■' : '●'}
              </motion.span>
            )}
          </AnimatePresence>
        </motion.button>
      </div>

      {recorder.error && (
        <p style={{ margin: '-8px 0 0', fontSize: 13, color: FAIL_COLOR, textAlign: 'center', maxWidth: 320 }}>
          {recorder.error}
        </p>
      )}

      {/* Attached parts tray (photos + the voice chip), each removable with an 'x'. */}
      {parts.length > 0 && (
        <div style={{ width: '100%', maxWidth: 480, display: 'flex', flexWrap: 'wrap', gap: 10 }}>
          <AnimatePresence initial={false}>
            {parts.map((p) => (
              <PartTile
                key={p.id}
                part={p}
                onRemove={() => draftId && removePart.mutate({ id: draftId, mediaId: p.id })}
              />
            ))}
          </AnimatePresence>
        </div>
      )}

      {/* Text + photo attach + Send. */}
      <form onSubmit={onSend} style={{ display: 'flex', gap: 8, width: '100%', maxWidth: 480 }}>
        <input
          ref={fileInput}
          type="file"
          accept="image/*,.heic,.heif"
          multiple
          onChange={pickImages}
          style={{ display: 'none' }}
        />
        <motion.button
          type="button"
          onClick={() => fileInput.current?.click()}
          whileTap={{ scale: 0.95 }}
          disabled={!draftId || converting}
          aria-label="Add photos"
          style={{
            flexShrink: 0,
            width: 48,
            padding: 0,
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: 18,
            opacity: !draftId || converting ? 0.5 : 1,
            cursor: !draftId || converting ? 'default' : 'pointer',
          }}
        >
          {converting ? '◌' : '❏'}
        </motion.button>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Add a note…"
          disabled={!draftId}
          style={{
            flex: 1,
            padding: '13px 16px',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: 15,
            outline: 'none',
          }}
        />
        <motion.button
          type="submit"
          whileTap={{ scale: 0.95 }}
          disabled={!canSend || busy}
          style={{
            padding: '0 22px',
            borderRadius: 'var(--radius)',
            border: 'none',
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: 'var(--on-accent)',
            fontSize: 15,
            fontWeight: 600,
            opacity: !canSend || busy ? 0.5 : 1,
          }}
        >
          {busy ? '◌' : 'Send'}
        </motion.button>
      </form>

      {(text.trim() !== '' || parts.length > 0) && (
        <button
          type="button"
          onClick={onDiscard}
          disabled={discard.isPending}
          style={{
            margin: '-14px 0 0',
            background: 'none',
            border: 'none',
            padding: 0,
            fontSize: 12,
            fontWeight: 600,
            color: 'var(--muted)',
            cursor: 'pointer',
          }}
        >
          Discard draft
        </button>
      )}

      {error && (
        <p style={{ margin: '-14px 0 0', fontSize: 13, color: FAIL_COLOR, textAlign: 'center', maxWidth: 320 }}>
          {error}
        </p>
      )}

      <div style={{ width: '100%', maxWidth: 480, marginTop: 8 }}>
        <RecentCaptures />
      </div>
    </div>
  );
}

// One attached draft part: a photo thumbnail (raw bytes stream even pre-derivation) or a voice
// chip, with an 'x' to remove it (hard-removes raw + row server-side, ADR-061 §3).
function PartTile({ part, onRemove }: { part: DraftPart; onRemove: () => void }) {
  const isVoice = part.kind === 'voice';
  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.85 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.85 }}
      transition={{ type: 'spring', stiffness: 420, damping: 30 }}
      style={{
        position: 'relative',
        width: isVoice ? 'auto' : 72,
        height: 72,
        borderRadius: 'var(--radius)',
        overflow: 'hidden',
        border: '1px solid var(--surface-border)',
        background: 'var(--surface)',
        display: 'grid',
        placeItems: 'center',
        padding: isVoice ? '0 16px' : 0,
      }}
    >
      {isVoice ? (
        <span style={{ fontSize: 13, color: 'var(--text)', whiteSpace: 'nowrap' }}>◉ Voice note</span>
      ) : (
        <img
          src={mediaUrl(part.id)}
          alt="Attached photo"
          style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label="Remove part"
        style={{
          position: 'absolute',
          top: 3,
          right: 3,
          width: 20,
          height: 20,
          borderRadius: '50%',
          border: 'none',
          background: 'rgba(0,0,0,0.6)',
          color: '#fff',
          fontSize: 12,
          lineHeight: 1,
          cursor: 'pointer',
          display: 'grid',
          placeItems: 'center',
        }}
      >
        ✕
      </button>
    </motion.div>
  );
}
