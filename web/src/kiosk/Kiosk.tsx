// 受付端末（S-40 受付・S-41 予定の表示と入館票）。
// - メニューやリンクは置かない。社内の画面には移れない（この画面は社内の画面とは別に組み立てる）
// - 一定時間触られなければ、最初の画面に戻る（前の方の内容を残さない）
// - 返事の文はサーバーが決めたものをそのまま出す。コードが違う日・間違い・取り消しでも、画面は同じ
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { CheckInReply, formatCode, kiosk, KioskError, KioskReply, KioskVisit, loadToken, saveToken } from "./kioskApi";

export const IDLE_MS = 60_000;

// コードが確認できなかった回数を、この画面で数える。直近1分に CALL_STAFF_AFTER 回から「受付の者をお呼びください」を出す。
// サーバーは、1分に10回を超えて間違えた端末では照合を止めるが、返事は間違えたときと同じにしている（止めているかどうかを
// 返事から分からせないため）。画面は返事の違いではなく自分で数えた回数で案内を出し、サーバーより少ない回数から出すので、
// サーバーが止めているあいだは必ず案内が出る。数えた回数は、最初の画面に戻っても1分のあいだは残す（次の方にも出す）
export const CALL_STAFF_AFTER = 3;
const FAIL_WINDOW_MS = 60_000;

type Screen =
  | { kind: "setup"; notice?: string }
  | { kind: "welcome" }
  | { kind: "code" }
  | { kind: "chat" }
  | { kind: "visit"; visit: KioskVisit; message: string }
  | { kind: "done"; message: string };

const FALLBACK = "恐れ入りますが、受付の者にお声がけください。";

export default function Kiosk({ idleMs = IDLE_MS }: { idleMs?: number }) {
  const [token, setToken] = useState(loadToken);
  const [screen, setScreen] = useState<Screen>(() => (loadToken() ? { kind: "welcome" } : { kind: "setup" }));
  const [round, setRound] = useState(0); // 最初の画面に戻るたびに増やし、前の方の入力を消す
  const failures = useRef<number[]>([]);
  const [callStaff, setCallStaff] = useState(false);

  const recount = useCallback(() => {
    const now = Date.now();
    failures.current = failures.current.filter((t) => now - t < FAIL_WINDOW_MS);
    setCallStaff(failures.current.length >= CALL_STAFF_AFTER);
  }, []);

  const failed = useCallback(() => {
    failures.current.push(Date.now());
    recount();
  }, [recount]);

  // 1分たったら案内を消せるよう、案内を出しているあいだは数え直す
  useEffect(() => {
    if (!callStaff) return;
    const t = window.setInterval(recount, 5_000);
    return () => window.clearInterval(t);
  }, [callStaff, recount]);

  const home = useCallback(() => {
    setScreen({ kind: "welcome" });
    setRound((r) => r + 1);
  }, []);

  // 登録されていない・取り消された端末になったら、設定の画面に戻す
  const onError = useCallback((e: unknown): string => {
    if (e instanceof KioskError && e.status === 401) {
      saveToken("");
      setToken("");
      setScreen({ kind: "setup", notice: "この端末は受付端末として登録されていません。管理者に端末トークンを発行してもらってください。" });
      return "";
    }
    return e instanceof KioskError ? e.message : FALLBACK;
  }, []);

  // 一定時間触られなければ最初の画面に戻る
  useEffect(() => {
    if (screen.kind === "welcome" || screen.kind === "setup") return;
    let timer = window.setTimeout(home, idleMs);
    const touch = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(home, idleMs);
    };
    const events = ["pointerdown", "keydown", "input", "touchstart"];
    events.forEach((ev) => window.addEventListener(ev, touch, { passive: true }));
    return () => {
      window.clearTimeout(timer);
      events.forEach((ev) => window.removeEventListener(ev, touch));
    };
  }, [screen.kind, idleMs, home, round]);

  return (
    <div className="kiosk">
      <header className="k-head">
        <span className="k-brand">受付</span>
        {screen.kind !== "welcome" && screen.kind !== "setup" && (
          <button className="k-back" onClick={home}>最初に戻る</button>
        )}
      </header>
      {callStaff && screen.kind !== "setup" && (
        <p className="k-staff" role="alert">お手数ですが、受付の者をお呼びください。</p>
      )}
      <main className="k-main" key={round}>
        {screen.kind === "setup" && (
          <Setup notice={screen.notice} onSaved={(t) => { saveToken(t); setToken(t); home(); }} />
        )}
        {screen.kind === "welcome" && <Welcome onCode={() => setScreen({ kind: "code" })} onNoCode={() => setScreen({ kind: "chat" })} />}
        {screen.kind === "code" && (
          <CodeEntry
            token={token}
            onError={onError}
            onOk={(r) => setScreen({ kind: "visit", visit: r.visit!, message: r.message })}
            onFailed={failed}
            onNoCode={() => setScreen({ kind: "chat" })}
          />
        )}
        {screen.kind === "chat" && (
          <Conversation
            token={token}
            onError={onError}
            onVisit={(visit, message) => setScreen({ kind: "visit", visit, message })}
            onDone={(message) => setScreen({ kind: "done", message })}
          />
        )}
        {screen.kind === "visit" && <VisitCard visit={screen.visit} message={screen.message} onEnd={home} />}
        {screen.kind === "done" && (
          <section className="k-panel">
            <p className="k-say">{screen.message}</p>
            <button className="k-primary" onClick={home}>終わる</button>
          </section>
        )}
      </main>
    </div>
  );
}

