// S-12 来訪予定（自分の）。登録 → コードと案内文を表示（コードはこのとき1回だけ）、一覧、取り消し
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, CreatedVisit, Visit } from "../api";
import { errorText, Status, todayIso } from "../ui";

const EMPTY = { date: "", time: "", room: "", visitor_company: "", visitor_name: "", purpose: "", host_contact: "" };

export default function Visits() {
  const [visits, setVisits] = useState<Visit[] | null>(null);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<CreatedVisit | null>(null);
  const [showPast, setShowPast] = useState(false);

  const load = useCallback(async () => {
    try {
      setVisits((await api<{ visits: Visit[] }>("GET", "/api/visits")).visits);
      setError("");
    } catch (e) {
      setError(errorText(e, "来訪予定を読み込めませんでした"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const today = todayIso();
  const shown = visits?.filter((v) => showPast || v.date >= today);

  return (
    <>
      <h1 className="page-title">来訪予定</h1>
      <div className="grid wide">
        <VisitForm onCreated={(c) => { setCreated(c); void load(); }} />
        {created && <CodeOnce created={created} onClose={() => setCreated(null)} />}
      </div>
      <section className="card spaced">
        <div className="row-between">
          <h2>自分の来訪予定</h2>
          <label className="check">
            <input type="checkbox" checked={showPast} onChange={(e) => setShowPast(e.target.checked)} /> 過ぎた予定も出す
          </label>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {!error && visits === null && <p className="muted">読み込んでいます…</p>}
        {shown?.length === 0 && <p className="muted">来訪予定はありません。</p>}
        {shown && shown.length > 0 && <VisitTable visits={shown} onChanged={load} />}
      </section>
    </>
  );
}

export function visitState(v: Visit): string {
  return v.cancelled ? "取り消し" : v.checked_in_at ? "受付済み" : "予定";
}

export function VisitTable({ visits, onChanged, showHost = false }: { visits: Visit[]; onChanged: () => void; showHost?: boolean }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>日時</th>
            <th>会議室</th>
            <th>お客様</th>
            {showHost && <th>担当</th>}
            <th>状態</th>
            <th><span className="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          {visits.map((v) => (
            <tr key={v.visit_id}>
              <td className="nowrap">{v.date} {v.time}</td>
              <td>{v.room}</td>
              <td>{v.visitor_company} {v.visitor_name}様{v.purpose && <div className="small muted">{v.purpose}</div>}</td>
              {showHost && <td>{v.host_name}<div className="small muted">{v.host_dept}</div></td>}
              <td><Status s={visitState(v)} /></td>
              <td>{!v.cancelled && !v.checked_in_at && <CancelButton visit={v} onDone={onChanged} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// 取り消しは2段階（押す → 「取り消す」を押して確かめる）。画面の上で確かめるので confirm() は使わない
function CancelButton({ visit, onDone }: { visit: Visit; onDone: () => void }) {
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const label = `${visit.date} ${visit.time} ${visit.visitor_company}`;
  if (!asking) return <button className="link" onClick={() => setAsking(true)} aria-label={`${label}の予定を取り消す`}>取り消す</button>;
  return (
    <span className="confirm">
      <span className="small">コードも使えなくなります。</span>
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api("DELETE", `/api/visits/${visit.visit_id}`);
            onDone();
          } catch (e) {
            setError(errorText(e, "取り消せませんでした"));
            setBusy(false);
          }
        }}
      >
        取り消す
      </button>
      <button className="link" onClick={() => setAsking(false)} disabled={busy}>やめる</button>
      {error && <span className="error small" role="alert">{error}</span>}
    </span>
  );
}

function VisitForm({ onCreated }: { onCreated: (c: CreatedVisit) => void }) {
  const [f, setF] = useState({ ...EMPTY, date: todayIso() });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const field = (key: keyof typeof EMPTY) => ({
    id: `visit-${key}`,
    value: f[key],
    onChange: (e: { target: { value: string } }) => setF({ ...f, [key]: e.target.value }),
  });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const c = await api<CreatedVisit>("POST", "/api/visits", f);
      setF({ ...EMPTY, date: f.date });
      onCreated(c);
    } catch (err) {
      setError(errorText(err, "登録できませんでした"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2>来訪予定を登録する</h2>
      <form className="form" onSubmit={submit}>
        <div className="form-row">
          <label htmlFor="visit-date">日付</label>
          <input type="date" required {...field("date")} />
        </div>
        <div className="form-row">
          <label htmlFor="visit-time">時刻</label>
          <input type="time" required {...field("time")} />
        </div>
        <div className="form-row">
          <label htmlFor="visit-room">会議室</label>
          <input required maxLength={100} placeholder="例：5階 第2会議室" {...field("room")} />
        </div>
        <div className="form-row">
          <label htmlFor="visit-visitor_company">お客様の会社名</label>
          <input required maxLength={100} {...field("visitor_company")} />
        </div>
        <div className="form-row">
          <label htmlFor="visit-visitor_name">お客様のお名前</label>
          <input required maxLength={100} {...field("visitor_name")} />
        </div>
        <div className="form-row">
          <label htmlFor="visit-purpose">用件（社内向け。任意）</label>
          <input maxLength={500} {...field("purpose")} />
        </div>
        <div className="form-row">
          <label htmlFor="visit-host_contact">当日の連絡先（任意）</label>
          <input maxLength={100} placeholder="例：内線4100" {...field("host_contact")} />
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" className="primary" disabled={busy}>{busy ? "登録しています…" : "登録してコードを作る"}</button>
      </form>
    </section>
  );
}

// コードはサーバーに残していない（ハッシュだけ）。この画面を閉じると、もう表示できない
function CodeOnce({ created, onClose }: { created: CreatedVisit; onClose: () => void }) {
  const [copied, setCopied] = useState("");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(created.invitation);
      setCopied("案内文をコピーしました。");
    } catch {
      setCopied("コピーできませんでした。下の案内文を選んでコピーしてください。");
    }
  };
  return (
    <section className="card code-once" aria-label="インビテーションコード">
      <h2>インビテーションコード</h2>
      <p className="code">{created.code}</p>
      <p className="notice"><strong>コードはこのときだけ表示します。</strong>閉じると、もう見られません。案内文をお客様に送ってください。</p>
      <pre className="invitation">{created.invitation}</pre>
      <div className="toolbar">
        <button className="primary" onClick={() => void copy()}>案内文をコピー</button>
        <button onClick={onClose}>閉じる</button>
      </div>
      {copied && <p className="small" role="status">{copied}</p>}
    </section>
  );
}
