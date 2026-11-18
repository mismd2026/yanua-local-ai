// K-047 仕分けルールの下書き・保存・前の版（S-20 の中で使う。総務だけ）
// - 下書きは pdf-rule-draft と同じ処理（ジョブ）で作り、画面で直してから保存する
// - 保存の前に、サーバーがルールの読み込みと同じ検証をかける。誤りは項目の横に出す（where の位置）
// - 保存するたびに前の版が残り、元に戻せる（段階4の設定の画面 S-52 と同じ考え方）
import { useEffect, useState } from "react";
import { api, ApiError, Job, RULE_FIELDS, RuleDraft, RuleVersion, SettingStatus } from "../api";
import { useJob } from "../useJob";
import { errorText, JobProgress, time } from "../ui";

export type DraftSource = { plan_id: string; index: number; file: string } | { inbox: string; file: string };

type Problems = Record<string, string[]>;

const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

// 仕分けの画面の上に出す知らせ：画面で保存したルールを使っていて、そのあと既定のルールが更新されたとき
export function DefaultRulesNotice({ tick }: { tick: number }) {
  const [status, setStatus] = useState<SettingStatus | null>(null);
  useEffect(() => {
    api<SettingStatus>("GET", "/api/paper/rules/status").then(setStatus).catch(() => setStatus(null));
  }, [tick]);
  if (!status?.default_changed) return null;
  return (
    <p className="warn notice-box" role="status">
      ⚠ 既定のルールが更新されています{status.default_updated_at && `（${time(status.default_updated_at)}）`}。
      今は画面で保存したルールを使っているので、既定の更新は反映されていません。
      新しい既定を使うときは、「ルールの版」の「既定に戻す」を使ってください（画面で足したルールは、版として残ります）。
    </p>
  );
}

// 下書きを作るジョブを始めて、できたら編集の画面を出す
export function RuleDraftPanel({ source, onClose, onSaved }: { source: DraftSource; onClose: () => void; onSaved: (version: number) => void }) {
  const [name, setName] = useState("");
  const [useLlm, setUseLlm] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ draft: RuleDraft; source: string; excerpt: string } | null>(null);
  const { job } = useJob(jobId, (j) => {
    if (j.status === "完了" && j.result) setResult(j.result as unknown as { draft: RuleDraft; source: string; excerpt: string });
  });

  const start = async () => {
    setError("");
    setResult(null);
    try {
      const { file: _file, ...where } = source;
      const j = await api<Job>("POST", "/api/paper/rule-drafts", { ...where, name: name.trim() || null, use_llm: useLlm });
      setJobId(j.job_id);
    } catch (e) {
      setError(errorText(e, "下書きを作れませんでした"));
    }
  };

  return (
    <section className="card" aria-label="ルールの下書き">
      <div className="row-between">
        <h2>ルールの下書き：{source.file}</h2>
        <button className="link" onClick={onClose}>閉じる</button>
      </div>
      {!result && (
        <>
          <p className="small muted">この PDF の文字から、新しい種類の書類のルールの下書きを作ります。下書きは、確かめて直してから保存します。</p>
          <div className="form-grid">
            <div className="form-row">
              <label htmlFor="d-name">書類の種類の名前（任意。空なら書類から推測）</label>
              <input id="d-name" type="text" maxLength={60} value={name} onChange={(e) => setName(e.target.value)} disabled={!!jobId && !job?.finished_at} />
            </div>
          </div>
          <label className="check">
            <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
            見分ける言葉を、社内の LLM にも挙げさせる（時間がかかります）
          </label>
          <div className="spaced">
            <button className="primary" onClick={() => void start()} disabled={!!jobId && !job?.finished_at}>下書きを作る</button>
          </div>
          {error && <p className="error" role="alert">{error}</p>}
          {jobId && <JobProgress job={job} label="ルールの下書き作り" />}
        </>
      )}
      {result && <RuleEditor draft={result.draft} excerpt={result.excerpt} onSaved={onSaved} />}
    </section>
  );
}

