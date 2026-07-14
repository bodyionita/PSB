import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import type { ChatSessionItem, ChatSourceItem } from '../../api/types';
import { NodePreview, PlaneBadge } from '../../ui/NodePreview';
import { baseName } from '../../ui/nodeDetail';
import { Surface } from '../../ui/Surface';
import { typeIcon, typeLabel } from '../../ui/nodeTypes';
// usePlanes is the canonical meta read (shared cache key ['planes']); reused here for the composer's
// retrieval-scoping chips rather than re-declared.
import { usePlanes } from '../search/useSearch';
import { useChatModels, useChatSession, useChatSessions, useSendChat } from './useChat';

// Chat tab (06 §2, ADR-025): ask across your memories, answers with cited [n] source cards. Client-
// side reveal over the non-streaming response, a per-conversation model picker + plane chips in the
// composer, a discreet fallback banner, and a "not from your memories" chip on ungrounded answers.
// List / open / new only in M4 (rename + delete are deferred — no endpoints yet).

const FAIL_COLOR = '#ff6b6b';

// One turn in the active thread. Local render state; the server persists the same content. `reveal`
// plays the client-side reveal on a freshly-arrived assistant turn (not on history). `fallbackUsed`
// is a live-response-only signal (not persisted), so it's absent on seeded history.
interface ThreadMessage {
  key: string;
  role: 'user' | 'assistant';
  content: string;
  model?: string | null;
  fallbackUsed?: boolean;
  sources: ChatSourceItem[];
  reveal: boolean;
  error?: boolean;
}

// --- Answer body: citation-aware tokens with a staggered reveal --------------------------------
type Unit = { kind: 'word'; value: string } | { kind: 'cite'; n: number };

// Split an answer into word tokens (word + trailing whitespace, so line breaks survive in a
// pre-wrap container) and inline `[n]` citation markers.
function toUnits(text: string): Unit[] {
  const units: Unit[] = [];
  const re = /\[(\d+)\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  // Keep leading + trailing whitespace on each token so spacing survives — including the space
  // that sits between a word and an adjacent `[n]` badge (`\S+\s*` alone would drop a chunk's
  // leading space, gluing "…SQS [1] and…" into "…[1]and…").
  const pushText = (chunk: string) => {
    for (const w of chunk.match(/\s*\S+\s*/g) ?? []) units.push({ kind: 'word', value: w });
  };
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) pushText(text.slice(last, m.index));
    units.push({ kind: 'cite', n: Number(m[1]) });
    last = re.lastIndex;
  }
  if (last < text.length) pushText(text.slice(last));
  return units;
}

const revealItem = {
  hidden: { opacity: 0 },
  show: { opacity: 1, transition: { duration: 0.18 } },
};

function CiteBadge({ n, onClick }: { n: number; onClick: () => void }) {
  return (
    <motion.button
      variants={revealItem}
      onClick={onClick}
      title={`Source ${n}`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        verticalAlign: 'baseline',
        minWidth: 18,
        height: 18,
        margin: '0 1px',
        padding: '0 5px',
        borderRadius: 6,
        border: '1px solid var(--accent)',
        background: 'transparent',
        color: 'var(--accent)',
        fontSize: 11,
        fontWeight: 700,
        lineHeight: 1,
        fontVariantNumeric: 'tabular-nums',
        cursor: 'pointer',
      }}
    >
      {n}
    </motion.button>
  );
}

