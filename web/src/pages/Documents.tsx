// S-50 文書（documents.manage）。文書ごとに「だれに見せるか」を決める（docs/access-model.md）
// - 文書が持つもの：持ち主の部署・公開先（部署・グループ・全社）・見るのに要る権限（既定 documents.view）・社外公開か
// - 扱えるのは、documents.manage の対象に含まれる部署（持ち主）の文書だけ。守りは API の側
// - 公開先と要る権限は、中身を変えずに直せる（本文は作り直さない）。外した文書は「外したもの」で見られ、戻せる
// - 変える前に、見られるようになる人・見られなくなる人を、グループごとの人数で出す（試しに当てるだけ。保存はしない）
import { useCallback, useEffect, useState } from "react";
import { AdminDocument, AdminDocuments, api, ApiError, DocImpact, Job, PublishTarget } from "../api";
import { useJob } from "../useJob";
import { errorText, JobProgress } from "../ui";

const label = (t: PublishTarget) => (t.kind === "全社" ? "全社" : `${t.kind}：${t.name}`);

export default function DocumentsPage() {
  const [removed, setRemoved] = useState(false);
  const [view, setView] = useState<AdminDocuments | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const [owner, setOwner] = useState("");
  const [target, setTarget] = useState("");
  const [selected, setSelected] = useState("");
  const [message, setMessage] = useState("");
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    api<AdminDocuments>("GET", `/api/admin/documents${removed ? "?removed=true" : ""}`)
      .then((v) => { setView(v); setError(""); })
      .catch((e) => setError(errorText(e, "文書を読み込めませんでした")));
  }, [removed, tick]);

  const docs = (view?.documents ?? []).filter((d) => (!owner || d.owner === owner) && (!target || d.targets.includes(target)));
  const doc = view?.documents.find((d) => d.doc_id === selected);
  const done = (text: string) => { setMessage(text); setSelected(""); reload(); };
  return (
    <>
      <h1 className="page-title">文書</h1>
      <div className="jobs-page">
        {error && <p className="error" role="alert">{error}</p>}
        <section className="card" aria-label="文書の一覧">
          <p className="small muted">
            文書を見られるのは、見るのに要る権限を、公開先を含む対象で持っている人だけです（公開先「全社」は、要る権限を持つ全員）。
            あなたが扱えるのは、持ち主が {view ? (view.owners.join("・") || "なし") : "…"} の文書です。変更はすべて監査ログに残ります。
          </p>
          <div className="tabs" role="tablist" aria-label="文書の区切り">
            {([[false, "取り込んだもの"], [true, "外したもの"]] as const).map(([r, text]) => (
              <button key={text} role="tab" aria-selected={removed === r} className={`tab${removed === r ? " on" : ""}`}
                onClick={() => { setRemoved(r); setSelected(""); setMessage(""); }}>{text}</button>
            ))}
          </div>
          {message && <p className="notice" role="status">{message}</p>}
          {view && (
            <p>
              <label>持ち主の部署{" "}
                <select value={owner} onChange={(e) => setOwner(e.target.value)}>
                  <option value="">すべて</option>
                  {view.owners.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </label>{" "}
              <label>公開先{" "}
                <select value={target} onChange={(e) => setTarget(e.target.value)}>
                  <option value="">すべて</option>
                  {view.publish_targets.map((t) => <option key={t.target_id} value={t.target_id}>{label(t)}</option>)}
                </select>
              </label>
            </p>
          )}
          {view && docs.length === 0 && <p className="muted">{removed ? "外した文書はありません。" : "文書はありません。"}</p>}
          {view && docs.length > 0 && (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>題名</th><th>持ち主の部署</th><th>公開先</th><th>要る権限</th><th>{removed ? "外した日" : "取り込んだ日"}</th></tr></thead>
                <tbody>
                  {docs.map((d) => (
                    <tr key={d.doc_id} className={d.doc_id === selected ? "selected" : ""}>
                      <td><button className="link" onClick={() => { setSelected(d.doc_id); setMessage(""); }}>{d.title}</button>
                        <span className="small muted">（{d.doc_id}）</span></td>
                      <td>{d.owner || "—"}</td>
                      <td>{d.target_names.join("・")}{d.external && <span className="small">　＋社外公開</span>}</td>
                      <td><code>{d.required}</code></td>
                      <td className="small">{(removed ? d.removed_at : d.indexed_at)?.slice(0, 10)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        {view && doc && <Editor key={doc.doc_id} doc={doc} view={view} onDone={done} />}
        {view && !removed && <IngestForm view={view} onDone={reload} />}
      </div>
    </>
  );
}

function Impact({ p }: { p: DocImpact }) {
  if (p.too_many) return <p className="small">人数が多い（{p.checked}人）ので、見られる人の増減は数えていません。</p>;
  return (
    <>
      <p>見られるようになる人：<strong>{p.gain}人</strong>　見られなくなる人：<strong>{p.lose}人</strong></p>
      {p.groups.length > 0 && (
        <div className="table-wrap">
          <table className="table" aria-label="グループごとの人数">
            <thead><tr><th>グループ</th><th>メンバー</th><th>見られるようになる</th><th>見られなくなる</th><th>変えたあと見られる</th></tr></thead>
            <tbody>
              {p.groups.map((g) => (
                <tr key={g.name}><td>{g.name}</td><td>{g.members}人</td><td>{g.gain}人</td><td>{g.lose}人</td><td>{g.can_see}人</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {p.gain + p.lose === 0 && <p className="small muted">見られる人は変わりません。</p>}
    </>
  );
}

function TargetChecks({ view, value, onChange }: { view: AdminDocuments; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <fieldset>
      <legend>公開先（1つ以上）</legend>
      {view.publish_targets.map((t) => (
        <label key={t.target_id} style={{ marginRight: "1em", display: "inline-block" }}>
          <input type="checkbox" style={{ width: "auto" }} checked={value.includes(t.target_id)}
            onChange={(e) => onChange(e.target.checked ? [...value, t.target_id] : value.filter((x) => x !== t.target_id))} /> {label(t)}
        </label>
      ))}
    </fieldset>
  );
}

function RequiredSelect({ view, value, onChange }: { view: AdminDocuments; value: string; onChange: (v: string) => void }) {
  return (
    <label>見るのに要る権限{" "}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {view.permissions.map((p) => <option key={p.name} value={p.name}>{p.name}（{p.does}）</option>)}
      </select>
    </label>
  );
}

function Editor({ doc, view, onDone }: { doc: AdminDocument; view: AdminDocuments; onDone: (message: string) => void }) {
  const [targets, setTargets] = useState(doc.targets);
  const [required, setRequired] = useState(doc.required);
  const [external, setExternal] = useState(doc.external);
  const [pending, setPending] = useState<{ action: string; impact: DocImpact } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // 確かめで「やめる」：欄を保存されている値に戻す
  const cancel = () => { setTargets(doc.targets); setRequired(doc.required); setExternal(doc.external); setPending(null); };
  const id = encodeURIComponent(doc.doc_id);
  const publication = { targets, required, external };

  const check = async (action: string) => {
    setBusy(true);
    setError("");
    setPending(null);
    try {
      const body = action === "publish" ? { doc_id: doc.doc_id, action, ...publication } : { doc_id: doc.doc_id, action };
      setPending({ action, impact: await api<DocImpact>("POST", "/api/admin/documents/preview", body) });
    } catch (e) {
      setError(errorText(e, "確かめられませんでした"));
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const r = pending.action === "publish"
        ? await api<{ detail: string }>("PATCH", `/api/admin/documents/${id}`, publication)
        : await api<{ detail: string }>("POST", `/api/admin/documents/${id}/${pending.action}`);
      onDone(`保存しました：${r.detail}`);
    } catch (e) {
      setPending(null);
      setError(errorText(e, "保存できませんでした"));
    } finally {
      setBusy(false);
    }
  };
  const SAVE: Record<string, string> = { publish: "保存する", remove: "外す", restore: "戻す" };
  return (
    <section className="card" aria-label={`文書「${doc.title}」`}>
      <h2>{doc.title}<span className="small muted">（{doc.doc_id}・持ち主 {doc.owner || "なし"}）</span></h2>
      <p className="small muted">
        公開先を{doc.decided_by === "screen" ? "画面で決めました" : "ファイルの先頭の access で決めています（画面で直すと、取り込み直しても画面のものを使います）"}。
        中身（本文）はここでは変わりません。
      </p>
      {doc.removed_at ? (
        <p className="notice">この文書は外しています（{doc.removed_at.slice(0, 10)}、{doc.removed_by}）。検索にも一覧にも出ません。</p>
      ) : (
        <>
          <TargetChecks view={view} value={targets} onChange={(v) => { setTargets(v); setPending(null); }} />
          <p><RequiredSelect view={view} value={required} onChange={(v) => { setRequired(v); setPending(null); }} /></p>
          <p>
            <label>
              <input type="checkbox" style={{ width: "auto" }} checked={external} onChange={(e) => { setExternal(e.target.checked); setPending(null); }} />
              {" "}社外公開（社外向けの受付の答えに使う）
            </label>
          </p>
        </>
      )}
      <p>
        {!doc.removed_at && <button className="primary" disabled={busy || targets.length === 0} onClick={() => void check("publish")}>変更を確かめる</button>}{" "}
        {!doc.removed_at && <button disabled={busy} onClick={() => void check("remove")}>この文書を外す</button>}
        {doc.removed_at && <button className="primary" disabled={busy} onClick={() => void check("restore")}>この文書を戻す</button>}
      </p>
      {error && <p className="error" role="alert" aria-label="変えられない理由">{error}</p>}
      {pending && (
        <div className="notice-box" role="region" aria-label="変更の確かめ">
          <p><strong>この変更：</strong>{pending.impact.detail}</p>
          <Impact p={pending.impact} />
          <p>
            <button className="primary" disabled={busy} onClick={() => void save()}>{SAVE[pending.action]}</button>{" "}
            <button disabled={busy} onClick={cancel}>やめる</button>
          </p>
        </div>
      )}
    </section>
  );
}

function IngestForm({ view, onDone }: { view: AdminDocuments; onDone: () => void }) {
  const [sources, setSources] = useState<{ path: string; label: string }[] | null>(null);
  const [source, setSource] = useState("");
  const [mode, setMode] = useState<"file" | "chosen">("file");
  const [owner, setOwner] = useState(view.owners[0] ?? "");
  const [targets, setTargets] = useState<string[]>([]);
  const [required, setRequired] = useState("documents.view");
  const [external, setExternal] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const { job } = useJob(jobId, () => onDone());

  useEffect(() => {
    api<{ sources: { path: string; label: string }[] }>("GET", "/api/documents/ingest/sources")
      .then((r) => { setSources(r.sources); setSource(r.sources[0]?.path ?? ""); })
      .catch((e) => setError(errorText(e, "取り込めるフォルダを読み込めませんでした")));
  }, []);

  const start = async () => {
    setError("");
    try {
      const body = mode === "file" ? { source } : { source, mode, owner, targets, required, external };
      setJobId((await api<Job>("POST", "/api/documents/ingest", body)).job_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "取り込みを始められませんでした");
    }
  };
  const result = job?.status === "完了" ? (job.result as Record<string, string[]>) : null;
  return (
    <section className="card" aria-label="文書の取り込み">
      <h2>文書の取り込み</h2>
      <p className="small muted">
        フォルダの中の文書を取り込みます。前に同じフォルダから取り込んだ文書は、変わったものだけを反映し、なくなったものは消します。
        下で選ぶ公開先は、新しく足す文書だけに使います（前からある文書の公開先は変えません）。
      </p>
      {sources && sources.length === 0 && <p className="muted">取り込めるフォルダがありません（設定の ingest_roots）。</p>}
      {sources && sources.length > 0 && (
        <>
          <p>
            <label>フォルダ{" "}
              <select value={source} onChange={(e) => setSource(e.target.value)}>
                {sources.map((s) => <option key={s.path} value={s.path}>{s.label}</option>)}
              </select>
            </label>
          </p>
          <fieldset>
            <legend>新しい文書の持ち主の部署・公開先</legend>
            <label style={{ marginRight: "1em" }}>
              <input type="radio" name="ingest-mode" style={{ width: "auto" }} checked={mode === "file"} onChange={() => setMode("file")} />
              {" "}ファイルの先頭に書いたもの（owner・access）
            </label>
            <label>
              <input type="radio" name="ingest-mode" style={{ width: "auto" }} checked={mode === "chosen"} onChange={() => setMode("chosen")} />
              {" "}ここで選ぶ
            </label>
          </fieldset>
          {mode === "chosen" && (
            <>
              <p>
                <label>持ち主の部署{" "}
                  <select value={owner} onChange={(e) => setOwner(e.target.value)}>
                    {view.owners.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                </label>
              </p>
              <TargetChecks view={view} value={targets} onChange={setTargets} />
              <p><RequiredSelect view={view} value={required} onChange={setRequired} /></p>
              <p>
                <label><input type="checkbox" style={{ width: "auto" }} checked={external} onChange={(e) => setExternal(e.target.checked)} /> 社外公開</label>
              </p>
            </>
          )}
          <button className="primary" disabled={!source || (mode === "chosen" && (!owner || targets.length === 0)) || job?.status === "実行中"}
            onClick={() => void start()}>取り込みを始める</button>
        </>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {jobId && <JobProgress job={job} label="文書の取り込み" />}
      {result && (
        <ul className="small" aria-label="取り込みの結果">
          {([["added", "足した"], ["updated", "取り込み直した"], ["unchanged", "変わらない"], ["deleted", "消した"], ["errors", "取り込めなかった"]] as const)
            .map(([k, text]) => (
              <li key={k} className={k === "errors" && result[k]?.length ? "error" : undefined}>
                {text}：{result[k]?.length ?? 0}件{result[k]?.length ? `　${result[k].join("、")}` : ""}
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}
