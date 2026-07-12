// Voice recording + a live amplitude signal for the visualizer (06 / 08 M1: record button +
// Web-Audio AnalyserNode). MediaRecorder captures the audio; an AnalyserNode reads the mic RMS
// each frame and pushes it into a MotionValue, so the orb reacts at 60fps without React re-renders.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useMotionValue, type MotionValue } from 'framer-motion';

export type RecorderState = 'idle' | 'recording' | 'error';

export interface RecordedAudio {
  blob: Blob;
  filename: string;
}

export interface Recorder {
  state: RecorderState;
  error: string | null;
  level: MotionValue<number>; // 0..1 live mic amplitude
  start: () => Promise<void>;
  stop: () => Promise<RecordedAudio | null>;
}

// Server accepts m4a/webm/ogg/mp3/wav (03-api). Pick the first container the browser can record
// (Chrome/Firefox → webm, Safari → mp4/m4a). Empty mimeType lets the UA choose its default.
function pickContainer(): { mimeType: string; ext: string } {
  const candidates = [
    { mimeType: 'audio/webm', ext: 'webm' },
    { mimeType: 'audio/mp4', ext: 'm4a' },
    { mimeType: 'audio/ogg', ext: 'ogg' },
  ];
  if (typeof MediaRecorder !== 'undefined') {
    for (const c of candidates) {
      if (MediaRecorder.isTypeSupported(c.mimeType)) return c;
    }
  }
  return { mimeType: '', ext: 'webm' };
}

export function useRecorder(): Recorder {
  const [state, setState] = useState<RecorderState>('idle');
  const [error, setError] = useState<string | null>(null);
  const level = useMotionValue(0);

  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const extRef = useRef<string>('webm');

  const teardown = useCallback(() => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (ctxRef.current && ctxRef.current.state !== 'closed') void ctxRef.current.close();
    ctxRef.current = null;
    recorderRef.current = null;
    level.set(0);
  }, [level]);

  // Release the mic if the screen unmounts mid-recording (e.g. tab switch).
  useEffect(() => teardown, [teardown]);

  const startMeter = useCallback(
    (stream: MediaStream) => {
      try {
        const AudioCtx =
          window.AudioContext ??
          (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        ctxRef.current = ctx;
        // Chrome starts the context suspended until a gesture; start() runs inside the tap, so
        // resuming here is allowed and keeps the visualizer alive.
        void ctx.resume();
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.8;
        source.connect(analyser);
        const buf = new Uint8Array(analyser.fftSize);
        const tick = () => {
          analyser.getByteTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i++) {
            const v = (buf[i]! - 128) / 128;
            sum += v * v;
          }
          const rms = Math.sqrt(sum / buf.length);
          // Perceptual boost so quiet speech still visibly moves the orb; clamp to 0..1.
          level.set(Math.min(1, rms * 3.2));
          rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);
      } catch {
        // The visualizer is non-essential; recording works without it.
      }
    },
    [level],
  );

  const start = useCallback(async () => {
    if (state === 'recording') return;
    setError(null);
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Recording needs a secure (https) connection.');
      setState('error');
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError('Microphone access was blocked. Enable it to record voice notes.');
      setState('error');
      return;
    }
    streamRef.current = stream;

    const container = pickContainer();
    extRef.current = container.ext;
    let recorder: MediaRecorder;
    try {
      recorder = container.mimeType
        ? new MediaRecorder(stream, { mimeType: container.mimeType })
        : new MediaRecorder(stream);
    } catch {
      recorder = new MediaRecorder(stream);
    }
    chunksRef.current = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorderRef.current = recorder;
    recorder.start();

    startMeter(stream);
    setState('recording');
  }, [startMeter, state]);

  const stop = useCallback(async (): Promise<RecordedAudio | null> => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === 'inactive') {
      teardown();
      setState('idle');
      return null;
    }
    const blob = await new Promise<Blob>((resolve) => {
      recorder.onstop = () => {
        const type = recorder.mimeType || chunksRef.current[0]?.type || 'audio/webm';
        resolve(new Blob(chunksRef.current, { type }));
      };
      recorder.stop();
    });
    teardown();
    setState('idle');
    if (blob.size === 0) return null;
    return { blob, filename: `capture.${extRef.current}` };
  }, [teardown]);

  return { state, error, level, start, stop };
}
