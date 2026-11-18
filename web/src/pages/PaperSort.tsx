// S-20 PDF の仕分け（総務）。受信箱の PDF → 仕分けの予定（ジョブ）→ ルールを選び直す → 実行 → 記録
// - 予定は作った人だけが見られる（API の側で守る）。仕分けの記録は総務の全員が見られる
// - ルールを選び直すときは、サーバーが予定を作ったときの文字を使う（OCR をし直さないので速い）
// - 未分類の PDF（または選んだ PDF）から、ルールの下書きを作って保存できる（PaperRules.tsx。K-047）
import { useCallback, useEffect, useState } from "react";
import { api, InboxFile, Job, Plan, PlanItem, PaperRule, SortRecord } from "../api";
import { useJob } from "../useJob";
import { DraftSource, RuleDraftPanel, RuleHistory } from "./PaperRules";
import { bytes, errorText, JobProgress, queryParam, setQuery, Status, time } from "../ui";

const BY: Record<string, string> = { rule: "ルール", forced: "選び直し", llm: "LLM", none: "当たらない" };
const FIELD_LABELS: Record<string, string> = { date: "日付", company: "相手先", amount: "金額", to: "宛先", yyyy: "年", mm: "月", dd: "日" };
const label = (k: string) => FIELD_LABELS[k] ?? k;

export default function PaperSort({ canRules = false }: { canRules?: boolean }) {
  const [planId, setPlanId] = useState<string | null>(() => queryParam("plan"));
  const [jobId, setJobId] = useState<string | null>(null);
  const [historyTick, setHistoryTick] = useState(0);
  const [draft, setDraft] = useState<DraftSource | null>(null);
  const [rulesTick, setRulesTick] = useState(0);     // ルールを保存した・戻したときに、ルールの一覧を読み直す
  const [saved, setSaved] = useState<number | null>(null);

  const open = (id: string | null) => {
    setPlanId(id);
    setQuery("plan", id);
  };
  const { job, error: jobError } = useJob(jobId, (j: Job) => {
    if (j.status === "完了" && typeof j.result?.plan_id === "string") {
      open(j.result.plan_id);
      setJobId(null);
    }
  });

  return (
    <>
      <h1 className="page-title">PDF の仕分け</h1>
      <div className="jobs-page">
        {saved !== null && (
          <p className="notice" role="status">
            ルールを保存しました（版 {saved}）。予定を作り直すと、新しいルールが使われます。
            <button className="link" onClick={() => setSaved(null)}>閉じる</button>
          </p>
        )}
        {draft && (
          <RuleDraftPanel key={JSON.stringify(draft)} source={draft} onClose={() => setDraft(null)}
            onSaved={(v) => { setSaved(v); setDraft(null); setRulesTick((t) => t + 1); }} />
        )}
        {!planId && <Inbox busy={!!jobId} onStarted={setJobId} onDraft={canRules ? setDraft : undefined} />}
        {jobId && (
          <section className="card" aria-label="予定作りの進み具合">
            {jobError ? <p className="error" role="alert">{jobError}</p> : <JobProgress job={job} label="仕分けの予定作り" />}
            {job && !["待ち", "実行中", "完了"].includes(job.status) && <button onClick={() => setJobId(null)}>受信箱に戻る</button>}
          </section>
        )}
        {planId && (
          <PlanView planId={planId} onBack={() => open(null)} onExecuted={() => setHistoryTick((t) => t + 1)} rulesTick={rulesTick}
            onDraft={canRules ? setDraft : undefined} replan={saved !== null} onReplan={(id) => { setSaved(null); setJobId(id); }} />
        )}
        <History tick={historyTick} />
        {canRules && <RuleHistory tick={rulesTick} onRestored={() => setRulesTick((t) => t + 1)} />}
      </div>
    </>
  );
}