// ------------------------------------------------------------ 端末の設定（管理者が1回だけ）

function Setup({ notice, onSaved }: { notice?: string; onSaved: (token: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <section className="k-panel k-setup">
      <h1>受付端末の設定</h1>
      <p>管理者が発行した端末トークンを入れてください。この画面は、設定のときに管理者だけが使います。</p>
      {notice && <p className="k-notice" role="alert">{notice}</p>}
      <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) onSaved(value.trim()); }}>
        <label htmlFor="k-token">端末トークン</label>
        <input id="k-token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
        <button type="submit" className="k-primary" disabled={!value.trim()}>設定する</button>
      </form>
    </section>
  );
}

// ------------------------------------------------------------ S-40 受付：挨拶

function Welcome({ onCode, onNoCode }: { onCode: () => void; onNoCode: () => void }) {
  return (
    <section className="k-panel k-welcome">
      <h1>いらっしゃいませ</h1>
      <p className="k-lead">ご来訪ありがとうございます。あてはまる方を押してください。</p>
      <div className="k-choices">
        <button className="k-choice" onClick={onCode}>
          <strong>インビテーションコードを</strong>
          <strong>お持ちの方</strong>
          <span>ご案内のメールに書かれた8文字のコード</span>
        </button>
        <button className="k-choice" onClick={onNoCode}>
          <strong>お持ちでない方</strong>
          <span>ご用件をお聞かせください</span>
        </button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------ S-40 受付：コードの入力

function CodeEntry({ token, onError, onOk, onFailed, onNoCode }: {
  token: string;
  onError: (e: unknown) => string;
  onOk: (r: CheckInReply) => void;
  onFailed: () => void;
  onNoCode: () => void;
}) {
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (code.replace("-", "").length !== 8 || busy) return;
    setBusy(true);
    setMessage("");
    try {
      const r = await kiosk<CheckInReply>(token, "/api/kiosk/check-in", { code });
      if (r.ok && r.visit) {
        onOk(r);
        return;
      }
      // 文言はサーバーの返事のまま（止めているときも同じ文言）
      setMessage(r.message);
      onFailed();
      setCode("");
      input.current?.focus();
    } catch (err) {
      setMessage(onError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="k-panel">
      <h1>インビテーションコードを入れてください</h1>
      <form className="k-code" onSubmit={submit}>
        <label htmlFor="k-code" className="sr-only">インビテーションコード</label>
        <input
          id="k-code"
          ref={input}
          autoFocus
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          inputMode="text"
          placeholder="ABCD-EFGH"
          value={code}
          onChange={(e) => setCode(formatCode(e.target.value))}
        />
        <button type="submit" className="k-primary" disabled={busy || code.replace("-", "").length !== 8}>
          {busy ? "確認しています…" : "確認する"}
        </button>
      </form>
      {message && <p className="k-notice" role="alert">{message}</p>}
      <button className="k-secondary" onClick={onNoCode}>コードがわからない・お持ちでない方</button>
    </section>
  );
}

// ------------------------------------------------------------ S-40 受付：用件の会話

interface Line {
  id: number;
  from: "kiosk" | "visitor";
  text: string;
}

function Conversation({ token, onError, onVisit, onDone }: {
  token: string;
  onError: (e: unknown) => string;
  onVisit: (v: KioskVisit, message: string) => void;
  onDone: (message: string) => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [session, setSession] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const nextId = useRef(1);
  const log = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  const add = (from: Line["from"], t: string) => setLines((ls) => [...ls, { id: nextId.current++, from, text: t }]);

  const handle = (r: KioskReply) => {
    if (r.finished) {
      if (r.visit) onVisit(r.visit, r.reply);
      else onDone(r.reply);
      return;
    }
    setSession(r.session_id);
    add("kiosk", r.reply);
  };

  useEffect(() => {
    // 開発時の StrictMode で2回呼ばれても、会話は1つだけ始める
    if (started.current) return;
    started.current = true;
    kiosk<KioskReply>(token, "/api/kiosk/sessions")
      .then(handle)
      .catch((e) => setError(onError(e)))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    if (log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [lines.length, busy]);

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t || !session || busy) return;
    setText("");
    add("visitor", t);
    setBusy(true);
    setError("");
    try {
      handle(await kiosk<KioskReply>(token, `/api/kiosk/sessions/${encodeURIComponent(session)}/messages`, { text: t }));
    } catch (err) {
      // 会話が消えていた（時間切れ）ときも、受付の者に任せる案内にする
      setError(onError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="k-panel k-chat">
      <div className="k-log" ref={log} aria-live="polite">
        {lines.map((l) => (
          <p key={l.id} className={`k-line ${l.from}`}>{l.text}</p>
        ))}
        {busy && <p className="k-line kiosk waiting" role="status">少々お待ちください…</p>}
      </div>
      {error && <p className="k-notice" role="alert">{error}</p>}
      <form className="k-input" onSubmit={send}>
        <label htmlFor="k-text" className="sr-only">お返事</label>
        <input
          id="k-text"
          autoComplete="off"
          maxLength={2000}
          placeholder="ここに入れてください"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!session}
        />
        <button type="submit" className="k-primary" disabled={busy || !session || !text.trim()}>送る</button>
      </form>
    </section>
  );
}

// ------------------------------------------------------------ S-41 予定の表示と入館票

function VisitCard({ visit, message, onEnd }: { visit: KioskVisit; message: string; onEnd: () => void }) {
  const rows: [string, string][] = [
    ["日時", `${visit.date} ${visit.time}`],
    ["会議室", visit.room],
    ["担当", visit.host_name],
    ["連絡先", visit.host_contact],
    ["お名前", `${visit.visitor_company} ${visit.visitor_name}様`],
  ];
  return (
    <section className="k-panel k-visit">
      <p className="k-say">{message}</p>
      <table className="k-table">
        <tbody>
          {rows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v}</td></tr>)}
        </tbody>
      </table>
      <div className="k-actions">
        <button className="k-primary" onClick={() => window.print()}>入館票を印刷する</button>
        <button className="k-secondary" onClick={onEnd}>終わる</button>
      </div>
      {/* 印刷するときだけ出す入館票。コードは載せない（落としても、ほかの人が使えないように） */}
      <div className="k-pass" aria-hidden="true">
        <h1>入館票（ゲスト）</h1>
        <table>
          <tbody>
            {rows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v}</td></tr>)}
          </tbody>
        </table>
        <p>お帰りの際は、受付に入館票をお返しください。</p>
      </div>
    </section>
  );
}
