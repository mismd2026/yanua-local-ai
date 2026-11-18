// S-21 PDF の変換（総務）。PDF を選ぶ（アップロードか受信箱）→ Word / Excel / 会社の様式に変換（ジョブ）→ ダウンロード
// - 結果のファイルは、変換した本人だけがダウンロードできる。24時間で消える（API の側で守る）
import { FormEvent, useEffect, useState } from "react";
import { api, fileUrl, InboxFile, Job, PaperRule, uploadFile, WorkFile } from "../api";
import { useJob } from "../useJob";
import { bytes, errorText, JobProgress, time } from "../ui";

type Source = "upload" | "inbox";

export default function PaperConvert() {
  const [source, setSource] = useState<Source>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [inbox, setInbox] = useState<InboxFile[]>([]);
  const [inboxName, setInboxName] = useState("");
  const [rules, setRules] = useState<PaperRule[]>([]);
  const [to, setTo] = useState<"docx" | "xlsx">("docx");
  const [form, setForm] = useState(false);
  const [rule, setRule] = useState("");
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState<number | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [done, setDone] = useState<Job | null>(null);

  useEffect(() => {
    Promise.all([api<{ files: InboxFile[] }>("GET", "/api/paper/inbox"), api<{ rules: PaperRule[] }>("GET", "/api/paper/rules")])
      .then(([i, r]) => {
        setInbox(i.files);
        setInboxName(i.files[0]?.name ?? "");
        setRules(r.rules);
      })
      .catch((e) => setError(errorText(e, "受信箱・ルールを読み込めませんでした")));
  }, []);

  const { job } = useJob(jobId, (j) => setDone(j));
  // 会社の様式は、変換先（docx・xlsx）の様式があるルールだけを選べる
  const withTemplate = rules.filter((r) => r.templates.includes(to));
  useEffect(() => {
    if (rule && !withTemplate.some((r) => r.name === rule)) setRule("");
  }, [to, rule, withTemplate]);

  const running = uploading !== null || (!!jobId && !done);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setDone(null);
    setJobId(null);
    try {
      let body: Record<string, unknown> = { to, form, rule: form && rule ? rule : null };
      if (source === "upload") {
        if (!file) return setError("PDF を選んでください");
        setUploading(0);
        const up: WorkFile = await uploadFile("pdf", file, setUploading);
        body = { ...body, file_id: up.file_id };
      } else {
        if (!inboxName) return setError("受信箱の PDF を選んでください");
        body = { ...body, inbox: inboxName };
      }
      setUploading(null);
      const j = await api<Job>("POST", "/api/paper/convert", body);
      setJobId(j.job_id);
    } catch (err) {
      setError(errorText(err, "変換を始められませんでした"));
    } finally {
      setUploading(null);
    }
  };

  const results = (done?.status === "完了" ? (done.result?.files as WorkFile[] | undefined) : undefined) ?? [];

  return (
    <>
      <h1 className="page-title">PDF の変換</h1>
      <div className="jobs-page">
        <section className="card">
          <form className="form" onSubmit={submit}>
            <fieldset className="form-row">
              <legend>変換する PDF</legend>
              <label className="check">
                <input type="radio" name="source" checked={source === "upload"} onChange={() => setSource("upload")} />
                パソコンの PDF を送る（50MB まで）
              </label>
              {source === "upload" && (
                <input type="file" accept="application/pdf,.pdf" aria-label="変換する PDF"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)} disabled={running} />
              )}
              <label className="check">
                <input type="radio" name="source" checked={source === "inbox"} onChange={() => setSource("inbox")} disabled={inbox.length === 0} />
                受信箱の PDF を使う{inbox.length === 0 && "（受信箱に PDF はありません）"}
              </label>
              {source === "inbox" && (
                <select value={inboxName} onChange={(e) => setInboxName(e.target.value)} aria-label="受信箱の PDF" disabled={running}>
                  {inbox.map((f) => <option key={f.name} value={f.name}>{f.name}（{bytes(f.size)}）</option>)}
                </select>
              )}
            </fieldset>
            <div className="form-grid">
              <div className="form-row">
                <label htmlFor="c-to">変換先</label>
                <select id="c-to" value={to} onChange={(e) => setTo(e.target.value as "docx" | "xlsx")} disabled={running}>
                  <option value="docx">Word（.docx）</option>
                  <option value="xlsx">Excel（.xlsx）</option>
                </select>
              </div>
              <div className="form-row">
                <label className="check">
                  <input type="checkbox" checked={form} onChange={(e) => setForm(e.target.checked)} disabled={running} />
                  会社の様式に差し込む
                </label>
                {form && (
                  <select value={rule} onChange={(e) => setRule(e.target.value)} aria-label="様式を使うルール" disabled={running}>
                    <option value="">書類の種類を自動で見分ける</option>
                    {withTemplate.map((r) => <option key={r.name} value={r.name}>{r.name}の様式</option>)}
                  </select>
                )}
              </div>
            </div>
            {uploading !== null && (
              <p className="progress-cell">
                <progress max={100} value={uploading} aria-label="アップロードの進み具合" /> <span className="small">送っています {uploading}%</span>
              </p>
            )}
            {error && <p className="error" role="alert">{error}</p>}
            <div><button type="submit" className="primary" disabled={running}>変換する</button></div>
          </form>
        </section>
        {jobId && (
          <section className="card" aria-label="変換の結果">
            <JobProgress job={done ?? job} label="PDF の変換" />
            {done?.status === "完了" && (
              <>
                {typeof done.result?.message === "string" && <p role="status">{done.result.message}</p>}
                <ul className="result-list">
                  {results.map((f) => (
                    <li key={f.file_id}>
                      <a href={fileUrl(f.file_id)} download={f.name}>{f.name}</a>
                      <span className="small muted">（{bytes(f.size)}・{time(f.expires_at)} まで）</span>
                    </li>
                  ))}
                </ul>
                <p className="small muted">変換したファイルは、あなただけがダウンロードできます。24時間で消えます。</p>
              </>
            )}
          </section>
        )}
      </div>
    </>
  );
}
