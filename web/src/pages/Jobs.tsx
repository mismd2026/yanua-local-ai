// S-03 ジョブの一覧。自分のジョブ（管理者はすべての人のジョブ）、詳しい内容、取り消し。
// 管理者は、ここから文書の取り込みを始められる（実機で試すため）
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Job, jobName, Me, RUNNING_STATUSES } from "../api";

const POLL_MS = 3000;

export default function Jobs({ me }: { me: Me }) {
  const canAll = me.permissions.includes("jobs.all");
  const canIngest = me.permissions.includes("documents.ingest");
  const [all, setAll] = useState(false);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(() => new URLSearchParams(window.location.search).get("job"));
  const [tick, setTick] = useState(0); // 取り込みを始めた・取り消したときに読み直す
  const reload = useCallback(() => setTick((t) => t + 1), []);

  // 一覧。実行中・待ちのジョブがあるあいだだけ、数秒ごとに読み直す
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      try {
        const r = await api<{ jobs: Job[] }>("GET", all ? "/api/jobs?all=true" : "/api/jobs");
        if (!alive) return;
        setJobs(r.jobs);
        setError("");
        if (r.jobs.some((j) => RUNNING_STATUSES.includes(j.status))) timer = window.setTimeout(load, POLL_MS);
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? e.message : "ジョブを読み込めませんでした");
      }
    };
    void load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [all, tick]);

  return (
    <>
      <h1 className="page-title">ジョブ</h1>
      {canIngest && <IngestForm onStarted={(id) => { setSelected(id); reload(); }} />}
      <div className="jobs-page">
        <section className="card">
          <div className="row-between">
            <h2>{all ? "すべての人のジョブ" : "自分のジョブ"}</h2>
            {canAll && (
              <div className="tabs" role="tablist" aria-label="表示するジョブ">
                <button role="tab" aria-selected={!all} className={!all ? "tab on" : "tab"} onClick={() => setAll(false)}>自分のジョブ</button>
                <button role="tab" aria-selected={all} className={all ? "tab on" : "tab"} onClick={() => setAll(true)}>すべての人のジョブ</button>
              </div>
            )}
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          {!error && jobs === null && <p className="muted">読み込んでいます…</p>}
          {jobs?.length === 0 && <p className="muted">ジョブはありません。</p>}
          {jobs && jobs.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>種類</th>
                    {all && <th>依頼した人</th>}
                    <th>状態</th>
                    <th>進み具合</th>
                    <th>始めた時刻</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.job_id} className={j.job_id === selected ? "selected" : undefined}>
                      <td>
                        <button className="link" onClick={() => setSelected(j.job_id)} aria-label={`${jobName(j.kind)}（${time(j.created_at)}）の詳しい内容`}>
                          {jobName(j.kind)}
                        </button>
                      </td>
                      {all && <td>{j.owner}</td>}
                      <td><Status s={j.status} /></td>
                      <td className="progress-cell">
                        <progress max={100} value={j.progress} aria-label="進み具合" /> <span className="small">{j.progress}%</span>
                      </td>
                      <td className="small">{time(j.started_at || j.created_at)}{!j.started_at && <span className="muted">（受付）</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {selected && <JobDetail id={selected} onClose={() => setSelected(null)} onChanged={reload} />}
      </div>
    </>
  );
}

