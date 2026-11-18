// 1つのジョブの進み具合を、終わるまで数秒ごとに読み直す（段階3の画面で使う）
import { useEffect, useRef, useState } from "react";
import { api, Job, RUNNING_STATUSES } from "./api";
import { errorText } from "./ui";

export const JOB_POLL_MS = 1500;

export function useJob(jobId: string | null, onFinished?: (job: Job) => void): { job: Job | null; error: string } {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const finished = useRef(onFinished);
  finished.current = onFinished;

  useEffect(() => {
    setJob(null);
    setError("");
    if (!jobId) return;
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      try {
        const j = await api<Job>("GET", `/api/jobs/${encodeURIComponent(jobId)}`);
        if (!alive) return;
        setJob(j);
        if (RUNNING_STATUSES.includes(j.status)) timer = window.setTimeout(load, JOB_POLL_MS);
        else finished.current?.(j);
      } catch (e) {
        if (alive) setError(errorText(e, "ジョブの進み具合を読み込めませんでした"));
      }
    };
    void load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [jobId]);

  return { job, error };
}
