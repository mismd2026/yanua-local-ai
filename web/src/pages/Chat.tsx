// S-10 チャット。社内文書への質問（出典つきの回答）と、ヘルプデスクの会話（お困りごと〜問い合わせ票）を同じ画面で
import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ApiError, AskResult, ChatReply, DocumentBody, Me, Source } from "../api";

type Mode = "ask" | "helpdesk";

interface Message {
  id: number;
  from: "me" | "ai" | "info";
  text: string;
  sources?: Source[];
  refused?: boolean;
  note?: string;
}

const MAX_TEXT = 2000;
let nextId = 1;
const msg = (m: Omit<Message, "id">): Message => ({ id: nextId++, ...m });

export default function Chat({ me }: { me: Me }) {
  const canAsk = me.permissions.includes("ask");
  const canHelpdesk = me.permissions.includes("helpdesk");
  const [mode, setMode] = useState<Mode>(canAsk ? "ask" : "helpdesk");
  const [opened, setOpened] = useState<Source | null>(null);

  if (!canAsk && !canHelpdesk) {
    return <section className="card"><p className="muted">チャットを使う権限がありません。</p></section>;
  }

  return (
    <div className={opened ? "chat-page with-panel" : "chat-page"}>
      <div className="chat-main">
        <div className="tabs" role="tablist" aria-label="相談の種類">
          {canAsk && (
            <button role="tab" aria-selected={mode === "ask"} className={mode === "ask" ? "tab on" : "tab"} onClick={() => setMode("ask")}>
              社内文書に質問
            </button>
          )}
          {canHelpdesk && (
            <button role="tab" aria-selected={mode === "helpdesk"} className={mode === "helpdesk" ? "tab on" : "tab"} onClick={() => setMode("helpdesk")}>
              ヘルプデスクに相談
            </button>
          )}
        </div>
        {/* 切り替えても会話が消えないよう、両方を置いたまま隠す */}
        {canAsk && <div hidden={mode !== "ask"}><AskChat onOpen={setOpened} /></div>}
        {canHelpdesk && <div hidden={mode !== "helpdesk"}><HelpdeskChat onOpen={setOpened} /></div>}
      </div>
      {opened && <SourcePanel source={opened} onClose={() => setOpened(null)} />}
    </div>
  );
}

// ------------------------------------------------------------ 社内文書に質問

