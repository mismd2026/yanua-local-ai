// S-30 問い合わせ票。一覧（優先度・状態で絞り込み）、中身、状態の変更
import { FormEvent, Fragment, useCallback, useEffect, useState } from "react";
import { api, Ticket, TICKET_STATUSES } from "../api";
import { errorText, Status, time } from "../ui";

export default function Tickets() {
  const [status, setStatus] = useState("");
  const [priority, setPriority] = useState("");
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (priority) q.set("priority", priority);
    try {
      const s = q.toString();
      setTickets((await api<{ tickets: Ticket[] }>("GET", `/api/tickets${s ? `?${s}` : ""}`)).tickets);
      setError("");
    } catch (e) {
      setError(errorText(e, "問い合わせ票を読み込めませんでした"));
    }
  }, [status, priority]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <h1 className="page-title">問い合わせ票</h1>
      <div className="jobs-page">
        <section className="card">
          <div className="toolbar">
            <label htmlFor="t-status">状態</label>
            <select id="t-status" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">すべて</option>
              {TICKET_STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
            <label htmlFor="t-priority">優先度</label>
            <select id="t-priority" value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option value="">すべて</option>
              {["高", "中", "低"].map((p) => <option key={p}>{p}</option>)}
            </select>
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          {!error && tickets === null && <p className="muted">読み込んでいます…</p>}
          {tickets?.length === 0 && <p className="muted">問い合わせ票はありません。</p>}
          {tickets && tickets.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>受付番号</th><th>優先度</th><th>状態</th><th>内容</th><th>依頼した人</th><th>受け付けた時刻</th></tr></thead>
                <tbody>
                  {tickets.map((t) => (
                    <tr key={t.ticket_id} className={t.ticket_id === selected ? "selected" : undefined}>
                      <td><button className="link" onClick={() => setSelected(t.ticket_id)}>{t.ticket_id}</button></td>
                      <td><Status s={t.priority} /></td>
                      <td><Status s={t.status} /></td>
                      <td>{t.category}<div className="small muted">{t.problem}</div></td>
                      <td>{t.requester.name}<div className="small muted">{t.requester.dept}</div></td>
                      <td className="small">{time(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {selected && <TicketDetail id={selected} onClose={() => setSelected(null)} onSaved={load} />}
      </div>
    </>
  );
}

// 票の中身のうち、画面に出す項目と見出し（payload の名前 → 表示）
const DETAIL_LABELS: [string, string][] = [
  ["problem", "お困りごと"], ["details", "状況"], ["contact", "連絡先"], ["tried", "試した手順"], ["slots", "聞き取った内容"],
];

function TicketDetail({ id, onClose, onSaved }: { id: string; onClose: () => void; onSaved: () => void }) {
  const [t, setT] = useState<Ticket | null>(null);
  const [status, setStatus] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setT(null);
    setMessage("");
    api<Ticket>("GET", `/api/tickets/${encodeURIComponent(id)}`)
      .then((r) => {
        setT(r);
        setStatus(r.status);
        setNote(r.note);
        setError("");
      })
      .catch((e) => setError(errorText(e, "問い合わせ票を読み込めませんでした")));
  }, [id]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const r = await api<Ticket>("PATCH", `/api/tickets/${encodeURIComponent(id)}`, { status, note });
      setT(r);
      setMessage("保存しました。");
      onSaved();
    } catch (err) {
      setError(errorText(err, "保存できませんでした"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="card detail" aria-label="問い合わせ票の中身">
      <div className="row-between">
        <h2>{id}</h2>
        <button className="link" onClick={onClose}>閉じる</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!t && !error && <p className="muted">読み込んでいます…</p>}
      {t && (
        <>
          <dl className="facts">
            <dt>依頼した人</dt><dd>{t.requester.name}（{t.requester.dept}・{t.requester.employee_no}）</dd>
            <dt>種類・優先度</dt><dd>{t.category}・<Status s={t.priority} /></dd>
            <dt>受け付けた時刻</dt><dd>{time(t.created_at)}</dd>
            {DETAIL_LABELS.map(([key, label]) => {
              const v = t.detail?.[key];
              if (v === undefined || v === "" || (Array.isArray(v) && v.length === 0)) return null;
              const text = typeof v === "string" ? v : Array.isArray(v) ? v.join("、") :
                Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k}：${String(x)}`).join("\n");
              return <Fragment key={key}><dt>{label}</dt><dd className="text">{text}</dd></Fragment>;
            })}
          </dl>
          <form className="form" onSubmit={save}>
            <div className="form-row">
              <label htmlFor="t-new-status">状態</label>
              <select id="t-new-status" value={status} onChange={(e) => setStatus(e.target.value)}>
                {TICKET_STATUSES.map((s) => <option key={s}>{s}</option>)}
              </select>
            </div>
            <div className="form-row">
              <label htmlFor="t-note">対応のメモ</label>
              <textarea id="t-note" rows={3} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
            {t.status_updated_at && <p className="small muted">最後に変えた人：{t.status_updated_by}（{time(t.status_updated_at)}）</p>}
            <button type="submit" className="primary" disabled={busy}>{busy ? "保存しています…" : "保存する"}</button>
            {message && <p role="status">{message}</p>}
          </form>
        </>
      )}
    </aside>
  );
}
