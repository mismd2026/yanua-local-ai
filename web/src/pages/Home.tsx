// S-02 ホーム。役割に応じたメニュー、未読のお知らせの件数、実行中のジョブ
import { useEffect, useState } from "react";
import { api, ApiError, Job, Me, RUNNING_STATUSES } from "../api";
import { menuFor } from "../menu";
import { navigate } from "../router";

const POLL_MS = 5000;

// ジョブの種類の表示名（yanua/api/app.py で register したもの）
const JOB_NAMES: Record<string, string> = { ingest: "文書の取り込み" };

export default function Home({ me }: { me: Me }) {
  const items = menuFor(me.permissions);
  return (
    <>
      <h1 className="page-title">ホーム</h1>
      <div className="grid">
        <section className="card">
          <h2>メニュー</h2>
          {items.length === 0 ? (
            <p className="muted">使えるメニューがありません。管理者に問い合わせてください。</p>
          ) : (
            <ul className="menu">
              {items.map((m) => (
                <li key={m.id}>
                  <a href={m.path} onClick={(e) => { e.preventDefault(); navigate(m.path); }}>
                    <strong>{m.label}</strong>
                    <span className="muted small">{m.description}</span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </section>
        <Notifications />
        {me.permissions.includes("jobs.own") && <RunningJobs />}
      </div>
    </>
  );
}

function Notifications() {
  // お知らせ（S-11）は段階2。API がまだないとき（404）は、この欄を出さない
  const [count, setCount] = useState<number | null | "none">(null);
  useEffect(() => {
    api<{ notifications?: unknown[]; unread?: number }>("GET", "/api/notifications?unread=1")
      .then((r) => setCount(r.unread ?? r.notifications?.length ?? 0))
      .catch(() => setCount("none"));
  }, []);
  if (count === "none") return null;
  return (
    <section className="card">
      <h2>お知らせ</h2>
      {count === null ? <p className="muted">読み込んでいます…</p> : <p>未読のお知らせが {count} 件あります。</p>}
    </section>
  );
}

function RunningJobs() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let timer: number | undefined;
    let alive = true;
    const load = async () => {
      try {
        const r = await api<{ jobs: Job[] }>("GET", "/api/jobs");
        if (!alive) return;
        const running = r.jobs.filter((j) => RUNNING_STATUSES.includes(j.status));
        setJobs(running);
        setError("");
        // 実行中のものがあるときだけ、進み具合を見に行く
        if (running.length > 0) timer = window.setTimeout(load, POLL_MS);
      } catch (e) {
        if (alive) setError(e instanceof ApiError ? e.message : "ジョブを読み込めませんでした");
      }
    };
    void load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);

  return (
    <section className="card">
      <h2>実行中のジョブ</h2>
      {error && <p className="error" role="alert">{error}</p>}
      {!error && jobs === null && <p className="muted">読み込んでいます…</p>}
      {jobs?.length === 0 && <p className="muted">実行中のジョブはありません。</p>}
      {jobs && jobs.length > 0 && (
        <ul className="jobs">
          {jobs.map((j) => (
            <li key={j.job_id}>
              <span>{JOB_NAMES[j.kind] ?? j.kind}</span>
              <span className="muted small">{j.status}{j.stage ? `・${j.stage}` : ""}</span>
              <progress max={100} value={j.progress} aria-label={`${JOB_NAMES[j.kind] ?? j.kind}の進み具合`} />
              <span className="small">{j.progress}%</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
