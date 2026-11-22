// S-52 設定（管理者だけ。K-042）。本人確認の方針・監査ログ・モデル・ファイルの4つ
// - 保存の仕組みは仕分けルールと同じ（yanua/settingfiles.py）。既定のファイルは書き換えず、画面で保存したものは写しにする
// - どの区切りも「今使っているもの」「版の一覧と、この版に戻す」「既定に戻す」を、ルールの画面と同じ形で出す
// - 保存の前にサーバーが検証し、誤りは欄の下に出す（誤りがあれば保存しない）。どれも再起動なしで効く
// - 起動の指定（--audit-body など）が勝つ項目は、画面では変えられない
// - 秘密の値（トークン・パスワード・端末の値）は、この画面では扱わない
import { ReactNode, useCallback, useEffect, useState } from "react";
import { api, ApiError, SettingDetail, SettingKey, SettingsOverview } from "../api";
import { navigate } from "../router";
import { errorText, time } from "../ui";

type Values = Record<string, unknown>;
type Problems = Record<string, string[]>;

const KEYS: SettingKey[] = ["identity", "audit", "models", "files", "hr", "access"];
const CLUE_ORDER = ["employee_no", "name", "dept"];
// 確定する組み合わせの選び肢（違う手がかりを2つ以上）
const COMBOS: string[][] = [["name", "employee_no"], ["name", "dept"], ["employee_no", "dept"], ["employee_no", "name", "dept"]];
const sameCombo = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

export default function SettingsPage({ canEdit = false }: { canEdit?: boolean }) {
  const [overview, setOverview] = useState<SettingsOverview | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    api<SettingsOverview>("GET", "/api/admin/settings")
      .then((r) => { setOverview(r); setError(""); })
      .catch((e) => setError(errorText(e, "設定を読み込めませんでした")));
  }, [tick]);

  const labels = Object.fromEntries((overview?.sections ?? []).map((s) => [s.key, s.label]));
  return (
    <>
      <h1 className="page-title">設定</h1>
      <div className="jobs-page">
        {error && <p className="error" role="alert">{error}</p>}
        {overview && overview.pending_restart.length > 0 && (
          <p className="warn notice-box" role="status" aria-label="再起動待ち">
            ⚠ 再起動すると使われる設定があります：{overview.pending_restart.map((k) => labels[k] ?? k).join("・")}。
            保存したもの（写しを直接直したものなど）と、今動いている値が違います。サーバーを起動し直すと使われます。
          </p>
        )}
        <section className="card">
          <p className="small muted">
            画面で保存した設定は <code>data/settings/</code> に写しとして書きます。リポジトリの <code>config/</code> のファイルは既定で、書き換えません。
            保存・版に戻す・既定に戻すは監査ログに残ります。ここで扱う設定は、どれも再起動しなくても、保存したときから使われます。
          </p>
          {overview?.links.map((l) => (
            <p key={l.path} className="small">
              {l.note}：<a href={l.path} onClick={(e) => { e.preventDefault(); navigate(l.path); }}>{l.label}へ</a>
            </p>
          ))}
        </section>
        {KEYS.map((k) => <Section key={k} settingKey={k} canEdit={canEdit} tick={tick} onChanged={reload} />)}
      </div>
    </>
  );
}