function RuleEditor({ draft, excerpt, onSaved }: { draft: RuleDraft; excerpt: string; onSaved: (version: number) => void }) {
  const [name, setName] = useState(draft.name);
  const [any, setAny] = useState(draft.keywords.any.join("\n"));
  const [all, setAll] = useState(draft.keywords.all.join("\n"));
  const [none, setNone] = useState(draft.keywords.none.join("\n"));
  const [filename, setFilename] = useState(draft.filename);
  const [folder, setFolder] = useState(draft.folder);
  const [priority, setPriority] = useState(String(draft.priority));
  const [fields, setFields] = useState<[string, string][]>(Object.entries(draft.fields));
  const [problems, setProblems] = useState<Problems>({});
  const [general, setGeneral] = useState("");
  const [checked, setChecked] = useState("");
  const [busy, setBusy] = useState(false);

  const rule = () => ({
    name: name.trim(),
    keywords: { any: lines(any), all: lines(all), none: lines(none) },
    fields: Object.fromEntries(fields.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v])),
    filename: filename.trim(),
    folder: folder.trim(),
    // 数字でないものは、そのまま送ってサーバーの検証で知らせる
    priority: /^-?\d+$/.test(priority.trim()) ? Number(priority.trim()) : priority,
  });

  const send = async (path: string) => {
    setBusy(true);
    setProblems({});
    setGeneral("");
    setChecked("");
    try {
      const r = await api<{ ok?: boolean; version?: number }>("POST", path, { rule: rule() });
      return r;
    } catch (e) {
      if (e instanceof ApiError && e.details.length) {
        const p: Problems = {};
        for (const d of e.details) (p[d.where] ??= []).push(d.message);
        setProblems(p);
        setGeneral(`${e.message}（${e.details.length}件）`);
      } else {
        setGeneral(errorText(e, "確かめられませんでした"));
      }
      return null;
    } finally {
      setBusy(false);
    }
  };

  const changed = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setChecked("");
  };

  // where（例：keywords.any、fields.invoice_no）に当たる誤りを、その項目の下に出す
  const Err = ({ at }: { at: string }) => {
    const msgs = Object.entries(problems).filter(([w]) => w === at || w.startsWith(`${at}.`)).flatMap(([, m]) => m);
    return msgs.length ? <>{msgs.map((m, i) => <div key={i} className="small error" role="alert">{m}</div>)}</> : null;
  };

  return (
    <div className="form">
      <details>
        <summary>PDF から読み取った文字（見分ける言葉を選ぶときの参考）</summary>
        <pre className="mono small excerpt">{excerpt || "（文字がありません）"}</pre>
      </details>
      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="r-name">ルールの名前（書類の種類）</label>
          <input id="r-name" type="text" maxLength={60} value={name} onChange={(e) => changed(setName)(e.target.value)} />
          <Err at="name" />
        </div>
        <div className="form-row">
          <label htmlFor="r-priority">優先度（大きいほど先に当てる）</label>
          <input id="r-priority" type="text" inputMode="numeric" value={priority} onChange={(e) => changed(setPriority)(e.target.value)} />
          <Err at="priority" />
        </div>
      </div>
      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="r-any">どれか1つがあれば当てる言葉（1行に1つ）</label>
          <textarea id="r-any" rows={3} value={any} onChange={(e) => changed(setAny)(e.target.value)} />
          <Err at="keywords.any" />
        </div>
        <div className="form-row">
          <label htmlFor="r-all">すべてあれば当てる言葉（1行に1つ）</label>
          <textarea id="r-all" rows={3} value={all} onChange={(e) => changed(setAll)(e.target.value)} />
          <Err at="keywords.all" />
        </div>
        <div className="form-row">
          <label htmlFor="r-none">あれば当てない言葉（1行に1つ）</label>
          <textarea id="r-none" rows={3} value={none} onChange={(e) => changed(setNone)(e.target.value)} />
          <Err at="keywords.none" />
        </div>
      </div>
      {problems.keywords && <div className="small error" role="alert">{problems.keywords.join("　")}</div>}
      <div className="form-row">
        <label htmlFor="r-filename">ファイル名</label>
        <input id="r-filename" type="text" value={filename} onChange={(e) => changed(setFilename)(e.target.value)} />
        <Err at="filename" />
      </div>
      <div className="form-row">
        <label htmlFor="r-folder">保存先のフォルダ</label>
        <input id="r-folder" type="text" value={folder} onChange={(e) => changed(setFolder)(e.target.value)} />
        <Err at="folder" />
      </div>
      <p className="small muted">
        使える項目：{RULE_FIELDS.map(([k, l]) => `{${k}}（${l}）`).join("・")}
        {fields.some(([k]) => k.trim()) && `・${fields.filter(([k]) => k.trim()).map(([k]) => `{${k.trim()}}`).join("・")}`}
      </p>
      <div className="form-row">
        <span className="small"><strong>独自の項目（任意）</strong>：書類から取り出す項目と、正規表現（かっこの中を取り出す）</span>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>項目名（英数字）</th><th>正規表現</th><th></th></tr></thead>
            <tbody>
              {fields.map(([k, v], i) => (
                <tr key={i}>
                  <td><input type="text" value={k} aria-label={`独自の項目${i + 1}の名前`}
                    onChange={(e) => changed(setFields)(fields.map((f, j) => (j === i ? [e.target.value, f[1]] : f)))} /></td>
                  <td>
                    <input type="text" value={v} aria-label={`独自の項目${i + 1}の正規表現`}
                      onChange={(e) => changed(setFields)(fields.map((f, j) => (j === i ? [f[0], e.target.value] : f)))} />
                    <Err at={`fields.${k.trim()}`} />
                  </td>
                  <td><button type="button" className="link" onClick={() => changed(setFields)(fields.filter((_, j) => j !== i))}>消す</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div><button type="button" onClick={() => changed(setFields)([...fields, ["", ""]])}>項目を足す</button></div>
      </div>
      {problems[""] && <div className="small error" role="alert">{problems[""].join("　")}</div>}
      <div className="toolbar">
        <button type="button" disabled={busy} onClick={async () => { if (await send("/api/paper/rules/validate")) setChecked("問題はありません。保存できます。"); }}>
          確かめる
        </button>
        <button type="button" className="primary" disabled={busy} onClick={async () => {
          const r = await send("/api/paper/rules");
          if (r?.version) onSaved(r.version);
        }}>
          {busy ? "確かめています…" : "保存する"}
        </button>
        {checked && <span role="status" className="small">{checked}</span>}
      </div>
      {general && <p className="error" role="alert">{general}</p>}
      <p className="small muted">保存すると、前のルールは版として残り、「ルールの版」から元に戻せます。</p>
    </div>
  );
}

// ルールの版の一覧と、元に戻す
export function RuleHistory({ tick, onRestored }: { tick: number; onRestored: () => void }) {
  const [versions, setVersions] = useState<RuleVersion[] | null>(null);
  const [current, setCurrent] = useState<number | null>(null);
  const [status, setStatus] = useState<SettingStatus | null>(null);
  const [askingReset, setAskingReset] = useState(false);
  const [asking, setAsking] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api<{ versions: RuleVersion[]; current: number | null; status: SettingStatus }>("GET", "/api/paper/rules/history")
      .then((r) => {
        setVersions(r.versions);
        setCurrent(r.current);
        setStatus(r.status);
        setError("");
      })
      .catch((e) => setError(errorText(e, "ルールの版を読み込めませんでした")));
  }, [tick, reload]);

  const restore = async (v: number) => {
    setBusy(true);
    setError("");
    try {
      const r = await api<{ version: number }>("POST", `/api/paper/rules/history/${v}/restore`, {});
      setMessage(`版 ${v} の中身に戻しました（新しい版 ${r.version}）。`);
      setReload((n) => n + 1);
      onRestored();
    } catch (e) {
      setError(errorText(e, "元に戻せませんでした"));
    } finally {
      setBusy(false);
      setAsking(null);
    }
  };

  const reset = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await api<{ version: number }>("POST", "/api/paper/rules/reset", {});
      setMessage(`既定のルールに戻しました（版 ${r.version}）。画面で足したルールは、版として残っています。`);
      setReload((n) => n + 1);
      onRestored();
    } catch (e) {
      setError(errorText(e, "既定に戻せませんでした"));
    } finally {
      setBusy(false);
      setAskingReset(false);
    }
  };

  return (
    <section className="card">
      <h2>ルールの版</h2>
      <p className="small muted">
        画面から保存したルールは、既定のファイルとは別に、全部の写しとして保存します（既定は書き換えません）。
        保存するたびに版が残り、前の版に戻せます。戻すと、その中身が新しい版になります（戻したことも、また戻せます）。
      </p>
      {status && (
        <div className="toolbar">
          <span>今使っているルール：<strong>{status.using === "saved" ? "画面で保存したもの" : "既定"}</strong></span>
          {status.using === "saved" && (askingReset ? (
            <span className="confirm">
              <span className="small">画面で保存したルールをやめて、既定のルールを使います。</span>
              <button className="primary" disabled={busy} onClick={() => void reset()}>はい、既定に戻す</button>
              <button disabled={busy} onClick={() => setAskingReset(false)}>やめる</button>
            </span>
          ) : (
            <button onClick={() => setAskingReset(true)}>既定に戻す</button>
          ))}
        </div>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      {versions?.length === 0 && <p className="muted">まだ画面から保存したことはありません。</p>}
      {versions && versions.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>版</th><th>保存した時刻</th><th>保存した人</th><th>したこと</th><th></th></tr></thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.version}>
                  <td className="nowrap">{v.version}{v.version === current && <span className="small muted">（今の版）</span>}</td>
                  <td className="small">{time(v.saved_at)}</td>
                  <td>{v.saved_by_name}</td>
                  <td>{v.action}</td>
                  <td className="nowrap">
                    {v.version !== current && (asking === v.version ? (
                      <span className="confirm">
                        <span className="small">今のルールを、版 {v.version} の中身にします。</span>
                        <button className="primary" disabled={busy} onClick={() => void restore(v.version)}>はい、戻す</button>
                        <button disabled={busy} onClick={() => setAsking(null)}>やめる</button>
                      </span>
                    ) : (
                      <button className="link" onClick={() => setAsking(v.version)} aria-label={`版 ${v.version} に戻す`}>この版に戻す</button>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