// The answer text with inline citation badges. When `reveal` (and motion is allowed) the words +
// badges fade in with a bounded stagger; otherwise they render immediately (history / reduced
// motion). Clicking a badge expands + scrolls to the matching source card.
function AnswerBody({
  text,
  reveal,
  onCite,
}: {
  text: string;
  reveal: boolean;
  onCite: (n: number) => void;
}) {
  const reduced = useReducedMotion();
  const play = reveal && !reduced;
  const units = toUnits(text);
  const step = Math.min(0.02, 1.2 / Math.max(units.length, 1));

  return (
    <motion.div
      variants={{ hidden: {}, show: { transition: { staggerChildren: step } } }}
      initial={play ? 'hidden' : 'show'}
      animate="show"
      style={{
        margin: 0,
        fontSize: 15,
        lineHeight: 1.6,
        color: 'var(--text)',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {units.map((u, i) =>
        u.kind === 'cite' ? (
          <CiteBadge key={i} n={u.n} onClick={() => onCite(u.n)} />
        ) : (
          <motion.span key={i} variants={revealItem} style={{ whiteSpace: 'pre-wrap' }}>
            {u.value}
          </motion.span>
        ),
      )}
    </motion.div>
  );
}

// --- Source card: an expandable cited node (reuses the shared NodePreview on expand) ------------
function SourceCard({
  index,
  source,
  open,
  onToggle,
  cardRef,
}: {
  index: number;
  source: ChatSourceItem;
  open: boolean;
  onToggle: () => void;
  cardRef: (el: HTMLDivElement | null) => void;
}) {
  const title = source.title ?? baseName(source.store_path);
  const plane = source.planes[0] ?? null;
  return (
    <div ref={cardRef}>
      <Surface padding={14} style={{ borderRadius: 'var(--radius)' }}>
        <button
          onClick={onToggle}
          aria-expanded={open}
          style={{
            display: 'block',
            width: '100%',
            textAlign: 'left',
            background: 'transparent',
            border: 'none',
            padding: 0,
            color: 'inherit',
            cursor: 'pointer',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span
              aria-hidden
              style={{
                flexShrink: 0,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                minWidth: 20,
                height: 20,
                borderRadius: 6,
                background: 'var(--surface)',
                border: '1px solid var(--surface-border)',
                fontSize: 11,
                fontWeight: 700,
                color: 'var(--accent)',
              }}
            >
              {index}
            </span>
            <span aria-hidden title={typeLabel(source.type)} style={{ flexShrink: 0 }}>
              {typeIcon(source.type)}
            </span>
            <span
              style={{
                flex: 1,
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontSize: 14,
                fontWeight: 700,
                letterSpacing: -0.2,
              }}
            >
              {title}
            </span>
            <PlaneBadge plane={plane} />
          </div>
          <p
            style={{
              margin: '8px 0 0',
              fontSize: 13,
              lineHeight: 1.5,
              color: 'var(--muted)',
              display: '-webkit-box',
              WebkitLineClamp: open ? 'unset' : 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}
          >
            {source.snippet}
          </p>
        </button>
        <AnimatePresence initial={false}>
          {open && <NodePreview nodeId={source.node_id} />}
        </AnimatePresence>
      </Surface>
    </div>
  );
}

// --- Message bubbles ---------------------------------------------------------------------------
function UserBubble({ content }: { content: string }) {
  const reduced = useReducedMotion();
  return (
    <motion.div
      initial={{ opacity: 0, y: reduced ? 0 : 8 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ display: 'flex', justifyContent: 'flex-end' }}
    >
      <div
        style={{
          maxWidth: '85%',
          padding: '10px 14px',
          borderRadius: 16,
          borderBottomRightRadius: 4,
          background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
          color: 'var(--on-accent)',
          fontSize: 15,
          lineHeight: 1.5,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {content}
      </div>
    </motion.div>
  );
}

function GroundingChip() {
  return (
    <span
      title="This answer isn't grounded in your memories"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontSize: 11,
        fontWeight: 600,
        color: 'var(--muted)',
        border: '1px dashed var(--surface-border)',
        borderRadius: 999,
        padding: '3px 9px',
      }}
    >
      <span aria-hidden>○</span> not from your memories
    </span>
  );
}

function FallbackBanner({ model }: { model: string }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 11,
        color: 'var(--muted)',
      }}
    >
      <span aria-hidden>⤳</span> answered by {model}
    </div>
  );
}

function AssistantMessage({ msg }: { msg: ThreadMessage }) {
  const reduced = useReducedMotion();
  const [openSources, setOpenSources] = useState<ReadonlySet<number>>(new Set());
  const refs = useRef<Record<number, HTMLDivElement | null>>({});

  const toggleSource = (index: number) =>
    setOpenSources((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  // A citation click expands the matching card and scrolls it into view (source `[n]` is 1-based).
  const onCite = (n: number) => {
    if (n < 1 || n > msg.sources.length) return;
    setOpenSources((prev) => new Set(prev).add(n));
    refs.current[n]?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };

  if (msg.error) {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
        <p style={{ margin: 0, fontSize: 14, color: FAIL_COLOR }}>
          Chat is temporarily unavailable — your message was saved, try again in a moment.
        </p>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: reduced ? 0 : 8 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ display: 'flex', flexDirection: 'column', gap: 10, alignItems: 'flex-start' }}
    >
      <div style={{ width: '100%' }}>
        <AnswerBody text={msg.content} reveal={msg.reveal} onCite={onCite} />
      </div>

      {(msg.fallbackUsed || msg.sources.length === 0) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          {msg.sources.length === 0 && <GroundingChip />}
          {msg.fallbackUsed && msg.model && <FallbackBanner model={msg.model} />}
        </div>
      )}

      {msg.sources.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, width: '100%' }}>
          {msg.sources.map((s, i) => {
            const index = i + 1;
            return (
              <SourceCard
                key={`${index}:${s.node_id}`}
                index={index}
                source={s}
                open={openSources.has(index)}
                onToggle={() => toggleSource(index)}
                cardRef={(el) => {
                  refs.current[index] = el;
                }}
              />
            );
          })}
        </div>
      )}
    </motion.div>
  );
}

