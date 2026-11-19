// S-53 通信とモデル（管理者だけ。K-052）。Yanua が社外のどこへ・いつ通信するかを決めて確かめる
// - 宛先：既定（config/network.json）は消せない。ON/OFF だけ。追加の宛先は足す・消す・ON/OFF
// - 更新の確認：しない／手動だけ／毎日／毎週。見に行くのは ON の宛先だけ。新しい版があっても自動では入れ替えない
// - モデルの目録：固定した版・記録したハッシュ・状態。照合は通信しない。入れ替えはボタンを押したときだけ
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Job, ModelRow, NetHost, NetworkView, UpdateCheck } from "../api";
import { useJob } from "../useJob";
import { errorText, JobProgress, time } from "../ui";

type Problems = Record<string, string[]>;
const WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"];
const STATE: Record<ModelRow["state"], string> = {
  match: "一致", differs: "違う", not_installed: "入っていない", not_registered: "登録していない", unverified: "未照合", unknown: "不明",
};
const TONE: Record<string, string> = { match: "ok", differs: "ng", not_installed: "ng", not_registered: "off", unverified: "run", unknown: "off" };
const short = (h: string) => (h ? h.slice(0, 12) : "—");
// 目録の用途（use）を日本語で出す
const USES: Record<string, string> = { transcribe: "文字起こし", chat: "チャット", summary: "要約", embedding: "埋め込み" };
const useText = (use: string[]) => use.map((u) => USES[u] ?? u).join("・") || "—";

export default function NetworkPage({ canEdit = false }: { canEdit?: boolean }) {
  const [view, setView] = useState<NetworkView | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    api<NetworkView>("GET", "/api/admin/network")
      .then((v) => { setView(v); setError(""); })
      .catch((e) => setError(errorText(e, "通信の設定を読み込めませんでした")));
  }, [tick]);

  return (
    <>
      <h1 className="page-title">通信とモデル</h1>
      <div className="jobs-page">
        {error && <p className="error" role="alert">{error}</p>}
        {view?.error && (
          <p className="error" role="alert">
            通信の設定（config/network.json）が読めないので、社外には通信していません（モデルの入手・更新の確認も止めています）：{view.error}
          </p>
        )}
        {view && view.new_defaults.length > 0 && (
          <p className="warn notice-box" role="status" aria-label="新しい既定の宛先">
            ⚠ 新しい既定の宛先があります：{view.new_defaults.map((d) => `${d.host}${d.purpose ? `（${d.purpose}）` : ""}`).join("、")}。
            今は OFF です。使うときは、下の一覧で ON にして保存してください。
          </p>
        )}
        {view && view.removed_defaults.length > 0 && (
          <p className="warn notice-box" role="status" aria-label="既定から外れた宛先">
            既定から外れました：{view.removed_defaults.join("、")}。この宛先には通信しません。
          </p>
        )}
        {view && <Hosts view={view} canEdit={canEdit} onChanged={reload} />}
        {view && <Models view={view} canEdit={canEdit} tick={tick} onChanged={reload} />}
      </div>
    </>
  );
}

function useSend(onDone: () => void) {
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<Problems>({});
  const [general, setGeneral] = useState("");
  const [message, setMessage] = useState("");
  const send = async <T,>(method: string, path: string, body: unknown, ok: (r: T) => string) => {
    setBusy(true);
    setProblems({});
    setGeneral("");
    setMessage("");
    try {
      const r = await api<T>(method, path, body);
      setMessage(ok(r));
      onDone();
      return r;
    } catch (e) {
      if (e instanceof ApiError && e.details.length) {
        const p: Problems = {};
        for (const d of e.details) (p[d.where] ??= []).push(d.message);
        setProblems(p);
        setGeneral(`${e.message}（${e.details.length}件）`);
      } else setGeneral(errorText(e, "処理できませんでした"));
      return null;
    } finally {
      setBusy(false);
    }
  };
  return { busy, problems, general, message, send, setMessage };
}