function JobDetail({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      try {
        const j = await api<Job>("GET", `/api/jobs/${encodeURIComponent(id)}`);
        if (!alive) return;
        setJob(j);
        setError("");
        if (RUNNING_STATUSES.includes(j.status)) timer = window.setTimeout(load, POLL_MS);
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? e.message : "ジョブを読み込めませんでした");
      }
    };
    setJob(null);
    void load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [id, tick]);

  const cancel = async () => {
    setBusy(true);
    try {
      await api("DELETE", `/api/jobs/${encodeURIComponent(id)}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "取り消せませんでした");
    } finally {
      setBusy(false);
      setTick((t) => t + 1);
      onChanged();
    }
  };

  return (
    <aside className="card detail" aria-label="ジョブの詳しい内容">
      <div className="row-between">
        <h2>{job ? jobName(job.kind) : "ジョブ"}</h2>
        <button className="link" onClick={onClose}>閉じる</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!job && !error && <p className="muted">読み込んでいます…</p>}
      {job && (
        <>
          <dl className="facts">
            <dt>状態</dt><dd><Status s={job.status} />{job.stage ? `（${job.stage}）` : ""}</dd>
            <dt>進み具合</dt><dd><progress max={100} value={job.progress} aria-label="進み具合" /> {job.progress}%</dd>
            {job.owner && <><dt>依頼した人</dt><dd>{job.owner}</dd></>}
            <dt>受け付けた時刻</dt><dd>{time(job.created_at)}</dd>
            <dt>始めた時刻</dt><dd>{job.started_at ? time(job.started_at) : "まだ始まっていません"}</dd>
            <dt>終わった時刻</dt><dd>{job.finished_at ? time(job.finished_at) : "—"}</dd>
            {typeof job.params?.source === "string" && <><dt>取り込むフォルダ</dt><dd className="mono">{job.params.source}</dd></>}
          </dl>
          {job.error && <p className="error">{job.error}</p>}
          {job.result && <Result kind={job.kind} result={job.result} />}
          {RUNNING_STATUSES.includes(job.status) && (
            <div className="actions">
              <button onClick={() => void cancel()} disabled={busy}>{busy ? "取り消しています…" : "このジョブを取り消す"}</button>
            </div>
          )}
          {job.status === "実行中" && <p className="small muted">実行中のジョブは、次の区切りで止まります。</p>}
        </>
      )}
    </aside>
  );
}

const INGEST_LABELS: [string, string][] = [
  ["added", "追加"],
  ["updated", "更新"],
  ["unchanged", "変わらない"],
  ["deleted", "削除"],
  ["errors", "エラー"],
];

function Result({ kind, result }: { kind: string; result: Record<string, unknown> }) {
  if (kind === "ingest") {
    return (
      <div>
        <h3>結果</h3>
        <ul className="result-list">
          {INGEST_LABELS.map(([k, label]) => {
            const items = Array.isArray(result[k]) ? (result[k] as string[]) : [];
            return (
              <li key={k} className={k === "errors" && items.length ? "error" : undefined}>
                {label}：{items.length} 件{items.length > 0 && <span className="small muted">　{items.join("、")}</span>}
              </li>
            );
          })}
        </ul>
      </div>
    );
  }
  return (
    <div>
      <h3>結果</h3>
      <pre className="mono small">{JSON.stringify(result, null, 2)}</pre>
    </div>
  );
}

function IngestForm({ onStarted }: { onStarted: (jobId: string) => void }) {
  const [sources, setSources] = useState<{ path: string; label: string }[] | null>(null);
  const [source, setSource] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ sources: { path: string; label: string }[] }>("GET", "/api/documents/ingest/sources")
      .then((r) => {
        setSources(r.sources);
        setSource(r.sources[0]?.path ?? "");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "取り込めるフォルダを読み込めませんでした"));
  }, []);

  const start = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const job = await api<Job>("POST", "/api/documents/ingest", { source });
      setMessage("取り込みを受け付けました。");
      onStarted(job.job_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "取り込みを始められませんでした");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card ingest">
      <h2>文書の取り込み（管理者）</h2>
      <p className="small muted">フォルダの中の文書を取り込みます。前に同じフォルダから取り込んだ文書は、変わったものだけを反映し、なくなったものは消します。</p>
      {sources && sources.length === 0 && <p className="muted">取り込めるフォルダがありません（設定の ingest_roots）。</p>}
      {sources && sources.length > 0 && (
        <div className="ingest-row">
          <label htmlFor="ingest-source">フォルダ</label>
          <select id="ingest-source" value={source} onChange={(e) => setSource(e.target.value)}>
            {sources.map((s) => <option key={s.path} value={s.path}>{s.label}</option>)}
          </select>
          <button className="primary" onClick={() => void start()} disabled={busy || !source}>
            {busy ? "受け付けています…" : "取り込みを始める"}
          </button>
        </div>
      )}
      {message && <p role="status">{message}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

function Status({ s }: { s: string }) {
  const cls = s === "完了" ? "ok" : s === "失敗" ? "ng" : s === "取り消し" ? "off" : "run";
  return <span className={`status ${cls}`}>{s}</span>;
}

function time(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ja-JP");
}
