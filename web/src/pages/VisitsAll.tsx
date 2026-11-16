// S-22 来訪予定（全社）。日付ごとの一覧、CSV・カレンダーの書き出しの取り込みと結果
import { ChangeEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, Visit } from "../api";
import { errorText, todayIso } from "../ui";
import { VisitTable } from "./Visits";

interface ImportResult {
  kind: "visits" | "calendar";
  errors: string[];
  added: { visit: Visit; code: string }[] | number;
  updated?: number;
  cancelled?: number;
}

export default function VisitsAll() {
  const [day, setDay] = useState(todayIso());
  const [visits, setVisits] = useState<Visit[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setVisits((await api<{ visits: Visit[] }>("GET", `/api/visits?date=${encodeURIComponent(day)}`)).visits);
      setError("");
    } catch (e) {
      setError(errorText(e, "来訪予定を読み込めませんでした"));
    }
  }, [day]);

  useEffect(() => {
    setVisits(null);
    void load();
  }, [load]);

  const active = visits?.filter((v) => !v.cancelled).length ?? 0;
  const arrived = visits?.filter((v) => v.checked_in_at && !v.cancelled).length ?? 0;

  return (
    <>
      <h1 className="page-title">全社の来訪予定</h1>
      <section className="card">
        <div className="row-between">
          <div className="toolbar">
            <label htmlFor="day">日付</label>
            <input id="day" type="date" value={day} onChange={(e) => e.target.value && setDay(e.target.value)} />
          </div>
          {visits && <p className="muted" role="status">予定 {active} 件（受付済み {arrived} 件）</p>}
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {!error && visits === null && <p className="muted">読み込んでいます…</p>}
        {visits?.length === 0 && <p className="muted">この日の来訪予定はありません。</p>}
        {visits && visits.length > 0 && <VisitTable visits={visits} onChanged={load} showHost />}
      </section>
      <ImportCard onImported={load} />
    </>
  );
}

// File.text() がない環境（古いブラウザ・テストの環境）でも読めるように、FileReader でも読む
function readText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result ?? ""));
    r.onerror = () => reject(r.error);
    r.readAsText(file, "utf-8");
  });
}

function ImportCard({ onImported }: { onImported: () => void }) {
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const upload = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      // 本文に CSV をそのまま送る（サーバーが来訪予定の CSV かカレンダーの書き出しかを見分ける）
      const res = await fetch("/api/visits/import", {
        method: "POST",
        headers: { "Content-Type": "text/csv", "X-Yanua-Client": "web", Accept: "application/json" },
        credentials: "same-origin",
        body: await readText(file),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new ApiError(res.status, data?.error?.code ?? "error", data?.error?.message ?? "取り込めませんでした");
      setResult(data as ImportResult);
      onImported();
    } catch (err) {
      setError(errorText(err, "取り込めませんでした"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card spaced">
      <h2>取り込み（CSV・カレンダーの書き出し）</h2>
      <p className="small muted">
        来訪予定の CSV（date・time・company・visitor・host_employee_no・room の列）か、Googleカレンダーのアドオンの書き出しを選んでください。
        カレンダーの書き出しは、日時・会議室の変更（リスケ）と取り消しも反映します。
      </p>
      {/* ブラウザの「ファイルを選択」のボタンは見えなくし、押せる場所はこのボタン1つにする（入力欄はキーボードでも選べる） */}
      <label className={busy ? "file busy" : "file"}>
        <input type="file" accept=".csv,text/csv" onChange={(e) => void upload(e)} disabled={busy} />
        <span aria-hidden="true">{busy ? "取り込んでいます…" : "ファイルを選ぶ"}</span>
        <span className="sr-only">{busy ? "取り込んでいます" : "取り込むファイルを選ぶ"}</span>
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      {result && <ImportReport r={result} />}
    </section>
  );
}

function ImportReport({ r }: { r: ImportResult }) {
  if (r.kind === "calendar") {
    return (
      <div role="status">
        <p>カレンダーの書き出しを取り込みました：追加 {String(r.added)} 件・更新 {r.updated ?? 0} 件・取り消し {r.cancelled ?? 0} 件</p>
        {r.errors.length > 0 && <ul className="error small">{r.errors.map((e) => <li key={e}>{e}</li>)}</ul>}
      </div>
    );
  }
  const added = Array.isArray(r.added) ? r.added : [];
  return (
    <div role="status">
      <p>来訪予定を {added.length} 件登録しました（登録済みの予定は飛ばしました）。</p>
      {added.length > 0 && (
        <>
          <p className="notice small"><strong>コードはこのときだけ表示します。</strong>担当者からお客様に送ってください。</p>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>日時</th><th>お客様</th><th>担当</th><th>コード</th></tr></thead>
              <tbody>
                {added.map(({ visit, code }) => (
                  <tr key={visit.visit_id}>
                    <td className="nowrap">{visit.date} {visit.time}</td>
                    <td>{visit.visitor_company} {visit.visitor_name}様</td>
                    <td>{visit.host_name}</td>
                    <td className="mono">{code}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