function AskChat({ onOpen }: { onOpen: (s: Source) => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);

  const send = async (text: string) => {
    setMessages((m) => [...m, msg({ from: "me", text })]);
    setBusy(true);
    try {
      const r = await api<AskResult>("POST", "/api/ask", { question: text });
      setMessages((m) => [
        ...m,
        msg({ from: "ai", text: r.answer, sources: r.sources, refused: r.refused, note: `${r.elapsed_sec}秒` }),
      ]);
    } catch (e) {
      setMessages((m) => [...m, msg({ from: "info", text: errorText(e) })]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Log
        messages={messages}
        empty="社内の規程や手順について質問してください。答えには、もとになった文書（出典）を付けます。"
        busy={busy}
        onOpen={onOpen}
      />
      <Composer disabled={busy} placeholder="例：在宅勤務は週に何日までできますか？" onSend={send} />
    </>
  );
}

// ------------------------------------------------------------ ヘルプデスク

function HelpdeskChat({ onOpen }: { onOpen: (s: Source) => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [session, setSession] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const handle = (r: ChatReply) => {
    const note = r.finished && r.ticket_id ? `受付番号 ${r.ticket_id}` : undefined;
    setMessages((m) => [...m, msg({ from: "ai", text: r.reply, sources: r.finished ? r.sources : undefined, note })]);
    if (r.finished) {
      setSession(null);
      setMessages((m) => [...m, msg({ from: "info", text: "相談が終わりました。" })]);
    } else {
      setSession(r.session_id);
    }
  };

  const start = async () => {
    setBusy(true);
    setMessages([]);
    try {
      handle(await api<ChatReply>("POST", "/api/chat/sessions", { flow: "helpdesk" }));
    } catch (e) {
      setMessages([msg({ from: "info", text: errorText(e) })]);
    } finally {
      setBusy(false);
    }
  };

  const send = async (text: string) => {
    if (!session) return;
    setMessages((m) => [...m, msg({ from: "me", text })]);
    setBusy(true);
    try {
      handle(await api<ChatReply>("POST", `/api/chat/sessions/${encodeURIComponent(session)}/messages`, { text }));
    } catch (e) {
      // 会話が見つからない（時間が切れて消えた）・もう終わっているときは、新しく始めてもらう
      if (e instanceof ApiError && (e.status === 404 || e.code === "finished")) {
        setSession(null);
        setMessages((m) => [
          ...m,
          msg({ from: "info", text: e.status === 404 ? "しばらく操作がなかったため、この相談は終わりました。新しく始めてください。" : e.message }),
        ]);
      } else {
        setMessages((m) => [...m, msg({ from: "info", text: errorText(e) })]);
      }
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!session) return;
    const sid = session;
    setSession(null);
    try {
      await api("DELETE", `/api/chat/sessions/${encodeURIComponent(sid)}`);
    } catch {
      // すでに消えていても、画面の上ではやめたことにする
    }
    setMessages((m) => [...m, msg({ from: "info", text: "相談をやめました。" })]);
  };

  return (
    <>
      <Log
        messages={messages}
        empty="パソコン・ネットワーク・印刷機・電話機などのお困りごとを、情報システム部のヘルプデスクに相談できます。解決しないときは問い合わせ票を作って担当者に引き継ぎます。"
        busy={busy}
        onOpen={onOpen}
      />
      {session ? (
        <>
          <Composer disabled={busy} placeholder="返事を入れてください" onSend={send} />
          <div className="actions">
            <button className="link" onClick={() => void stop()} disabled={busy}>相談をやめる</button>
          </div>
        </>
      ) : (
        <div className="actions">
          <button className="primary" onClick={() => void start()} disabled={busy}>
            {messages.length ? "新しく相談する" : "相談を始める"}
          </button>
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------------ 共通の部品

function Log({ messages, empty, busy, onOpen }: { messages: Message[]; empty: string; busy: boolean; onOpen: (s: Source) => void }) {
  const end = useRef<HTMLDivElement>(null);
  const seconds = useElapsed(busy);
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, busy]);

  return (
    <div className="log" aria-live="polite">
      {messages.length === 0 && !busy && <p className="muted">{empty}</p>}
      {messages.map((m) => (
        <div key={m.id} className={`bubble ${m.from}${m.refused ? " refused" : ""}`}>
          {m.from === "ai" && <span className="sr-only">Yanua：</span>}
          <p className="text">{m.text}</p>
          {m.sources && m.sources.length > 0 && !m.refused && (
            <div className="sources">
              <span className="small muted">出典：</span>
              {m.sources.map((s) => (
                <button key={`${s.doc_id}-${s.section}`} className="source" onClick={() => onOpen(s)}>
                  {s.title}{s.section ? `（${s.section}）` : ""}
                </button>
              ))}
            </div>
          )}
          {m.note && <span className="small muted note">{m.note}</span>}
        </div>
      ))}
      {busy && (
        <div className="bubble ai waiting" role="status">
          <p className="text">考えています…{seconds >= 3 ? `（${seconds}秒）` : ""}</p>
        </div>
      )}
      <div ref={end} />
    </div>
  );
}

function Composer({ disabled, placeholder, onSend }: { disabled: boolean; placeholder: string; onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || disabled) return;
    setText("");
    onSend(t);
  };
  return (
    <form className="composer" onSubmit={submit}>
      <textarea
        aria-label="メッセージ"
        value={text}
        maxLength={MAX_TEXT}
        rows={2}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // Enter で送る。Shift+Enter と、日本語の変換中の Enter は改行・確定のまま
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <button type="submit" className="primary" disabled={disabled || !text.trim()}>送る</button>
    </form>
  );
}

function SourcePanel({ source, onClose }: { source: Source; onClose: () => void }) {
  const [doc, setDoc] = useState<DocumentBody | null>(null);
  const [error, setError] = useState("");
  const target = useRef<HTMLElement>(null);

  useEffect(() => {
    setDoc(null);
    setError("");
    api<DocumentBody>("GET", `/api/documents/${encodeURIComponent(source.doc_id)}`)
      .then(setDoc)
      .catch((e) => setError(errorText(e)));
  }, [source.doc_id]);

  useEffect(() => {
    target.current?.scrollIntoView?.({ block: "start" });
  }, [doc, source.section]);

  return (
    <aside className="panel" aria-label="出典">
      <div className="panel-head">
        <h2>{doc?.title ?? source.title}</h2>
        <button className="link" onClick={onClose}>閉じる</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!doc && !error && <p className="muted">読み込んでいます…</p>}
      {doc && (
        <div className="doc">
          {doc.updated && <p className="small muted">更新：{doc.updated}</p>}
          {doc.sections.map((s, i) => (
            <section
              key={i}
              ref={s.section === source.section ? target : undefined}
              className={s.section === source.section ? "hit" : undefined}
            >
              {s.section && <h3>{s.section}</h3>}
              <p className="text">{s.text}</p>
            </section>
          ))}
        </div>
      )}
    </aside>
  );
}

function useElapsed(running: boolean): number {
  const [sec, setSec] = useState(0);
  useEffect(() => {
    setSec(0);
    if (!running) return;
    const started = Date.now();
    const t = window.setInterval(() => setSec(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(t);
  }, [running]);
  return sec;
}

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : "処理できませんでした。しばらくしてからやり直してください";
}
