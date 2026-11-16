// S-23 受付の記録。電話・Web・来訪の記録の一覧、対応済みにする
import { useCallback, useEffect, useState } from "react";
import { api, ReceptionRecord } from "../api";
import { errorText, Status, time } from "../ui";

export default function Reception() {
  const [status, setStatus] = useState<"未対応" | "">("未対応");
  const [records, setRecords] = useState<ReceptionRecord[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const q = status ? `?status=${encodeURIComponent(status)}` : "";
      setRecords((await api<{ records: ReceptionRecord[] }>("GET", `/api/reception/records${q}`)).records);
      setError("");
    } catch (e) {
      setError(errorText(e, "受付の記録を読み込めませんでした"));
    }
  }, [status]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <h1 className="page-title">受付の記録</h1>
      <section className="card">
        <div className="tabs" role="tablist" aria-label="表示する記録">
          <button role="tab" aria-selected={status === "未対応"} className={status === "未対応" ? "tab on" : "tab"} onClick={() => setStatus("未対応")}>未対応</button>
          <button role="tab" aria-selected={status === ""} className={status === "" ? "tab on" : "tab"} onClick={() => setStatus("")}>すべて</button>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {!error && records === null && <p className="muted">読み込んでいます…</p>}
        {records?.length === 0 && <p className="muted">{status ? "未対応の記録はありません。" : "記録はありません。"}</p>}
        {records && records.length > 0 && (
          <ul className="records">
            {records.map((r) => (
              <li key={r.record_id}>
                <div className="record-head">
                  <Status s={r.status} />
                  <Status s={r.priority} />
                  <strong>{r.category}</strong>
                  <span className="small muted">{r.channel}・{time(r.created_at)}・{r.record_id}</span>
                </div>
                <p className="text">
                  {[r.visitor.company, r.visitor.name].filter(Boolean).join(" ") || "（お名前なし）"}：{r.purpose}
                </p>
                <p className="small muted">引き継ぎ先：{r.route}{r.host ? `・訪ね先：${r.host.name}` : ""}</p>
                <div className="toolbar">
                  <button className="link" aria-expanded={open === r.record_id} onClick={() => setOpen(open === r.record_id ? null : r.record_id)}>
                    {open === r.record_id ? "詳しい内容を閉じる" : "詳しい内容"}
                  </button>
                  {r.status !== "対応済み" && <DoneButton id={r.record_id} onDone={load} />}
                </div>
                {open === r.record_id && (
                  <dl className="facts">
                    <dt>連絡先</dt><dd>{r.contact || "—"}</dd>
                    <dt>内容</dt><dd className="text">{r.details || "—"}</dd>
                    {r.candidates.length > 0 && <><dt>訪ね先の候補</dt><dd>{r.candidates.join("、")}（同じ姓の社員がいます。確認してください）</dd></>}
                    <dt>受付で解決</dt><dd>{r.resolved ? "はい" : "いいえ"}</dd>
                    {r.status_updated_at && <><dt>対応済みにした人</dt><dd>{r.status_updated_by}（{time(r.status_updated_at)}）</dd></>}
                  </dl>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function DoneButton({ id, onDone }: { id: string; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <>
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api("POST", `/api/reception/records/${encodeURIComponent(id)}/done`);
            onDone();
          } catch (e) {
            setError(errorText(e, "対応済みにできませんでした"));
            setBusy(false);
          }
        }}
      >
        対応済みにする
      </button>
      {error && <span className="error small" role="alert">{error}</span>}
    </>
  );
}