// 許可する宛先と、更新の確認の予定（どちらも同じ「状態」として保存する）
function Hosts({ view, canEdit, onChanged }: { view: NetworkView; canEdit: boolean; onChanged: () => void }) {
  const [hosts, setHosts] = useState<NetHost[]>(view.hosts);
  const [uc, setUc] = useState<UpdateCheck>(view.update_check);
  const [newHost, setNewHost] = useState("");
  const [newPurpose, setNewPurpose] = useState("");
  const [addError, setAddError] = useState<string[]>([]);
  const [asking, setAsking] = useState<number | "reset" | null>(null);
  const { busy, problems, general, message, send } = useSend(onChanged);
  useEffect(() => { setHosts(view.hosts); setUc(view.update_check); }, [view]);

  const values = () => ({
    defaults: Object.fromEntries(hosts.filter((h) => h.kind === "default").map((h) => [h.host, h.on])),
    added: hosts.filter((h) => h.kind === "added").map((h) => ({ host: h.host, on: h.on, purpose: h.purpose })),
    update_check: uc,
  });
  // 足す前に、サーバーの検証（保存のときと同じ）にかける。誤りなら一覧に入れず、欄の下に理由を出す
  const addHost = async () => {
    const candidate: NetHost = { host: newHost.trim().toLowerCase(), kind: "added", purpose: newPurpose.trim(), new: false, on: true };
    const next = [...hosts, candidate];
    const index = next.filter((h) => h.kind === "added").length - 1;
    const body = {
      defaults: Object.fromEntries(next.filter((h) => h.kind === "default").map((h) => [h.host, h.on])),
      added: next.filter((h) => h.kind === "added").map((h) => ({ host: h.host, on: h.on, purpose: h.purpose })),
      update_check: uc,
    };
    setAddError([]);
    try {
      await api("POST", "/api/admin/network/validate", { values: body });
    } catch (e) {
      const mine = e instanceof ApiError ? e.details.filter((d) => d.where === `added[${index}]`).map((d) => d.message) : [];
      if (mine.length || !(e instanceof ApiError)) {
        setAddError(mine.length ? mine : [errorText(e, "確かめられませんでした")]);
        return;
      }
    }
    setHosts(next);
    setNewHost("");
    setNewPurpose("");
  };
  const toggle = (host: string) => setHosts((hs) => hs.map((h) => (h.host === host ? { ...h, on: !h.on } : h)));
  const added = hosts.filter((h) => h.kind === "added");
  const Err = ({ at }: { at: string }) => (problems[at]?.length ? <>{problems[at].map((m, i) => <div key={i} className="small error" role="alert">{m}</div>)}</> : null);
  const done = (what: string) => (r: { version: number | null; already_default?: boolean; message?: string }) =>
    r.already_default ? (r.message ?? "すでに既定です") : `${what}（版 ${r.version}）。再起動しなくても、もう使われています。`;

  return (
    <>
      <section className="card" aria-label="許可する宛先">
        <h2>許可する宛先</h2>
        <p className="small muted">
          Yanua が社外に通信してよい宛先です。ON の宛先にだけ、つなぎます（つなぐ前に確かめます）。既定の宛先（config/network.json）は消せません。使わないときは OFF にします。
        </p>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>宛先</th><th>種類</th><th>用途</th><th>ON</th><th></th></tr></thead>
            <tbody>
              {hosts.map((h) => {
                const i = added.indexOf(h);
                return (
                  <tr key={h.host}>
                    <td className="mono">{h.host}{h.new && <span className="small warn">（新しい既定）</span>}</td>
                    <td className="small">{h.kind === "default" ? "既定" : "追加"}</td>
                    <td className="small">{h.purpose || "—"}</td>
                    <td><input type="checkbox" checked={h.on} onChange={() => toggle(h.host)} disabled={!canEdit} aria-label={`${h.host} を使う`} /></td>
                    <td>
                      {h.kind === "added" && canEdit && (
                        <button className="link" onClick={() => setHosts((hs) => hs.filter((x) => x.host !== h.host))} aria-label={`${h.host} を消す`}>消す</button>
                      )}
                      {i >= 0 && <Err at={`added[${i}]`} />}
                      <Err at={`defaults.${h.host}`} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {canEdit && (
          <div className="toolbar spaced">
            <input type="text" value={newHost} onChange={(e) => setNewHost(e.target.value)} placeholder="例：registry.ollama.ai" aria-label="追加する宛先（ホスト名）" />
            <input type="text" value={newPurpose} maxLength={100} onChange={(e) => setNewPurpose(e.target.value)} placeholder="用途" aria-label="追加する宛先の用途" />
            <button type="button" disabled={!newHost.trim()} onClick={() => void addHost()}>宛先を足す</button>
            <span className="small muted">https のホスト名だけ（パス・ワイルドカード・IP アドレスは使えません）。足したら「保存する」を押してください。</span>
          </div>
        )}
        {addError.map((m, i) => <div key={i} className="small error" role="alert">{m}</div>)}
      </section>

      <section className="card" aria-label="更新の確認">
        <h2>更新の確認</h2>
        <p className="small muted">新しい版があるかを見に行く予定です。見に行くのは ON の宛先だけです。新しい版があっても自動では入れ替えず、下の「モデルの目録」に出すだけです。</p>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="n-mode">いつ見に行くか</label>
            <select id="n-mode" value={uc.mode} disabled={!canEdit} onChange={(e) => setUc({ ...uc, mode: e.target.value as UpdateCheck["mode"] })}>
              {Object.entries(view.modes).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
            <Err at="update_check" />
          </div>
          {uc.mode === "weekly" && (
            <div className="form-row">
              <label htmlFor="n-weekday">曜日</label>
              <select id="n-weekday" value={uc.weekday} disabled={!canEdit} onChange={(e) => setUc({ ...uc, weekday: Number(e.target.value) })}>
                {WEEKDAYS.map((w, i) => <option key={w} value={i}>{w}曜</option>)}
              </select>
              <Err at="update_check.weekday" />
            </div>
          )}
          {(uc.mode === "daily" || uc.mode === "weekly") && (
            <div className="form-row">
              <label htmlFor="n-time">時刻</label>
              <input id="n-time" type="time" value={uc.time} disabled={!canEdit} onChange={(e) => setUc({ ...uc, time: e.target.value })} />
              <Err at="update_check.time" />
            </div>
          )}
        </div>
        {canEdit && (
          <div className="toolbar">
            <button type="button" disabled={busy} onClick={() => void send("POST", "/api/admin/network/validate", { values: values() }, () => "問題はありません。保存できます。")}>確かめる</button>
            <button type="button" className="primary" disabled={busy} onClick={() => void send("PUT", "/api/admin/network", { values: values() }, done("保存しました"))}>
              {busy ? "確かめています…" : "保存する（宛先と更新の確認）"}
            </button>
          </div>
        )}
        {general && <p className="error" role="alert">{general}</p>}
        {message && <p role="status">{message}</p>}

        <h3 className="small">版</h3>
        {canEdit && view.status.using === "saved" && (asking === "reset" ? (
          <span className="confirm">
            <span className="small">既定の宛先をすべて ON にし、追加の宛先を消します。更新の確認は「手動だけ」に戻り、入れ替えたモデルの版も既定の版に戻ります。</span>
            <button className="primary" disabled={busy} onClick={() => { setAsking(null); void send("POST", "/api/admin/network/reset", {}, done("既定に戻しました")); }}>はい、既定に戻す</button>
            <button disabled={busy} onClick={() => setAsking(null)}>やめる</button>
          </span>
        ) : <button onClick={() => setAsking("reset")}>既定に戻す</button>)}
        {view.versions.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>版</th><th>保存した時刻</th><th>保存した人</th><th>したこと</th><th></th></tr></thead>
              <tbody>
                {view.versions.map((v) => (
                  <tr key={v.version}>
                    <td className="nowrap">{v.version}{v.version === view.current && <span className="small muted">（今の版）</span>}</td>
                    <td className="small">{time(v.saved_at)}</td>
                    <td>{v.saved_by_name}</td>
                    <td>{v.action}</td>
                    <td className="nowrap">
                      {canEdit && v.version !== view.current && (asking === v.version ? (
                        <span className="confirm">
                          <span className="small">今の設定を、版 {v.version} の中身にします。</span>
                          <button className="primary" disabled={busy} onClick={() => { setAsking(null); void send("POST", `/api/admin/network/history/${v.version}/restore`, {}, done(`版 ${v.version} に戻しました`)); }}>はい、戻す</button>
                          <button disabled={busy} onClick={() => setAsking(null)}>やめる</button>
                        </span>
                      ) : <button className="link" onClick={() => setAsking(v.version)} aria-label={`通信の設定を版 ${v.version} に戻す`}>この版に戻す</button>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

function Models({ view, canEdit, tick, onChanged }: { view: NetworkView; canEdit: boolean; tick: number; onChanged: () => void }) {
  const [rows, setRows] = useState<ModelRow[] | null>(null);
  const [error, setError] = useState("");
  const [asking, setAsking] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const { busy, general, message, send, setMessage } = useSend(onChanged);
  const { job } = useJob(jobId, (j) => {
    setMessage(j.status === "完了" ? "入れ替えました（入手元のハッシュと照合済み）。" : "");
    onChanged();
  });

  useEffect(() => {
    api<{ models: ModelRow[] }>("GET", "/api/admin/models")
      .then((r) => { setRows(r.models); setError(""); })
      .catch((e) => setError(errorText(e, "モデルの目録を読み込めませんでした")));
  }, [tick]);

  const replace = async (name: string) => {
    setAsking(null);
    try {
      const j = await api<Job>("POST", `/api/admin/models/${encodeURIComponent(name)}/replace`, {});
      setJobId(j.job_id);
    } catch (e) {
      setError(errorText(e, "入れ替えを始められませんでした"));
    }
  };

  return (
    <section className="card" aria-label="モデルの目録">
      <h2>モデルの目録</h2>
      {view.last_check && <p className="small muted">前に更新を確かめた時刻：{time(view.last_check.checked_at)}（{view.last_check.by}）</p>}
      {canEdit && (
        <div className="toolbar">
          <button type="button" disabled={busy || view.update_check.mode === "none"}
            onClick={() => void send("POST", "/api/admin/network/check", {}, () => "更新を確かめました。結果は下の一覧にあります。")}>
            今すぐ更新を確かめる
          </button>
          <button type="button" disabled={busy} onClick={() => void send("POST", "/api/admin/models/verify", {}, () => "手元のファイルと記録を照らしました（通信していません）。")}>
            照合する
          </button>
          {view.update_check.mode === "none" && <span className="small muted">更新の確認は「しない」になっています。</span>}
        </div>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {general && <p className="error" role="alert">{general}</p>}
      {message && <p role="status">{message}</p>}
      {jobId && <JobProgress job={job} label="モデルの入れ替え" />}
      {rows && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>名前</th><th>種類</th><th>用途</th><th>固定した版</th><th>記録したハッシュ／digest</th><th>状態</th><th>更新</th></tr></thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.name}>
                  <td className="mono">{m.name}</td>
                  <td className="small">{m.kind}</td>
                  <td className="small">{useText(m.use)}</td>
                  <td className="mono small">{m.kind === "ollama" ? "—" : short(m.revision)}</td>
                  <td className="mono small">{short(m.recorded)}{m.files ? <span className="muted">（{m.files}件）</span> : null}</td>
                  <td>
                    <span className={`status ${TONE[m.state] ?? "run"}`}>{STATE[m.state] ?? m.state}</span>
                    {m.message && <div className="small muted">{m.message}</div>}
                  </td>
                  <td className="small">
                    {m.check_message || "—"}
                    {canEdit && m.check === "new" && m.kind === "whisper" && (asking === m.name ? (
                      <div className="confirm">
                        <span className="small">版 {short(m.latest)} を入手し、入手元のハッシュと照らしてから入れ替えます。失敗したら今のモデルのままです。</span>
                        <button className="primary" onClick={() => void replace(m.name)}>はい、入れ替える</button>
                        <button onClick={() => setAsking(null)}>やめる</button>
                      </div>
                    ) : (
                      <div><button onClick={() => setAsking(m.name)} aria-label={`${m.name} を入れ替える`}>入れ替える</button></div>
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