function ThinkingBubble() {
  const reduced = useReducedMotion();
  return (
    <div style={{ display: 'flex', gap: 5, alignItems: 'center', height: 20 }}>
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          // Reduced motion: hold a static row of dots rather than an endless opacity pulse.
          animate={reduced ? { opacity: 0.6 } : { opacity: [0.25, 1, 0.25] }}
          transition={reduced ? undefined : { duration: 1.1, repeat: Infinity, delay: i * 0.18 }}
          style={{ width: 6, height: 6, borderRadius: 999, background: 'var(--muted)' }}
        />
      ))}
    </div>
  );
}

// --- Session list ------------------------------------------------------------------------------
function sessionLabel(s: ChatSessionItem, fallback: string): string {
  return s.title ?? fallback;
}

function SessionList({
  sessions,
  activeId,
  activeFallback,
  onOpen,
}: {
  sessions: ChatSessionItem[];
  activeId: string | null;
  activeFallback: string;
  onOpen: (id: string) => void;
}) {
  if (sessions.length === 0) {
    return (
      <p style={{ margin: 0, padding: '4px 2px', fontSize: 13, color: 'var(--muted)' }}>
        No conversations yet.
      </p>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {sessions.map((s) => {
        const selected = s.id === activeId;
        // "First message until the title lands" (06 §2): for the active untitled thread we know its
        // first message locally; otherwise fall back to a neutral label.
        const fallback = selected ? activeFallback : 'New conversation';
        return (
          <button
            key={s.id}
            onClick={() => onOpen(s.id)}
            style={{
              display: 'block',
              width: '100%',
              textAlign: 'left',
              padding: '10px 12px',
              borderRadius: 'var(--radius)',
              border: selected ? '1px solid var(--accent)' : '1px solid var(--surface-border)',
              background: 'var(--surface)',
              color: 'var(--text)',
              fontSize: 14,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              cursor: 'pointer',
            }}
          >
            {sessionLabel(s, fallback)}
          </button>
        );
      })}
    </div>
  );
}

// --- Composer ----------------------------------------------------------------------------------
function ChipButton({
  on,
  label,
  onClick,
}: {
  on: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <motion.button
      onClick={onClick}
      whileTap={{ scale: 0.94 }}
      aria-pressed={on}
      style={{
        fontSize: 12,
        fontWeight: 600,
        padding: '5px 11px',
        borderRadius: 999,
        border: on ? '1px solid var(--accent)' : '1px solid var(--surface-border)',
        background: on ? 'var(--accent)' : 'transparent',
        color: on ? 'var(--on-accent)' : 'var(--muted)',
        cursor: 'pointer',
      }}
    >
      {label}
    </motion.button>
  );
}

const selectStyle: CSSProperties = {
  appearance: 'none',
  WebkitAppearance: 'none',
  padding: '7px 10px',
  borderRadius: 'var(--radius)',
  border: '1px solid var(--surface-border)',
  background: 'var(--surface)',
  color: 'var(--text)',
  fontSize: 13,
  maxWidth: 180,
  cursor: 'pointer',
};

// --- Screen ------------------------------------------------------------------------------------
export function ChatScreen() {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [thread, setThread] = useState<ThreadMessage[]>([]);
  const [input, setInput] = useState('');
  const [model, setModel] = useState<string | undefined>(undefined);
  const [planes, setPlanes] = useState<ReadonlySet<string>>(new Set());
  const [showList, setShowList] = useState(false);

  const keySeq = useRef(0);
  const seededFor = useRef<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const threadEndRef = useRef<HTMLDivElement>(null);

  const modelsQuery = useChatModels();
  const sessionsQuery = useChatSessions();
  const planesQuery = usePlanes();
  const detailQuery = useChatSession(activeId);
  const send = useSendChat();

  const models = modelsQuery.data;
  const selectedModel = model ?? models?.default ?? '';
  const allPlanes = planesQuery.data ? [...planesQuery.data.planes, planesQuery.data.inbox] : [];

  // Seed the thread once when a session is opened (click). The guard keeps optimistic turns from a
  // send — including a just-created session — from being wiped by the server refetch.
  useEffect(() => {
    const d = detailQuery.data;
    if (d && d.id === activeId && seededFor.current !== activeId) {
      setThread(
        d.messages.map((m, i) => ({
          key: `srv-${d.id}-${i}`,
          role: m.role,
          content: m.content,
          model: m.model,
          sources: m.sources,
          reveal: false,
        })),
      );
      seededFor.current = activeId;
    }
  }, [detailQuery.data, activeId]);

  // Keep the newest turn in view as the thread grows / the reveal plays.
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [thread.length, send.isPending]);

  const autosize = () => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  // A fresh or newly-opened thread starts at the Chat group's default model; the picker is a
  // per-conversation override that shouldn't leak from the previously-viewed thread (06 §2).
  const newChat = () => {
    seededFor.current = null;
    setActiveId(null);
    setThread([]);
    setModel(undefined);
    setShowList(false);
  };

  const openSession = (id: string) => {
    if (id !== activeId) setActiveId(id);
    setModel(undefined);
    setShowList(false);
  };

  const togglePlane = (p: string) =>
    setPlanes((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });

  const firstUserText = thread.find((m) => m.role === 'user')?.content ?? 'New conversation';

  const handleSend = async () => {
    const text = input.trim();
    if (!text || send.isPending) return;
    setThread((t) => [
      ...t,
      { key: `local-${keySeq.current++}`, role: 'user', content: text, sources: [], reveal: false },
    ]);
    setInput('');
    // Reset the textarea height after clearing (value change alone doesn't shrink it).
    requestAnimationFrame(autosize);

    try {
      const resp = await send.mutateAsync({
        message: text,
        ...(activeId ? { session_id: activeId } : {}),
        ...(model ? { model } : {}),
        ...(planes.size ? { planes: [...planes] } : {}),
      });
      // Implicit session creation: adopt the id and mark it seeded so the detail refetch won't wipe
      // the optimistic turns.
      if (!activeId) {
        seededFor.current = resp.session_id;
        setActiveId(resp.session_id);
      }
      setThread((t) => [
        ...t,
        {
          key: `local-${keySeq.current++}`,
          role: 'assistant',
          content: resp.answer,
          model: resp.model_used,
          fallbackUsed: resp.fallback_used,
          sources: resp.sources,
          reveal: true,
        },
      ]);
    } catch {
      // The user turn is already durably persisted server-side (rule 2) even on a 503; surface a
      // retry notice without dropping their message.
      setThread((t) => [
        ...t,
        {
          key: `local-${keySeq.current++}`,
          role: 'assistant',
          content: '',
          sources: [],
          reveal: false,
          error: true,
        },
      ]);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void handleSend();
    }
  };

  const sessions = sessionsQuery.data ?? [];
  const empty = thread.length === 0 && !send.isPending;

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, letterSpacing: -0.4, flex: 1 }}>
          Chat
        </h1>
        <ChipButton
          on={showList}
          label={`History${sessions.length ? ` · ${sessions.length}` : ''}`}
          onClick={() => setShowList((v) => !v)}
        />
        <ChipButton on={false} label="＋ New" onClick={newChat} />
      </header>

      <AnimatePresence initial={false}>
        {showList && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            style={{ overflow: 'hidden' }}
          >
            <Surface padding={12}>
              <SessionList
                sessions={sessions}
                activeId={activeId}
                activeFallback={firstUserText}
                onOpen={openSession}
              />
            </Surface>
          </motion.div>
        )}
      </AnimatePresence>

      <section style={{ display: 'flex', flexDirection: 'column', gap: 16, minHeight: 200 }}>
        {empty ? (
          <Surface>
            <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.6 }}>
              Ask anything across your memories. Answers cite the nodes they draw from; a reply with
              no citations is marked <em>not from your memories</em>.
            </p>
          </Surface>
        ) : (
          thread.map((m) =>
            m.role === 'user' ? (
              <UserBubble key={m.key} content={m.content} />
            ) : (
              <AssistantMessage key={m.key} msg={m} />
            ),
          )
        )}
        {send.isPending && <ThinkingBubble />}
        <div ref={threadEndRef} />
      </section>

      <Surface padding={12} style={{ position: 'sticky', bottom: 88, display: 'grid', gap: 10 }}>
        {allPlanes.length > 0 && (
          <div role="group" aria-label="Scope retrieval by plane" style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {allPlanes.map((p) => (
              <ChipButton key={p} on={planes.has(p)} label={p} onClick={() => togglePlane(p)} />
            ))}
          </div>
        )}

        <textarea
          ref={taRef}
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            autosize();
          }}
          onKeyDown={onKeyDown}
          rows={1}
          placeholder="Ask your brain…"
          aria-label="Message"
          style={{
            width: '100%',
            resize: 'none',
            minHeight: 44,
            maxHeight: 160,
            padding: '11px 14px',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--surface-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
            fontSize: 15,
            fontFamily: 'inherit',
            lineHeight: 1.5,
            outline: 'none',
          }}
        />

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <select
            value={selectedModel}
            onChange={(e) => setModel(e.target.value)}
            aria-label="Model"
            disabled={!models || models.models.length === 0}
            style={selectStyle}
          >
            {(models?.models ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
          <div style={{ flex: 1 }} />
          <motion.button
            onClick={() => void handleSend()}
            whileTap={{ scale: 0.95 }}
            disabled={input.trim() === '' || send.isPending}
            style={{
              padding: '10px 20px',
              borderRadius: 'var(--radius)',
              border: 'none',
              background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
              color: 'var(--on-accent)',
              fontSize: 15,
              fontWeight: 600,
              opacity: input.trim() === '' || send.isPending ? 0.5 : 1,
              cursor: input.trim() === '' || send.isPending ? 'default' : 'pointer',
            }}
          >
            Send
          </motion.button>
        </div>
      </Surface>
    </div>
  );
}
