// 画面で共通に使う小さな部品
import { ApiError } from "./api";

export function time(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ja-JP");
}

// 状態の印。色だけに頼らず、文字で状態を出す
const TONE: Record<string, string> = {
  完了: "ok", 対応済み: "ok", 既読: "off", 失敗: "ng", 取り消し: "off", 未対応: "ng", 未読: "run",
  待ち: "run", 実行中: "run", 対応中: "run", 受付済み: "ok", 予定: "run", 高: "ng", 中: "run", 低: "off",
};

export function Status({ s }: { s: string }) {
  return <span className={`status ${TONE[s] ?? "run"}`}>{s}</span>;
}

export function errorText(e: unknown, fallback = "処理できませんでした。しばらくしてからやり直してください"): string {
  return e instanceof ApiError ? e.message : fallback;
}

export function todayIso(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ジョブの進み具合（段階3の画面で使う）。失敗・取り消しのときは、その文を出す
export function JobProgress({ job, label }: { job: { status: string; progress: number; stage: string; error?: string | null } | null; label: string }) {
  if (!job) return <p className="muted">{label}を始めています…</p>;
  return (
    <div className="job-progress" aria-live="polite">
      <div className="note-head">
        <strong>{label}</strong> <Status s={job.status} />
      </div>
      {(job.status === "待ち" || job.status === "実行中") && (
        <p className="progress-cell">
          <progress max={100} value={job.progress} aria-label={`${label}の進み具合`} /> <span className="small">{job.progress}%</span>
          {job.stage && <span className="small muted">　{job.stage}</span>}
        </p>
      )}
      {job.status === "失敗" && <p className="error" role="alert">{job.error || "処理できませんでした"}</p>}
      {job.status === "取り消し" && <p className="muted">取り消されました。</p>}
    </div>
  );
}

export function bytes(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}GB`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}MB`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}KB`;
  return `${n}B`;
}

// URL の ?name= を変える（画面を読み込み直しても、同じ予定・議事録を開けるように）
export function setQuery(name: string, value: string | null): void {
  const q = new URLSearchParams(window.location.search);
  if (value) q.set(name, value);
  else q.delete(name);
  const s = q.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${s ? `?${s}` : ""}`);
}

export const queryParam = (name: string) => new URLSearchParams(window.location.search).get(name);
