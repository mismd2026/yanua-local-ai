// S-51 人事データ（取り込みは hr.import、一覧は hr.view）。考え方は yanua/hrimport.py
// - 人事アプリから出したファイル（Excel・CSV・JSON）を上げ、列を Yanua の項目に割り当てる（名前を付けて保存して、次から選べる）
// - 読むのは割り当てた列だけ。在籍状態は、ファイルの値を在籍・休職・退職に当てる。雇用区分も同じ作りで、5つの区分に当てる
// - 取り込みの種類：「一部のファイル」（既定。ファイルにいない人は変えない）か「全員のファイル」（ファイルにいない人はいなくなる）
// - 試す（保存しない）→ 確かめる（入る・変わる・いなくなる人、グループごとの増減、誤りは行番号つき）→ 確定（まとめて1回）
//   全員のファイルで、いなくなる人が今の人数の 10% を超えるときは、「いなくなる人が N人（全体の M%）います」と出して、もう一度押させる
// - 前の取り込みに戻す（戻す前にも試しの結果を出す）
// - 一覧は hr.view の対象の部署の人だけ（守りは API の側）
import { useEffect, useState } from "react";
import { api, HrEmployees, HrInspect, HrMapping, HrOverview, HrPreview, Job, uploadFile } from "../api";
import { useJob } from "../useJob";
import { errorText, JobProgress } from "../ui";

export default function HrPage() {
  const [view, setView] = useState<HrOverview | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"import" | "list">("import");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    api<HrOverview>("GET", "/api/hr/status")
      .then((v) => { setView(v); setError(""); if (!v.can_import) setTab("list"); })
      .catch((e) => setError(errorText(e, "人事データの様子を読み込めませんでした")));
  }, [tick]);
  const reload = () => setTick((t) => t + 1);
  return (
    <>
      <h1 className="page-title">人事データ</h1>
      <div className="jobs-page">
        {error && <p className="error" role="alert">{error}</p>}
        {view && view.problems.length > 0 && (
          <div className="card" role="alert" aria-label="人事データの注意">
            <p><strong>注意</strong>（Yanua は今の人事データのまま動いています）</p>
            <ul>{view.problems.map((p, i) => <li key={i}>{p.where && `${p.where}：`}{p.message}</li>)}</ul>
          </div>
        )}
        {view && (
          <p className="small muted">
            {view.last ? `最後の取り込み：${view.last.imported_at.slice(0, 16).replace("T", " ")}（${view.last.source}）。在籍・休職 ${view.last.active}人、退職 ${view.last.inactive}人` : "人事データはまだ取り込んでいません"}
            。休職中の人：ログイン{view.leave.login ? "できる" : "できない"}、権限「{view.leave.permissions}」（設定の画面の「人事」で変えられます）
          </p>
        )}
        {view && (
          <div className="tabs" role="tablist" aria-label="人事データの区切り">
            {view.can_import && <button role="tab" aria-selected={tab === "import"} className={`tab${tab === "import" ? " on" : ""}`} onClick={() => setTab("import")}>取り込み</button>}
            {view.can_view && <button role="tab" aria-selected={tab === "list"} className={`tab${tab === "list" ? " on" : ""}`} onClick={() => setTab("list")}>一覧</button>}
          </div>
        )}
        {view && tab === "import" && view.can_import && <ImportTab view={view} onDone={reload} />}
        {view && tab === "import" && view.can_import && <RestoreBox view={view} onDone={reload} />}
        {view && tab === "list" && view.can_view && <ListTab />}
      </div>
    </>
  );
}

// ------------------------------------------------ 取り込み

