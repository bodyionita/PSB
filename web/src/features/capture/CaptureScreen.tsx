import { AnimatePresence, motion, useReducedMotion, useTransform } from 'framer-motion';
import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { RecentCaptures } from './RecentCaptures';
import { toUploadable } from './heicConvert';
import { useCaptureImage, useCaptureText, useCaptureVoice } from './useCaptures';
import { useRecorder } from './useRecorder';

const FAIL_COLOR = '#ff6b6b';

// Capture is the hero action (06-web-app.md): oversized record button with a living, voice-
// reactive visualizer, a quick text field, and a satisfying confirmation when a capture lands.
export function CaptureScreen() {
  const reduced = useReducedMotion();
  const recorder = useRecorder();
  const voice = useCaptureVoice();
  const textCapture = useCaptureText();
  const image = useCaptureImage();

  // Photo capture (M9 T5, ADR-057 §6 / ADR-060 §8): a hidden file input opened by the photo button.
  // `converting` covers the lazy HEIC→JPEG step; `imageError` surfaces a conversion/upload failure.
  const fileInput = useRef<HTMLInputElement | null>(null);
  const [converting, setConverting] = useState(false);
  const [imageError, setImageError] = useState<string | null>(null);
  const uploadingImage = image.isPending || converting;

  const recording = recorder.state === 'recording';
  const uploading = voice.isPending;

  // Orb scales with live mic amplitude while recording; steady otherwise.
  const orbScale = useTransform(recorder.level, (l) => 1 + l * 0.42);
  const glowOpacity = useTransform(recorder.level, (l) => 0.35 + l * 0.5);

  // Brief "captured ✓" flash after any successful submit.
  const [confirming, setConfirming] = useState(false);
  const confirmTimer = useRef<number | null>(null);
  const flashCaptured = useCallback(() => {
    setConfirming(true);
    if (confirmTimer.current !== null) clearTimeout(confirmTimer.current);
    confirmTimer.current = window.setTimeout(() => setConfirming(false), 1600);
  }, []);
  useEffect(
    () => () => {
      if (confirmTimer.current !== null) clearTimeout(confirmTimer.current);
    },
    [],
  );

  const toggleRecord = useCallback(async () => {
    if (uploading) return;
    if (recording) {
      const result = await recorder.stop();
      if (result) voice.mutate(result, { onSuccess: flashCaptured });
      return;
    }
    void recorder.start();
  }, [flashCaptured, recorder, recording, uploading, voice]);

  const pickImage = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      e.target.value = ''; // let the same file be re-picked after an error
      if (!file) return;
      setImageError(null);
      setConverting(true);
      try {
        const upload = await toUploadable(file);
        setConverting(false);
        image.mutate(upload, {
          onSuccess: flashCaptured,
          onError: () => setImageError('Couldn’t send that photo — try again.'),
        });
      } catch {
        setConverting(false);
        setImageError('Couldn’t read that photo — try a different one.');
      }
    },
    [flashCaptured, image],
  );

  const [text, setText] = useState('');
  const submitText = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || textCapture.isPending) return;
    textCapture.mutate(trimmed, {
      onSuccess: () => {
        setText('');
        flashCaptured();
      },
    });
  };

  const heading = recording ? 'Listening…' : uploading ? 'Sending…' : "What's on your mind?";
  const subtitle = recording ? 'Tap to finish.' : 'Tap to speak, or type below.';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 28 }}>
      <div style={{ textAlign: 'center' }}>
        <h1 style={{ margin: '0 0 6px', fontSize: 26, fontWeight: 700, letterSpacing: -0.4 }}>
          {heading}
        </h1>
        <p style={{ margin: 0, color: 'var(--muted)' }}>{subtitle}</p>
      </div>

      {/* Record orb */}
      <div style={{ position: 'relative', width: 240, height: 240, display: 'grid', placeItems: 'center' }}>
        {/* Breathing halo — autonomous, so it quiets under reduced motion. */}
        {!reduced && (
          <motion.div
            aria-hidden
            animate={recording ? { scale: 1, opacity: 0.6 } : { scale: [1, 1.14, 1], opacity: [0.5, 0.22, 0.5] }}
            transition={
              recording
                ? { duration: 0.3 }
                : { duration: 3.4, repeat: Infinity, ease: 'easeInOut' }
            }
            style={{
              position: 'absolute',
              width: 240,
              height: 240,
              borderRadius: '50%',
              background: 'radial-gradient(circle, var(--accent), transparent 65%)',
            }}
          />
        )}

        {/* Voice-reactive glow behind the button while recording. */}
        {recording && (
          <motion.div
            aria-hidden
            style={{
              position: 'absolute',
              width: 200,
              height: 200,
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
          whileHover={{ scale: 1.03 }}
          transition={{ type: 'spring', stiffness: 400, damping: 22 }}
          aria-label={recording ? 'Stop recording' : 'Record a voice capture'}
          aria-pressed={recording}
          disabled={uploading}
          style={{
            position: 'relative',
            width: 148,
            height: 148,
            borderRadius: '50%',
            border: 'none',
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            boxShadow: '0 20px 60px -18px var(--accent)',
            color: 'var(--on-accent)',
            fontSize: recording ? 30 : 44,
            display: 'grid',
            placeItems: 'center',
          }}
        >
          <AnimatePresence mode="wait" initial={false}>
            {confirming ? (
              <motion.span
                key="check"
                initial={{ scale: 0.4, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.4, opacity: 0 }}
                transition={{ type: 'spring', stiffness: 500, damping: 20 }}
                style={{ fontSize: 52 }}
              >
                ✓
              </motion.span>
            ) : uploading ? (
              <motion.span
                key="upload"
                aria-hidden
                animate={reduced ? undefined : { rotate: 360 }}
                transition={{ duration: 0.9, repeat: Infinity, ease: 'linear' }}
                style={{ fontSize: 30 }}
              >
                ◌
              </motion.span>
            ) : (
              <motion.span
                key={recording ? 'stop' : 'mic'}
                initial={{ scale: 0.6, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.6, opacity: 0 }}
                transition={{ duration: 0.15 }}
              >
                {recording ? '■' : '●'}
              </motion.span>
            )}
          </AnimatePresence>
        </motion.button>
      </div>

      {(recorder.error || voice.isError) && (
        <p style={{ margin: '-8px 0 0', fontSize: 13, color: FAIL_COLOR, textAlign: 'center', maxWidth: 320 }}>
          {recorder.error ?? 'Couldn’t send that voice note — try again.'}
        </p>
      )}

      {/* Quick text capture + a photo affordance (M9 T5, ADR-057 §6). */}
      <form onSubmit={submitText} style={{ display: 'flex', gap: 8, width: '100%', maxWidth: 480 }}>
        <input
          ref={fileInput}
          type="file"
          accept="image/*,.heic,.heif"
          onChange={pickImage}
          style={{ display: 'none' }}
        />
        <motion.button
          type="button"
          onClick={() => fileInput.current?.click()}
          whileTap={{ scale: 0.95 }}
          disabled={uploadingImage}
          aria-label="Add a photo"
          style={{
            flexShrink: 0,
            width: 48,
            padding: 0,
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: 18,
            opacity: uploadingImage ? 0.5 : 1,
            cursor: uploadingImage ? 'default' : 'pointer',
          }}
        >
          {uploadingImage ? '◌' : '❏'}
        </motion.button>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Jot a quick thought…"
          disabled={textCapture.isPending}
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
          disabled={textCapture.isPending || text.trim() === ''}
          style={{
            padding: '0 20px',
            borderRadius: 'var(--radius)',
            border: 'none',
            background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            color: 'var(--on-accent)',
            fontSize: 15,
            fontWeight: 600,
            opacity: textCapture.isPending || text.trim() === '' ? 0.5 : 1,
          }}
        >
          Add
        </motion.button>
      </form>

      {textCapture.isError && (
        <p style={{ margin: '-16px 0 0', fontSize: 13, color: FAIL_COLOR }}>
          Couldn’t save that — try again.
        </p>
      )}

      {imageError && (
        <p style={{ margin: '-16px 0 0', fontSize: 13, color: FAIL_COLOR, textAlign: 'center', maxWidth: 320 }}>
          {imageError}
        </p>
      )}

      <div style={{ width: '100%', maxWidth: 480, marginTop: 8 }}>
        <RecentCaptures />
      </div>
    </div>
  );
}