function Inbox({ busy, onStarted, onDraft }: { busy: boolean; onStarted: (jobId: string) => void; onDraft?: (s: DraftSource) => void }) {
  const [files, setFiles] = useState<InboxFile[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [useLlm, setUseLlm] = useState(false);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api<{ files: InboxFile[] }>("GET", "/api/paper/inbox");
      setFiles(r.files);
      setChosen(new Set(r.files.map((f) => f.name)));
      setError("");
    } catch (e) {
      setError(errorText(e, "受信箱を読み込めませんでした"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = (name: string) =>
    setChosen((s) => {
      const n = new Set(s);
      if (n.has(name)) n.delete(name);
      else n.add(name);
      return n;
    });

  const start = async () => {
    if (!files) return;
    setSending(true);
    setError("");
    try {
      // すべてを選んでいるときは、空で送る（受信箱のすべて。予定を作る間に届いたものも入る）
      const all = chosen.size === files.length;
      const job = await api<Job>("POST", "/api/paper/plans", { files: all ? [] : files.filter((f) => chosen.has(f.name)).map((f) => f.name), use_llm: useLlm });
      onStarted(job.job_id);
    } catch (e) {
      setError(errorText(e, "予定を作れませんでした"));
    } finally {
      setSending(false);
    }
  };

  return (
    <section className="card">
      <div className="row-between">
        <h2>受信箱</h2>
        <button className="link" onClick={() => void load()}>読み込み直す</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!error && files === null && <p className="muted">読み込んでいます…</p>}
      {files?.length === 0 && <p className="muted">受信箱に PDF はありません。</p>}
      {files && files.length > 0 && (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>選ぶ</th><th>ファイル</th><th>大きさ</th><th>届いた時刻</th></tr></thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.name}>
                    <td><input type="checkbox" checked={chosen.has(f.name)} onChange={() => toggle(f.name)} aria-label={`${f.name} を選ぶ`} /></td>
                    <td>{f.name}</td>
                    <td className="small nowrap">{bytes(f.size)}</td>
                    <td className="small">{time(f.modified)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="toolbar spaced">
            <label className="check">
              <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
              どのルールにも当たらないものは、社内の LLM に選ばせる（時間がかかります）
            </label>
          </div>
          <p className="small muted">まだファイルは動かしません。予定を見て、確かめてから実行します。スキャンした PDF は文字を読み取る（OCR）ので時間がかかります。</p>
          <button className="primary" disabled={busy || sending || chosen.size === 0} onClick={() => void start()}>
            {chosen.size}件の仕分けの予定を作る
          </button>
          {onDraft && chosen.size === 1 && (
            <button className="spaced-left" onClick={() => { const f = [...chosen][0]; onDraft({ inbox: f, file: f }); }}>
              選んだ1件からルールの下書きを作る
            </button>
          )}
        </>
      )}
    </section>
  );
}

function PlanView({ planId, onBack, onExecuted, rulesTick, onDraft, replan, onReplan }: {
  planId: string; onBack: () => void; onExecuted: () => void; rulesTick: number;
  onDraft?: (s: DraftSource) => void; replan: boolean; onReplan: (jobId: string) => void;
}) {
  const [replanning, setReplanning] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [rules, setRules] = useState<PaperRule[]>([]);
  const [error, setError] = useState("");
  const [rowError, setRowError] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState<number | null>(null);
  const [move, setMove] = useState(false);
  const [asking, setAsking] = useState(false);
  const [executing, setExecuting] = useState(false);

  useEffect(() => {
    setPlan(null);
    Promise.all([api<Plan>("GET", `/api/paper/plans/${encodeURIComponent(planId)}`), api<{ rules: PaperRule[] }>("GET", "/api/paper/rules")])
      .then(([p, r]) => {
        setPlan(p);
        setRules(r.rules);
        setError("");
      })
      .catch((e) => setError(errorText(e, "仕分けの予定を読み込めませんでした")));
  }, [planId]);

  // ルールを保存した・戻したときは、選べるルールを読み直す
  useEffect(() => {
    if (rulesTick === 0) return;
    api<{ rules: PaperRule[] }>("GET", "/api/paper/rules").then((r) => setRules(r.rules)).catch(() => {});
  }, [rulesTick]);

  // 同じファイルで予定を作り直す（保存した新しいルールを使う）
  const remake = async () => {
    if (!plan) return;
    setReplanning(true);
    setError("");
    try {
      const j = await api<Job>("POST", "/api/paper/plans", { files: plan.items.map((x) => x.file), use_llm: false });
      onReplan(j.job_id);
    } catch (e) {
      setError(errorText(e, "予定を作り直せませんでした"));
    } finally {
      setReplanning(false);
    }
  };

  const rechoose = async (item: PlanItem, rule: string) => {
    setSaving(item.index);
    setRowError((m) => ({ ...m, [item.index]: "" }));
    try {
      const r = await api<PlanItem>("PUT", `/api/paper/plans/${encodeURIComponent(planId)}/items/${item.index}`, { rule: rule || null });
      setPlan((p) => p && { ...p, items: p.items.map((x) => (x.index === r.index ? r : x)) });
    } catch (e) {
      setRowError((m) => ({ ...m, [item.index]: errorText(e, "選び直せませんでした") }));
    } finally {
      setSaving(null);
    }
  };

  const execute = async () => {
    setExecuting(true);
    setError("");
    try {
      const result = await api<Plan["result"]>("POST", `/api/paper/plans/${encodeURIComponent(planId)}/execute`, { move });
      setPlan((p) => p && { ...p, executed_at: new Date().toISOString(), result });
      onExecuted();
    } catch (e) {
      setError(errorText(e, "実行できませんでした"));
    } finally {
      setExecuting(false);
      setAsking(false);
    }
  };

  const placeable = plan?.items.filter((x) => !x.error && x.dest).length ?? 0;
  // 同じ置き先の行（実行すると、2つ目からは番号を付けて分ける）
  const destCount = new Map<string, number>();
  for (const x of plan?.items ?? []) if (!x.error && x.dest) destCount.set(x.dest, (destCount.get(x.dest) ?? 0) + 1);
  const warned = plan?.items.filter((x) => !x.error && ((x.missing?.length ?? 0) > 0 || (destCount.get(x.dest) ?? 0) > 1)).length ?? 0;
  const done = !!plan?.executed_at;

  return (
    <section className="card" aria-label="仕分けの予定">
      <div className="row-between">
        <h2>仕分けの予定{plan && <span className="small muted">（{time(plan.created_at)} に作成）</span>}</h2>
        <button className="link" onClick={onBack}>受信箱に戻る</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!plan && !error && <p className="muted">読み込んでいます…</p>}
      {plan && (
        <>
          {!done && <p className="small muted">置く先を確かめてください。ルールが違うものは、選び直せます（PDF は読み直しません）。</p>}
          {!done && warned > 0 && <p className="warn" role="status">⚠ 注意がある行が{warned}件あります（表の「置く先」を見てください）。</p>}
          {replan && !done && (
            <p><button className="primary" disabled={replanning} onClick={() => void remake()}>この予定のファイルで、予定を作り直す</button></p>
          )}
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>ファイル</th><th>ルール</th><th>読み取った内容</th><th>置く先</th></tr></thead>
              <tbody>
                {plan.items.map((x) => (
                  <tr key={x.index}>
                    <td>
                      {x.file}
                      {x.ocr && <div className="small muted">OCR で読み取り</div>}
                    </td>
                    <td className="nowrap">
                      {x.error ? (
                        <Status s="失敗" />
                      ) : done ? (
                        x.rule || "未分類"
                      ) : (
                        <select value={x.by === "forced" ? x.rule : ""} disabled={saving !== null} onChange={(e) => void rechoose(x, e.target.value)}
                          aria-label={`${x.file} のルール`}>
                          <option value="">自動（{x.by === "forced" ? "ルールで決め直す" : x.rule || "未分類"}）</option>
                          {rules.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
                        </select>
                      )}
                      <div className="small muted">{x.error ? "" : `決め方：${BY[x.by] ?? x.by}`}</div>
                      {saving === x.index && <div className="small muted">選び直しています…</div>}
                      {rowError[x.index] && <div className="small error" role="alert">{rowError[x.index]}</div>}
                      {onDraft && !x.error && (
                        <div>
                          <button className="link small" onClick={() => onDraft({ plan_id: planId, index: x.index, file: x.file })}
                            aria-label={`${x.file} からルールの下書きを作る`}>
                            {x.rule ? "ルールの下書きを作る" : "未分類：ルールの下書きを作る"}
                          </button>
                        </div>
                      )}
                    </td>
                    <td className="small">
                      {Object.entries(x.fields).length === 0 && "—"}
                      {Object.entries(x.fields).map(([k, v]) => <div key={k}>{FIELD_LABELS[k] ?? k}：{v}</div>)}
                    </td>
                    <td className="dest small">
                      {x.error ? <span className="error">{x.error}</span> : x.dest}
                      {!x.error && (x.missing?.length ?? 0) > 0 && (
                        <div className="warn" role="note">
                          ⚠ 読み取れなかった項目（{x.missing!.map(label).join("・")}）は「不明」になります。中身を確かめ、合うルールを選んでください。
                        </div>
                      )}
                      {!x.error && (destCount.get(x.dest) ?? 0) > 1 && (
                        <div className="warn" role="note">⚠ 同じ置き先がほかにもあります。実行すると、上書きせずに番号（_2 など）を付けて分けます。</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!done && (
            <div className="toolbar spaced">
              <label className="check">
                <input type="checkbox" checked={move} onChange={(e) => setMove(e.target.checked)} />
                受信箱から移す（外すと、受信箱にも残します）
              </label>
              {!asking ? (
                <button className="primary" disabled={placeable === 0 || saving !== null} onClick={() => setAsking(true)}>実行する</button>
              ) : (
                <span className="confirm">
                  <span>{placeable}件を保存先に{move ? "移します" : "コピーします"}。よろしいですか？</span>
                  <button className="primary" disabled={executing} onClick={() => void execute()}>{executing ? "実行しています…" : "はい、実行する"}</button>
                  <button disabled={executing} onClick={() => setAsking(false)}>やめる</button>
                </span>
              )}
            </div>
          )}
          {plan.result && (
            <div className="spaced" role="status">
              <p><strong>{plan.result.placed.length}件を{plan.result.moved ? "移しました" : "コピーしました"}。</strong></p>
              {plan.result.placed.length > 0 && <ul className="result-list small">{plan.result.placed.map((p) => <li key={p}>{p}</li>)}</ul>}
              {(plan.result.renamed?.length ?? 0) > 0 && (
                <>
                  <p>同じ名前のファイルがあったので、番号を付けて置いたもの（{plan.result.renamed!.length}件）：</p>
                  <ul className="result-list small">{plan.result.renamed!.map((r) => <li key={r.placed}>{r.file} → {r.placed}</li>)}</ul>
                </>
              )}
              {plan.result.skipped.length > 0 && (
                <>
                  <p>置かなかったもの（{plan.result.skipped.length}件）：</p>
                  <ul className="result-list small">{plan.result.skipped.map((s) => <li key={s.file}>{s.file}：{s.reason}</li>)}</ul>
                </>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function History({ tick }: { tick: number }) {
  const [records, setRecords] = useState<SortRecord[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<{ records: SortRecord[] }>("GET", "/api/paper/history?limit=100")
      .then((r) => {
        setRecords(r.records);
        setError("");
      })
      .catch((e) => setError(errorText(e, "仕分けの記録を読み込めませんでした")));
  }, [tick]);

  return (
    <section className="card">
      <h2>仕分けの記録</h2>
      <p className="small muted">総務の全員が仕分けたものです（新しい順に100件）。</p>
      {error && <p className="error" role="alert">{error}</p>}
      {!error && records === null && <p className="muted">読み込んでいます…</p>}
      {records?.length === 0 && <p className="muted">まだ記録はありません。</p>}
      {records && records.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>時刻</th><th>元のファイル</th><th>ルール</th><th>置いた先</th></tr></thead>
            <tbody>
              {records.map((r, i) => (
                <tr key={`${r.time}-${r.source}-${i}`}>
                  <td className="small">{time(r.time)}</td>
                  <td>{r.source}{r.ocr && <div className="small muted">OCR</div>}</td>
                  <td className="nowrap">{r.rule}<div className="small muted">{BY[r.by] ?? r.by}</div></td>
                  <td className="dest small">{r.dest}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