function ImportTab({ view, onDone }: { view: HrOverview; onDone: () => void }) {
  const [fileId, setFileId] = useState("");
  const [fileName, setFileName] = useState("");
  const [uploading, setUploading] = useState<number | null>(null);
  const [info, setInfo] = useState<HrInspect | null>(null);
  const [mapping, setMapping] = useState<HrMapping>({ columns: {}, status_values: {} });
  const [chosen, setChosen] = useState("");
  const [values, setValues] = useState<{ value: string; guess: string }[]>([]);
  const [kinds, setKinds] = useState<{ value: string; guess: string }[]>([]);   // 雇用区分の列に出てくる値
  const [saveName, setSaveName] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [mode, setMode] = useState<"preview" | "commit">("preview");
  const [result, setResult] = useState<HrPreview | null>(null);
  const [tried, setTried] = useState("");     // 試したときの対応づけ（変えたら、もう一度試す）
  const [scope, setScope] = useState<"partial" | "all">("partial");
  const [ack, setAck] = useState(false);      // いなくなる人が多いことを、一度押して確かめた
  const { job } = useJob(jobId, (j: Job) => {
    if (j.status !== "完了") return;
    const r = j.result as unknown as HrPreview;
    if (mode === "commit") {
      setResult(null);
      setMessage(r.committed ? `確定しました：入る ${r.added.count}人・変わる ${r.changed.count}人・いなくなる ${r.removed.count}人` : `確定しませんでした：${r.reason}`);
      onDone();
    } else { setResult(r); setAck(false); }
  });
  const key = JSON.stringify({ fileId, mapping, scope });

  const inspect = async (id: string, sheet?: string, statusColumn?: string, employmentColumn?: string) => {
    const r = await api<HrInspect>("POST", "/api/hr/inspect", { file_id: id, sheet: sheet || null, status_column: statusColumn || null,
      employment_column: employmentColumn || null });
    return r;
  };
  const choose = async (file: File) => {
    setError(""); setMessage(""); setResult(null); setInfo(null);
    try {
      setUploading(0);
      const up = await uploadFile("hr", file, setUploading);
      setFileId(up.file_id); setFileName(up.name);
      const r = await inspect(up.file_id);
      setInfo(r);
      const first = Object.entries(view.mappings).find(([, m]) => Object.values(m.columns).every((c) => r.headers.includes(c)));
      if (first) { setChosen(first[0]); setMapping(first[1]); await loadValues(up.file_id, first[1]); }
      else { setChosen(""); setMapping({ columns: r.draft, status_values: {}, employment_values: {} }); await loadValues(up.file_id, { columns: r.draft }); }
    } catch (e) {
      setError(errorText(e, "ファイルを読み込めませんでした"));
    } finally {
      setUploading(null);
    }
  };
  const loadValues = async (id: string, m: HrMapping) => {
    const col = m.columns.status;
    const kindCol = m.columns.employment;
    if (!col) setValues([]);
    if (!kindCol) setKinds([]);
    if (!col && !kindCol) return;
    const r = await inspect(id, m.sheet, col, kindCol);
    if (col) setValues(r.status_values);
    const ev = r.employment_values ?? [];
    if (kindCol) setKinds(ev);
    setMapping((cur) => ({
      ...cur,
      status_values: col ? Object.fromEntries(r.status_values.map((v) => [v.value, cur.status_values?.[v.value] || m.status_values?.[v.value] || v.guess])) : cur.status_values,
      employment_values: kindCol ? Object.fromEntries(ev.map((v) => [v.value, cur.employment_values?.[v.value] || m.employment_values?.[v.value] || v.guess])) : cur.employment_values,
    }));
  };
  const setColumn = async (field: string, col: string) => {
    const columns = { ...mapping.columns };
    if (col) columns[field] = col; else delete columns[field];
    const next = { ...mapping, columns };
    setMapping(next); setChosen("");
    if (field === "status" || field === "employment") {
      try { await loadValues(fileId, next); } catch (e) { setError(errorText(e, "列の値を読めませんでした")); }
    }
  };
  const pickSaved = async (name: string) => {
    setChosen(name);
    if (!name) return;
    const m = view.mappings[name];
    setMapping(m);
    try { await loadValues(fileId, m); } catch (e) { setError(errorText(e, "在籍状態の値を読めませんでした")); }
  };
  const body = () => {
    const sv = Object.fromEntries(Object.entries(mapping.status_values ?? {}).filter(([, v]) => v));
    const ev = Object.fromEntries(Object.entries(mapping.employment_values ?? {}).filter(([, v]) => v));
    return { file_id: fileId, scope, mapping: { columns: mapping.columns, status_values: mapping.columns.status ? sv : undefined,
      employment_values: mapping.columns.employment ? ev : undefined, sheet: mapping.sheet || undefined } };
  };
  const start = async (m: "preview" | "commit") => {
    setError(""); setMessage("");
    try {
      setMode(m);
      if (m === "preview") { setResult(null); setTried(key); }
      const confirm = m === "commit" && !!result?.removal_warning;
      const j = await api<Job>("POST", m === "preview" ? "/api/hr/preview" : "/api/hr/imports", m === "preview" ? body() : { ...body(), confirm_removal: confirm });
      setJobId(j.job_id);
    } catch (e) {
      setError(errorText(e, "始められませんでした"));
    }
  };
  const save = async () => {
    setError(""); setMessage("");
    try {
      await api("PUT", `/api/hr/mappings/${encodeURIComponent(saveName.trim())}`, body().mapping);  // 種類は対応づけに入れない
      setMessage(`対応づけ「${saveName.trim()}」を保存しました。次からこの名前で選べます`);
      setChosen(saveName.trim());
      onDone();
    } catch (e) {
      setError(errorText(e, "対応づけを保存できませんでした"));
    }
  };
  const running = job !== null && (job.status === "待ち" || job.status === "実行中");
  const missing = view.required.filter((f) => !mapping.columns[f]);
  const unmapped = values.filter((v) => !mapping.status_values?.[v.value]);
  const unmappedKinds = kinds.filter((v) => !mapping.employment_values?.[v.value]);
  return (
    <section className="card" aria-label="人事データの取り込み">
      <h2>取り込み</h2>
      <p className="small muted">
        読むのは、下で割り当てた列だけです（住所・給与などの列がファイルにあっても読みません）。確定すると、本人確認・部署の決まりで入るグループ・権限にそのまま効きます。
      </p>
      <p>
        <label>ファイル（Excel・CSV・JSON）{" "}
          <input type="file" accept=".csv,.xlsx,.xlsm,.json" onChange={(e) => e.target.files?.[0] && choose(e.target.files[0])} disabled={running} />
        </label>
        {uploading !== null && <span className="small">　送っています {uploading}%</span>}
        {fileName && uploading === null && <span className="small muted">　{fileName}（24時間で消えます）</span>}
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="notice" role="status">{message}</p>}
      {info && (
        <>
          <h3>列の対応づけ</h3>
          <p>
            <label>保存した対応づけ{" "}
              <select value={chosen} onChange={(e) => pickSaved(e.target.value)}>
                <option value="">（ここで決める）</option>
                {Object.keys(view.mappings).map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            {info.sheets.length > 1 && (
              <label>　シート{" "}
                <select value={mapping.sheet ?? ""} onChange={(e) => setMapping({ ...mapping, sheet: e.target.value || undefined })}>
                  <option value="">最初のシート</option>
                  {info.sheets.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
            )}
          </p>
          <div className="table-wrap">
            <table className="table" aria-label="列の対応づけ">
              <thead><tr><th>Yanua の項目</th><th>ファイルの列</th></tr></thead>
              <tbody>
                {Object.entries(view.fields).map(([f, label]) => (
                  <tr key={f}>
                    <td>{label}{view.required.includes(f) && <span className="small">（必要）</span>}</td>
                    <td>
                      <select aria-label={label} value={mapping.columns[f] ?? ""} onChange={(e) => setColumn(f, e.target.value)}>
                        <option value="">（使わない）</option>
                        {info.headers.map((h) => <option key={h} value={h}>{h}</option>)}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="small muted">読まない列：{info.headers.filter((h) => !Object.values(mapping.columns).includes(h)).join("・") || "なし"}</p>
          {mapping.columns.status && (
            <fieldset>
              <legend>在籍状態の対応（ファイルの値 → Yanua）</legend>
              {values.length === 0 && <p className="small muted">値が見つかりません。</p>}
              {values.map((v) => (
                <label key={v.value} style={{ marginRight: "1.5em", display: "inline-block" }}>「{v.value}」→{" "}
                  <select aria-label={`在籍状態「${v.value}」`} value={mapping.status_values?.[v.value] ?? ""}
                    onChange={(e) => { setChosen(""); setMapping({ ...mapping, status_values: { ...mapping.status_values, [v.value]: e.target.value } }); }}>
                    <option value="">（選ぶ）</option>
                    {view.statuses.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </label>
              ))}
            </fieldset>
          )}
          {mapping.columns.employment && (
            <fieldset>
              <legend>雇用区分の対応（ファイルの値 → Yanua）</legend>
              {kinds.length === 0 && <p className="small muted">値が見つかりません。</p>}
              {kinds.map((v) => (
                <label key={v.value} style={{ marginRight: "1.5em", display: "inline-block" }}>「{v.value}」→{" "}
                  <select aria-label={`雇用区分「${v.value}」`} value={mapping.employment_values?.[v.value] ?? ""}
                    onChange={(e) => { setChosen(""); setMapping({ ...mapping, employment_values: { ...mapping.employment_values, [v.value]: e.target.value } }); }}>
                    <option value="">（選ぶ）</option>
                    {view.employments.map((s) => <option key={s} value={s}>{s}</option>)}
                  </select>
                </label>
              ))}
              <p className="small muted">雇用区分は人の属性で、権限は持ちません（期限・表示などに使います）。列を使わないときは、今いる人は今のまま、新しく入る人は「社員」です。</p>
            </fieldset>
          )}
          {missing.length > 0 && <p className="error">必要な項目の列を選んでください：{missing.map((f) => view.fields[f]).join("・")}</p>}
          {unmapped.length > 0 && <p className="small">在籍状態「{unmapped.map((v) => v.value).join("」「")}」の当て先が決まっていません（このままだと、その行は誤りになります）</p>}
          {unmappedKinds.length > 0 && <p className="small">雇用区分「{unmappedKinds.map((v) => v.value).join("」「")}」の当て先が決まっていません（このままだと、その行は誤りになります）</p>}
          <p>
            <label>この対応づけに名前を付けて保存{" "}
              <input value={saveName} onChange={(e) => setSaveName(e.target.value)} maxLength={40} style={{ width: "14em" }} />
            </label>{" "}
            <button disabled={!saveName.trim() || missing.length > 0} onClick={save}>保存する</button>
          </p>
          <fieldset>
            <legend>取り込みの種類</legend>
            <label style={{ display: "block" }}><input type="radio" name="hr-scope" style={{ width: "auto" }} checked={scope === "partial"} onChange={() => setScope("partial")} />
              {" "}一部のファイル（ファイルにいない人は変えない）</label>
            <label style={{ display: "block" }}><input type="radio" name="hr-scope" style={{ width: "auto" }} checked={scope === "all"} onChange={() => setScope("all")} />
              {" "}全員のファイル（ファイルにいない人は、いなくなる人として扱う）</label>
          </fieldset>
          <p className="small muted">「使わない」にした よみ・部署コード・メールアドレス・在籍状態は、今いる人の値をそのまま残します（新しく入る人だけ空のまま）。</p>
          <p><button className="primary" disabled={running || missing.length > 0 || !fileId} onClick={() => start("preview")}>試す（保存しない）</button></p>
        </>
      )}
      {jobId && running && <JobProgress job={job} label={mode === "preview" ? "試し" : "確定"} />}
      {job?.status === "失敗" && <p className="error" role="alert">{job.error}</p>}
      {result && (
        <PreviewBox p={result} title="試しの結果" stale={tried !== key} ack={ack}>
          <button className="primary" disabled={!result.ok || tried !== key || running}
            onClick={() => (result.removal_warning && !ack ? setAck(true) : start("commit"))}>確定する</button>{" "}
          <button disabled={running} onClick={() => setResult(null)}>やめる</button>
        </PreviewBox>
      )}
    </section>
  );
}

function People({ label, part }: { label: string; part: { count: number; first: { employee_no: string; name: string; dept: string; status: string; changes?: string[] }[] } }) {
  return (
    <li>
      {label}：<strong>{part.count}人</strong>
      {part.first.length > 0 && (
        <span className="small muted">　（先頭：{part.first.map((x) => `${x.employee_no} ${x.name}` + (x.changes ? `［${x.changes.join("、")}］` : `［${x.dept}・${x.status}］`)).join("　")}{part.count > part.first.length ? " …" : ""}）</span>
      )}
    </li>
  );
}

export function PreviewBox({ p, title, stale, ack, children }: { p: HrPreview; title: string; stale?: boolean; ack?: boolean; children: React.ReactNode }) {
  const by = Object.entries(p.changed.by_field ?? {}).filter(([, n]) => n > 0);
  return (
    <section className="card" aria-label={title}>
      <h3>{title}</h3>
      <p>取り込みの種類：<strong>{p.scope_label}</strong>{p.scope === "partial" && <span className="small muted">（ファイルにいない人は変えません）</span>}</p>
      <p>
        読んだ行：{p.rows}行。在籍 {p.counts["在籍"] ?? 0}人・休職 {p.counts["休職"] ?? 0}人・退職 {p.counts["退職"] ?? 0}人
        {p.ignored_columns.length > 0 && <span className="small muted">（読まなかった列：{p.ignored_columns.join("・")}）</span>}
      </p>
      {p.error_count > 0 && (
        <div role="alert">
          <p className="error">誤りが {p.error_count}件 あるので、確定できません。ファイルか対応づけを直して、もう一度試してください。</p>
          <ul aria-label="誤り">{p.errors.map((e, i) => <li key={i}>{e.line}行目：{e.message}</li>)}</ul>
          {p.error_count > p.errors.length && <p className="small">ほかに {p.error_count - p.errors.length}件</p>}
        </div>
      )}
      {p.error_count === 0 && (
        <>
          <ul aria-label="変わる人">
            <People label="新しく入る人" part={p.added} />
            <People label="変わる人" part={p.changed} />
            <People label="いなくなる人" part={p.removed} />
          </ul>
          {by.length > 0 && <p className="small">変わるところ：{by.map(([k, n]) => `${k} ${n}人`).join("・")}</p>}
          <h4>権限への効き方（入る人・部署や在籍が変わる人・いなくなる人）</h4>
          {p.groups.length === 0 ? <p className="small muted">入るグループ・外れるグループはありません。</p> : (
            <div className="table-wrap">
              <table className="table" aria-label="グループごとの人数">
                <thead><tr><th>グループ</th><th>入る人</th><th>外れる人</th></tr></thead>
                <tbody>{p.groups.map((g) => <tr key={g.group_id}><td>{g.name}</td><td>{g.join}人</td><td>{g.leave}人</td></tr>)}</tbody>
              </table>
            </div>
          )}
          {(p.default_employment ?? 0) > 0 && <p className="small">雇用区分なしで入る人：{p.default_employment}人（社員として入ります）</p>}
          {p.retired_members > 0 && <p className="small">退職・いなくなる {p.retired_members}人は、手で入れたグループからも外れます（在籍に戻っても自動では戻りません）。</p>}
          {p.lockout && <p className="error" role="alert">確定すると、権限を変えられる人（roles.assign）がいなくなるので、確定できません。</p>}
          {p.removal_warning && (
            <p className={ack ? "error" : "small"} role={ack ? "alert" : undefined}>
              {p.removal_warning.message}（今の {p.current_total}人のうち）。{ack ? "よければ、もう一度押してください。" : "確定するときは、もう一度確かめます。"}
            </p>
          )}
        </>
      )}
      {stale && <p className="small">対応づけかファイルを変えたので、もう一度試してから確定してください。</p>}
      <p>{children}</p>
    </section>
  );
}

function RestoreBox({ view, onDone }: { view: HrOverview; onDone: () => void }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [mode, setMode] = useState<"preview" | "restore">("preview");
  const [result, setResult] = useState<HrPreview | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [ack, setAck] = useState(false);
  const { job } = useJob(jobId, (j: Job) => {
    if (j.status !== "完了") return;
    const r = j.result as unknown as HrPreview;
    setAck(false);
    if (mode === "restore") {
      setResult(null);
      setMessage(r.committed ? "1つ前の取り込みに戻しました" : `戻しませんでした：${r.reason}`);
      onDone();
    } else setResult(r);
  });
  const start = async (m: "preview" | "restore") => {
    setError(""); setMessage("");
    try {
      setMode(m);
      if (m === "preview") setResult(null);
      const j = await api<Job>("POST", m === "preview" ? "/api/hr/restore/preview" : "/api/hr/restore",
        m === "preview" ? undefined : { confirm_removal: !!result?.removal_warning });
      setJobId(j.job_id);
    } catch (e) {
      setError(errorText(e, "始められませんでした"));
    }
  };
  const running = job !== null && (job.status === "待ち" || job.status === "実行中");
  return (
    <section className="card" aria-label="前の取り込みに戻す">
      <h2>前の取り込み</h2>
      {view.history.length > 0 && (
        <div className="table-wrap">
          <table className="table" aria-label="取り込みの記録">
            <thead><tr><th>日時</th><th>したこと</th><th>種類</th><th>ファイル</th><th>した人</th><th>入る・変わる・いなくなる</th></tr></thead>
            <tbody>
              {view.history.map((h, i) => (
                <tr key={i}><td className="small">{h.imported_at.slice(0, 16).replace("T", " ")}</td><td>{h.kind}</td><td>{h.scope || "—"}</td><td>{h.source}</td><td>{h.by}</td>
                  <td>{h.added}人・{h.changed}人・{h.removed}人</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small muted">直近 {view.keep}回分の前のデータを残しています（今 {view.backups.length}回分）。戻すと、手で入れたグループのメンバーは戻りません。</p>
      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="notice" role="status">{message}</p>}
      <p><button disabled={view.backups.length === 0 || running} onClick={() => start("preview")}>1つ前に戻す（まず試す）</button></p>
      {jobId && running && <JobProgress job={job} label={mode === "preview" ? "戻す前の試し" : "戻す"} />}
      {job?.status === "失敗" && <p className="error" role="alert">{job.error}</p>}
      {result && (
        <PreviewBox p={result} title="戻す前の試し" ack={ack}>
          <button className="primary" disabled={!result.ok || running}
            onClick={() => (result.removal_warning && !ack ? setAck(true) : start("restore"))}>戻す</button>{" "}
          <button disabled={running} onClick={() => setResult(null)}>やめる</button>
        </PreviewBox>
      )}
    </section>
  );
}

// ------------------------------------------------ 一覧

function ListTab() {
  const [q, setQ] = useState("");
  const [dept, setDept] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<HrEmployees | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => {
      const params = new URLSearchParams({ q, dept, offset: String(offset) });
      api<HrEmployees>("GET", `/api/hr/employees?${params}`)
        .then((d) => { setData(d); setError(""); })
        .catch((e) => setError(errorText(e, "一覧を読み込めませんでした")));
    }, 250);
    return () => window.clearTimeout(t);
  }, [q, dept, offset]);
  return (
    <section className="card" aria-label="社員の一覧">
      <h2>一覧</h2>
      <p className="small muted">見られるのは、あなたの hr.view の対象の部署の人だけです。個人の詳しい情報は持っていません。</p>
      <p>
        <label>番号か名前の一部{" "}<input value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} style={{ width: "12em" }} /></label>{" "}
        <label>部署{" "}
          <select value={dept} onChange={(e) => { setDept(e.target.value); setOffset(0); }}>
            <option value="">すべて</option>
            {(data?.departments ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {data && data.total === 0 && <p className="muted">当てはまる人はいません。</p>}
      {data && data.total > 0 && (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>社員番号</th><th>氏名</th><th>部署</th><th>在籍状態</th><th>最後に変わった日</th></tr></thead>
              <tbody>
                {data.employees.map((e) => (
                  <tr key={e.employee_no}><td>{e.employee_no}</td><td>{e.name}</td><td>{e.dept}</td><td>{e.status}</td><td className="small">{e.changed_at || "—"}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="small">
            {offset + 1}〜{offset + data.employees.length}人目（全部で {data.total}人）{" "}
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>前へ</button>{" "}
            <button disabled={offset + data.employees.length >= data.total} onClick={() => setOffset(offset + 100)}>次へ</button>
          </p>
        </>
      )}
    </section>
  );
}