// 1つの設定の区切り：今使っているもの・編集・保存・版・既定に戻す
function Section({ settingKey, canEdit, tick, onChanged }: { settingKey: SettingKey; canEdit: boolean; tick: number; onChanged: () => void }) {
  const [data, setData] = useState<SettingDetail | null>(null);
  const [error, setError] = useState("");
  const [values, setValues] = useState<Values | null>(null);
  const [problems, setProblems] = useState<Problems>({});
  const [general, setGeneral] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState<number | "reset" | null>(null);

  useEffect(() => {
    api<SettingDetail>("GET", `/api/admin/settings/${settingKey}`)
      .then((r) => { setData(r); setValues(r.values ? { ...r.values } : null); setError(""); })
      .catch((e) => setError(errorText(e, "設定を読み込めませんでした")));
  }, [settingKey, tick]);

  const call = async <T,>(method: string, path: string, body: unknown, ok: (r: T) => string) => {
    setBusy(true);
    setProblems({});
    setGeneral("");
    setMessage("");
    try {
      const r = await api<T>(method, path, body);
      setMessage(ok(r));
      return r;
    } catch (e) {
      if (e instanceof ApiError && e.details.length) {
        const p: Problems = {};
        for (const d of e.details) (p[d.where] ??= []).push(d.message);
        setProblems(p);
        setGeneral(`${e.message}（${e.details.length}件）`);
      } else {
        setGeneral(errorText(e, "処理できませんでした"));
      }
      return null;
    } finally {
      setBusy(false);
      setAsking(null);
    }
  };

  const done = (r: { version: number | null; applied?: boolean; already_default?: boolean; message?: string }, what: string) => {
    if (r.already_default) return r.message ?? "すでに既定です";
    onChanged();
    return `${what}（版 ${r.version}）。${r.applied === false ? "再起動すると使われます。" : "再起動しなくても、もう使われています。"}`;
  };

  if (error) return <section className="card"><p className="error" role="alert">{error}</p></section>;
  if (!data) return <section className="card"><p className="muted">読み込んでいます…</p></section>;

  const set = (k: string, v: unknown) => { setValues((x) => ({ ...(x ?? {}), [k]: v })); setMessage(""); };
  const Err = ({ at }: { at: string }) => (problems[at]?.length
    ? <>{problems[at].map((m, i) => <div key={i} className="small error" role="alert">{m}</div>)}</> : null);
  const Pinned = ({ at }: { at: string }) => (data.pinned[at]
    ? <div className="small warn" role="note">起動の指定（{data.pinned[at]}）が勝っているので、画面では変えられません。今は「{String(data.running[at])}」で動いています。</div>
    : null);
  const editable = canEdit && values !== null;

  return (
    <section className="card" aria-label={data.label}>
      <h2>{data.label}</h2>
      <div className="toolbar">
        <span>今使っているもの：<strong>{data.status.using === "saved" ? "画面で保存したもの" : "既定"}</strong></span>
        {!data.applied && <span className="small warn">再起動すると使われます</span>}
      </div>
      {data.problems.length > 0 && (
        <div className="error" role="alert">
          保存した設定が読み込めないので、既定で動いています。直すところ：
          <ul className="small">{data.problems.map((p, i) => <li key={i}>{p.where && `${p.where}：`}{p.message}</li>)}</ul>
          前の版に戻すか、既定に戻してください。
        </div>
      )}
      {values && (
        <div className="form">
          {settingKey === "identity" && <IdentityEditor data={data} values={values} set={set} setValues={setValues} Err={Err} disabled={!editable} />}
          {settingKey === "audit" && <AuditEditor data={data} values={values} set={set} Err={Err} Pinned={Pinned} disabled={!editable} />}
          {settingKey === "models" && <ModelsEditor data={data} values={values} set={set} Err={Err} Pinned={Pinned} disabled={!editable} />}
          {settingKey === "files" && <FilesEditor data={data} values={values} set={set} Err={Err} disabled={!editable} />}
          {settingKey === "hr" && <HrEditor data={data} values={values} set={set} Err={Err} disabled={!editable} />}
          {settingKey === "access" && <AccessEditor data={data} values={values} set={set} Err={Err} disabled={!editable} />}
          {problems[""] && <div className="small error" role="alert">{problems[""].join("　")}</div>}
          {canEdit && (
            <div className="toolbar">
              <button type="button" disabled={busy} onClick={() => void call("POST", `/api/admin/settings/${settingKey}/validate`, { values }, () => "問題はありません。保存できます。")}>
                確かめる
              </button>
              <button type="button" className="primary" disabled={busy}
                onClick={() => void call<{ version: number; applied: boolean }>("PUT", `/api/admin/settings/${settingKey}`, { values }, (r) => done(r, "保存しました"))}>
                {busy ? "確かめています…" : "保存する"}
              </button>
            </div>
          )}
        </div>
      )}
      {general && <p className="error" role="alert">{general}</p>}
      {message && <p role="status">{message}</p>}

      <h3 className="small">版</h3>
      {canEdit && data.status.using === "saved" && (asking === "reset" ? (
        <span className="confirm">
          <span className="small">画面で保存したものをやめて、既定を使います。</span>
          <button className="primary" disabled={busy}
            onClick={() => void call<{ version: number | null; applied: boolean; already_default: boolean; message?: string }>("POST", `/api/admin/settings/${settingKey}/reset`, {}, (r) => done(r, "既定に戻しました"))}>
            はい、既定に戻す
          </button>
          <button disabled={busy} onClick={() => setAsking(null)}>やめる</button>
        </span>
      ) : (
        <button onClick={() => setAsking("reset")}>既定に戻す</button>
      ))}
      {data.versions.length === 0 ? (
        <p className="muted small">まだ画面から保存したことはありません。</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>版</th><th>保存した時刻</th><th>保存した人</th><th>したこと</th><th></th></tr></thead>
            <tbody>
              {data.versions.map((v) => (
                <tr key={v.version}>
                  <td className="nowrap">{v.version}{v.version === data.current && <span className="small muted">（今の版）</span>}</td>
                  <td className="small">{time(v.saved_at)}</td>
                  <td>{v.saved_by_name}</td>
                  <td>{v.action}</td>
                  <td className="nowrap">
                    {canEdit && v.version !== data.current && (asking === v.version ? (
                      <span className="confirm">
                        <span className="small">今の設定を、版 {v.version} の中身にします。</span>
                        <button className="primary" disabled={busy}
                          onClick={() => void call<{ version: number; applied: boolean }>("POST", `/api/admin/settings/${settingKey}/history/${v.version}/restore`, {}, (r) => done(r, `版 ${v.version} に戻しました`))}>
                          はい、戻す
                        </button>
                        <button disabled={busy} onClick={() => setAsking(null)}>やめる</button>
                      </span>
                    ) : (
                      <button className="link" onClick={() => setAsking(v.version)} aria-label={`${data.label}を版 ${v.version} に戻す`}>この版に戻す</button>
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

type EditorProps = {
  data: SettingDetail; values: Values; set: (k: string, v: unknown) => void; Err: (p: { at: string }) => ReactNode | null; disabled: boolean;
};

function IdentityEditor({ data, values, set, setValues, Err, disabled }: EditorProps & { setValues: (f: (v: Values | null) => Values | null) => void }) {
  const presets = data.meta.presets ?? {};
  const labels = data.meta.clues ?? {};
  const clues = (values.clues as string[]) ?? [];
  const confirm = (values.confirm as string[][]) ?? [];
  // 型を選ぶと、その型の中身（聞き方・手がかり・組み合わせ）を入れる。あとから項目ごとに直せる
  const choose = (name: string) => {
    const p = presets[name];
    if (p) setValues((v) => ({ ...(v ?? {}), preset: name, question: p.question, clues: [...p.clues], confirm: p.confirm.map((c) => [...c]) }));
  };
  const toggleClue = (c: string) => set("clues", clues.includes(c) ? clues.filter((x) => x !== c) : CLUE_ORDER.filter((x) => x === c || clues.includes(x)));
  const toggleCombo = (combo: string[]) =>
    set("confirm", confirm.some((x) => sameCombo(x, combo)) ? confirm.filter((x) => !sameCombo(x, combo)) : [...confirm, combo]);

  return (
    <>
      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="i-preset">型</label>
          <select id="i-preset" value={String(values.preset)} onChange={(e) => choose(e.target.value)} disabled={disabled}>
            {Object.keys(presets).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <div className="small muted">{presets[String(values.preset)]?.description}</div>
          <Err at="preset" />
        </div>
        <div className="form-row">
          <label htmlFor="i-follow">聞き返しの上限</label>
          <select id="i-follow" value={String(values.max_follow_ups)} onChange={(e) => set("max_follow_ups", Number(e.target.value))} disabled={disabled}>
            {[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n}回</option>)}
          </select>
          <Err at="max_follow_ups" />
        </div>
      </div>
      <div className="form-row">
        <label htmlFor="i-question">最初の聞き方</label>
        <textarea id="i-question" rows={2} maxLength={300} value={String(values.question ?? "")} onChange={(e) => set("question", e.target.value)} disabled={disabled} />
        <Err at="question" />
      </div>
      <fieldset className="form-row">
        <legend>使う手がかり</legend>
        {CLUE_ORDER.map((c) => (
          <label key={c} className="check">
            <input type="checkbox" checked={clues.includes(c)} onChange={() => toggleClue(c)} disabled={disabled} /> {labels[c] ?? c}
          </label>
        ))}
        <Err at="clues" />
      </fieldset>
      <fieldset className="form-row">
        <legend>何がそろったら本人と確定するか（どれか1つ）</legend>
        {COMBOS.map((combo) => (
          <label key={combo.join("+")} className="check">
            <input type="checkbox" checked={confirm.some((x) => sameCombo(x, combo))} onChange={() => toggleCombo(combo)} disabled={disabled} />
            {" "}{combo.map((c) => labels[c] ?? c).join("＋")}
          </label>
        ))}
        <Err at="confirm" />
      </fieldset>
      <label className="check">
        <input type="checkbox" checked={values.surname_counts === true} onChange={(e) => set("surname_counts", e.target.checked)} disabled={disabled} />
        姓だけの一致も、名前として数える（いいえなら、フルネームのときだけ数える）
      </label>
      <Err at="surname_counts" />
      <IdentityTry values={values} />
    </>
  );
}

// 試す欄：保存前の方針で、言い方を1行試す。保存しない。特定できたかと、聞き返す項目だけを出す
function IdentityTry({ values }: { values: Values }) {
  const [said, setSaid] = useState("");
  const [result, setResult] = useState<{ identified: boolean; matched: string[]; ask: string[]; question: string; note: string } | null>(null);
  const [error, setError] = useState("");
  const run = async () => {
    setError("");
    setResult(null);
    try {
      setResult(await api("POST", "/api/admin/settings/identity/try", { values, said }));
    } catch (e) {
      setError(errorText(e, "試せませんでした"));
    }
  };
  return (
    <div className="form-row spaced">
      <label htmlFor="i-try">試す（言い方を1行。保存はしません）</label>
      <div className="toolbar">
        <input id="i-try" type="text" maxLength={300} value={said} onChange={(e) => setSaid(e.target.value)} placeholder="例：営業部の高橋です" />
        <button type="button" disabled={!said.trim()} onClick={() => void run()}>試す</button>
      </div>
      {error && <p className="error small" role="alert">{error}</p>}
      {result && (
        <p role="status" className="small">
          {result.identified
            ? `特定できました（一致したもの：${result.matched.join("・")}）`
            : `特定できませんでした。${result.ask.length ? `聞き返すもの：${result.ask.join("・")}（「${result.question}」）` : "これ以上は聞き返しません。"}`}
          {!result.identified && result.note && <span className="muted">　{result.note}</span>}
        </p>
      )}
      <p className="small muted">人事データで照合しますが、ここには特定できたかどうかと、聞き返す項目だけを出します（ほかの社員の名前や番号は出しません）。</p>
    </div>
  );
}

function AuditEditor({ data, values, set, Err, Pinned, disabled }: EditorProps & { Pinned: (p: { at: string }) => ReactNode | null }) {
  const [asking, setAsking] = useState(false);
  const limit = data.meta.limits?.keep_days;
  return (
    <>
      <div className="form-row">
        <label htmlFor="a-days">保存期間</label>
        <span className="toolbar">
          <input id="a-days" type="number" min={limit?.min} max={limit?.max} step={1} value={String(values.keep_days ?? "")}
            onChange={(e) => set("keep_days", e.target.value === "" ? "" : Number(e.target.value))} disabled={disabled || !!data.pinned.keep_days} />
          <span>日（{limit?.min}〜{limit?.max}日）</span>
        </span>
        <Pinned at="keep_days" />
        <Err at="keep_days" />
      </div>
      <div className="form-row">
        <label className="check">
          <input type="checkbox" checked={values.keep_body === true} disabled={disabled || !!data.pinned.keep_body}
            onChange={(e) => (e.target.checked ? setAsking(true) : set("keep_body", false))} />
          質問の本文も残す
        </label>
        {asking && (
          <span className="confirm" role="alertdialog" aria-label="本文を残すかの確かめ">
            <span className="small">質問の本文が記録に残り、管理者が読めるようになります。</span>
            <button className="primary" onClick={() => { set("keep_body", true); setAsking(false); }}>はい、残す</button>
            <button onClick={() => setAsking(false)}>やめる</button>
          </span>
        )}
        <Pinned at="keep_body" />
        <Err at="keep_body" />
      </div>
    </>
  );
}

function ModelsEditor({ data, values, set, Err, Pinned, disabled }: EditorProps & { Pinned: (p: { at: string }) => ReactNode | null }) {
  const catalog = data.meta.catalog ?? [];
  const field = (k: "chat" | "summary", label: string) => {
    const v = String(values[k] ?? "");
    // 選び肢は、目録にあってこの用途のモデルだけ（埋め込み用などは出さない）。使えないものは理由をつけて選ばせない
    const usable = catalog.filter((m) => m.use.includes(k));
    const known = usable.some((m) => m.name === v);
    const blocked = usable.filter((m) => m.state !== "ok");
    return (
      <div className="form-row">
        <label htmlFor={`m-${k}`}>{label}</label>
        <select id={`m-${k}`} value={v} onChange={(e) => set(k, e.target.value)} disabled={disabled || !!data.pinned[k] || usable.length === 0}>
          {!known && <option value={v} disabled>{v}（Yanua の目録にありません）</option>}
          {usable.map((m) => (
            <option key={m.name} value={m.name} disabled={m.state !== "ok"}>
              {m.name}{m.state === "ok" ? "" : `（${m.state === "not_installed" ? "入っていません" : m.state === "not_registered" ? "登録されていません" : "登録と違います"}）`}
            </option>
          ))}
        </select>
        {blocked.length > 0 && (
          <ul className="small muted">
            {blocked.map((m) => <li key={m.name}>{m.name}：{m.message}</li>)}
          </ul>
        )}
        <Pinned at={k} />
        <Err at={k} />
      </div>
    );
  };
  return (
    <>
      {data.meta.error && <p className="error small" role="alert">{data.meta.error}</p>}
      <div className="form-grid">
        {field("chat", "チャット・ヘルプデスク・受付のモデル")}
        {field("summary", "議事録の要約のモデル")}
      </div>
      <p className="small muted">
        選べるのは、Yanua の目録（config/network.json）にあり、<code>python -m yanua models pull</code> で登録した digest と、手元の Ollama の digest が同じモデルだけです。
        Ollama に入っていても、目録にないモデルは出しません。
      </p>
    </>
  );
}

function FilesEditor({ data, values, set, Err, disabled }: EditorProps) {
  const lim = data.meta.limits ?? {};
  const num = (k: string, label: string, step: number) => (
    <div className="form-row">
      <label htmlFor={`f-${k}`}>{label}</label>
      <span className="toolbar">
        <input id={`f-${k}`} type="number" min={lim[k]?.min} max={lim[k]?.max} step={step} value={String(values[k] ?? "")}
          onChange={(e) => set(k, e.target.value === "" ? "" : Number(e.target.value))} disabled={disabled} />
        <span>{lim[k]?.unit}（{lim[k]?.min}〜{lim[k]?.max}{lim[k]?.unit}）</span>
      </span>
      <Err at={k} />
    </div>
  );
  return (
    <div className="form-grid">
      {num("work_max_gb", "作業用フォルダの合計の上限", 0.1)}
      {num("file_keep_hours", "アップロード・結果のファイルを消すまでの時間", 1)}
    </div>
  );
}

// 休職中の人の扱い（K-054）。退職した人は、いつもログインできない（ここでは変えない）
function HrEditor({ values, set, Err, disabled }: EditorProps) {
  return (
    <div className="form-grid">
      <div className="form-row">
        <span>休職中の人のログイン</span>
        <span className="toolbar">
          {([[true, "ログインできる"], [false, "ログインできない"]] as const).map(([v, text]) => (
            <label key={text}><input type="radio" style={{ width: "auto" }} name="on_leave_login" checked={values.on_leave_login === v}
              onChange={() => set("on_leave_login", v)} disabled={disabled} /> {text}</label>
          ))}
        </span>
        <Err at="on_leave_login" />
      </div>
      <div className="form-row">
        <label htmlFor="f-on_leave_permissions">休職中の人の権限</label>
        <select id="f-on_leave_permissions" value={String(values.on_leave_permissions ?? "")}
          onChange={(e) => set("on_leave_permissions", e.target.value)} disabled={disabled}>
          <option value="そのまま">そのまま</option>
          <option value="社員の役割だけ">社員の役割だけ（ほかのグループの役割は効かない）</option>
        </select>
        <Err at="on_leave_permissions" />
      </div>
      <p className="small muted">退職した人は、いつもログインできません。手で入れたグループのメンバーとしても数えません。</p>
    </div>
  );
}

// 権限の条件と役職の段（docs/access-model.md）。役職の段は人の属性で、権限は持たない（段で決まるグループを通して効く）
function AccessEditor({ values, set, Err, disabled }: EditorProps) {
  const levels = (values.title_levels ?? {}) as Record<string, string>;
  const rows = Object.entries(levels).sort((a, b) => Number(a[0]) - Number(b[0]));
  const setLevels = (next: Record<string, string>) => set("title_levels", next);
  const nextNo = String(Math.max(0, ...rows.map(([n]) => Number(n) || 0)) + 1);
  return (
    <div className="form-grid">
      <div className="form-row">
        <span>管理の権限は確かな入口が要る</span>
        <span className="toolbar">
          {([[false, "OFF（今のまま）"], [true, "ON"]] as const).map(([v, text]) => (
            <label key={text}><input type="radio" style={{ width: "auto" }} name="admin_needs_strong_entry"
              checked={values.admin_needs_strong_entry === v} onChange={() => set("admin_needs_strong_entry", v)} disabled={disabled} /> {text}</label>
          ))}
        </span>
        <Err at="admin_needs_strong_entry" />
        <p className="small muted">
          ON にすると、管理の権限（権限を変える・設定・通信・監査ログ・受付端末の登録・人事データの取り込み・文書の公開先の変更など）は、SSO か多要素つきの Yanua アカウントで入ったときだけ効きます。
          今は入口が社員番号だけなので、ON にするとだれも管理できなくなります。確かな入口で入っていない人は ON にできません（SSO ができてから ON にします）。
        </p>
      </div>
      <div className="form-row">
        <span>役職の段の名前</span>
        <table className="table" aria-label="役職の段の名前">
          <thead><tr><th>段</th><th>名前</th><th></th></tr></thead>
          <tbody>
            {rows.map(([n, name]) => (
              <tr key={n}>
                <td>{n}</td>
                <td><input aria-label={`段 ${n} の名前`} value={name} maxLength={20} disabled={disabled}
                  onChange={(e) => setLevels({ ...levels, [n]: e.target.value })} /></td>
                <td>{!disabled && rows.length > 1 && (
                  <button type="button" className="link" onClick={() => { const next = { ...levels }; delete next[n]; setLevels(next); }}>消す</button>
                )}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!disabled && <button type="button" onClick={() => setLevels({ ...levels, [nextNo]: "" })}>段 {nextNo} を足す</button>}
        <Err at="title_levels" />
        <p className="small muted">段は比べるための数です（大きいほど上）。名前は会社ごとに決めます。人ごとの段は人事データの取り込みで入ります。</p>
      </div>
      <div className="form-row">
        <label htmlFor="f-executive_level">「経営」のグループに入る段</label>
        <select id="f-executive_level" value={String(values.executive_level ?? "")} disabled={disabled}
          onChange={(e) => set("executive_level", Number(e.target.value))}>
          {rows.map(([n, name]) => <option key={n} value={n}>{n}（{name || "名前なし"}）以上</option>)}
        </select>
        <Err at="executive_level" />
        <p className="small muted">この段以上の人が「経営」のグループに入り、役割「経営」（全社の文書と集計を見る。管理の権限は入らない）を持ちます。</p>
      </div>
    </div>
  );
}
