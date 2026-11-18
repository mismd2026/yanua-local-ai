// S-55 監査ログ（管理者だけ。K-009）。だれが・いつ・何をしたかを、期間・した人・操作の種類・結果で絞り込んで見る
// - 新しい順、ページ送り。見たことも記録される（API の側で audit.view を残す）
// - 記録は足すだけ。この画面にも API にも、消す・書き換える操作はない
// - CSV への書き出しは、今は作らない（K-042 と一緒に考える）
import { FormEvent, useEffect, useState } from "react";
import { api, AuditEntry, AuditPage } from "../api";
import { errorText, time } from "../ui";

const PER_PAGE = 50;
const RESULTS = ["成功", "失敗", "拒否"] as const;
const TONE: Record<string, string> = { 成功: "ok", 失敗: "ng", 拒否: "ng" };

// 操作の名前の頭 → 画面に出す言葉（絞り込みの選び肢）
const GROUPS: [string, string][] = [
  ["auth", "ログイン・ログアウト"],
  ["ask", "質問（チャット・ヘルプデスク）"],
  ["access", "権限で断った操作"],
  ["api", "入口で断ったリクエスト"],
  ["job", "ジョブ"],
  ["kiosk", "受付端末"],
  ["paper", "PDF の仕分け・ルール"],
  ["file", "ファイルのダウンロード"],
  ["meeting", "議事録"],
  ["audit", "監査ログ"],
];

type Filters = { since: string; until: string; actor: string; action: string; result: string };
const EMPTY: Filters = { since: "", until: "", actor: "", action: "", result: "" };

export default function Audit() {
  const [form, setForm] = useState<Filters>(EMPTY);
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<AuditPage | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    const q = new URLSearchParams({ page: String(page), per_page: String(PER_PAGE) });
    for (const [k, v] of Object.entries(filters)) if (v.trim()) q.set(k, v.trim());
    api<AuditPage>("GET", `/api/audit?${q}`)
      .then((r) => {
        if (!alive) return;
        setData(r);
        setError("");
      })
      .catch((e) => alive && setError(errorText(e, "監査ログを読み込めませんでした")));
    return () => {
      alive = false;
    };
  }, [filters, page]);

  const search = (e: FormEvent) => {
    e.preventDefault();
    if (form.since && form.until && form.since > form.until) return setError("期間の始めは、終わりより前の日にしてください");
    setPage(1);
    setFilters({ ...form });
  };
  const set = (k: keyof Filters) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const pages = data ? Math.max(1, Math.ceil(data.total / data.per_page)) : 1;
  // 選び肢：決まった頭のうち、記録にあるもの＋記録にある操作の名前そのもの
  const known = data?.actions ?? [];
  const groups = GROUPS.filter(([g]) => known.some((a) => a.startsWith(`${g}.`)));

  return (
    <>
      <h1 className="page-title">監査ログ</h1>
      <div className="jobs-page">
        <section className="card">
          <form className="form" onSubmit={search} aria-label="絞り込み">
            <div className="form-grid">
              <div className="form-row">
                <label htmlFor="a-since">期間の始め</label>
                <input id="a-since" type="date" value={form.since} onChange={(e) => set("since")(e.target.value)} />
              </div>
              <div className="form-row">
                <label htmlFor="a-until">期間の終わり（その日まで）</label>
                <input id="a-until" type="date" value={form.until} onChange={(e) => set("until")(e.target.value)} />
              </div>
              <div className="form-row">
                <label htmlFor="a-actor">した人（社員番号・名前の一部・端末）</label>
                <input id="a-actor" type="text" maxLength={100} value={form.actor} onChange={(e) => set("actor")(e.target.value)} />
              </div>
              <div className="form-row">
                <label htmlFor="a-action">操作の種類</label>
                <select id="a-action" value={form.action} onChange={(e) => set("action")(e.target.value)}>
                  <option value="">すべて</option>
                  {groups.map(([g, label]) => <option key={g} value={g}>{label}（{g}.…）</option>)}
                  {known.map((a) => <option key={a} value={a}>{a}</option>)}
                </select>
              </div>
              <div className="form-row">
                <label htmlFor="a-result">結果</label>
                <select id="a-result" value={form.result} onChange={(e) => set("result")(e.target.value)}>
                  <option value="">すべて</option>
                  {RESULTS.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
            </div>
            <div className="toolbar">
              <button type="submit" className="primary">絞り込む</button>
              <button type="button" onClick={() => { setForm(EMPTY); setFilters(EMPTY); setPage(1); }}>条件を消す</button>
            </div>
          </form>
          {data && (
            <p className="small muted">
              記録は足すだけで、消したり書き換えたりはできません。{data.keep_days}日を過ぎた記録は自動で消え、消したことが記録に残ります。
              この画面を開いたことも記録されます。
              {data.keep_body ? "設定により、質問などの本文も残しています。" : "質問・会話の本文や文書の中身は残していません。"}
            </p>
          )}
        </section>
        <section className="card" aria-label="監査ログの一覧">
          {error && <p className="error" role="alert">{error}</p>}
          {!error && !data && <p className="muted">読み込んでいます…</p>}
          {data && (
            <>
              <div className="row-between">
                <span role="status">{data.total}件{data.total > 0 && `（${(page - 1) * data.per_page + 1}〜${Math.min(page * data.per_page, data.total)}件目）`}</span>
                <Pager page={page} pages={pages} onPage={setPage} />
              </div>
              {data.entries.length === 0 ? (
                <p className="muted">当てはまる記録はありません。</p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead><tr><th>時刻</th><th>した人</th><th>操作</th><th>対象</th><th>結果</th><th>接続元</th><th>説明</th></tr></thead>
                    <tbody>
                      {data.entries.map((e) => <Row key={e.id} e={e} open={open === e.id} onToggle={() => setOpen(open === e.id ? null : e.id)} />)}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="row-between spaced"><span /><Pager page={page} pages={pages} onPage={setPage} /></div>
            </>
          )}
        </section>
      </div>
    </>
  );
}

function Row({ e, open, onToggle }: { e: AuditEntry; open: boolean; onToggle: () => void }) {
  const who = e.actor_kind === "なし" || e.actor_kind === "システム" ? e.actor_name : `${e.actor_name}（${e.actor}）`;
  return (
    <tr>
      <td className="small nowrap">{time(e.at)}</td>
      <td>
        {who}
        {/* 種類は受付端末のときだけ添える（ログイン前の「なし」・サーバーの「システム」は名前と同じなので1つだけ出す） */}
        {e.actor_kind === "受付端末" && <div className="small muted">{e.actor_kind}</div>}
      </td>
      <td className="mono small">{e.action}</td>
      <td className="small">{e.target || "—"}</td>
      <td><span className={`status ${TONE[e.result] ?? "run"}`}>{e.result}</span></td>
      <td className="small mono">{e.ip || "—"}</td>
      <td className="small">
        {e.detail}
        {e.body && (
          <div>
            <button className="link small" onClick={onToggle} aria-expanded={open}>{open ? "本文を閉じる" : "本文を見る"}</button>
            {open && <pre className="mono small excerpt">{e.body}</pre>}
          </div>
        )}
      </td>
    </tr>
  );
}

function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null;
  return (
    <nav className="toolbar" aria-label="ページ送り">
      <button disabled={page <= 1} onClick={() => onPage(page - 1)}>前のページ</button>
      <span className="small">{page} / {pages} ページ</span>
      <button disabled={page >= pages} onClick={() => onPage(page + 1)}>次のページ</button>
    </nav>
  );
}
