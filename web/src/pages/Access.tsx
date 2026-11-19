// S-54 権限（役割 × 対象 × グループ。docs/access-model.md）。見るのは roles.view、変えるのは roles.assign
// - 4つのタブ：グループ・役割・対象・この人は何ができるか
// - 変える前に、試しに当てるだけの API（/api/admin/access/preview）で「だれのどの権限が増える・減るか」を出す。保存はしない
// - 自分の権限が減る変更は「外すと、あなた自身では戻せません」と出して、もう一度押させる
// - 断られる変更（自分の権限が増える・決まりのグループに手で入れる・最後の roles.assign 保持者を外す）は、理由を欄の近くに出す
// - 対象の選び肢は「種類：名前」で出す（グループ：人事・機能：人事 を見分けるため）
// - 変更はすべて今の API を通す（画面専用の抜け道はない）。守りは API の側
import { ReactNode, useCallback, useEffect, useState } from "react";
import {
  AccessExplain, AccessGroup, AccessPerson, AccessPreview, AccessRole, AccessSet, AccessTarget, AccessView, api, ApiError,
} from "../api";
import { errorText } from "../ui";

type Tab = "groups" | "roles" | "targets" | "who";
const TABS: [Tab, string][] = [["groups", "グループ"], ["roles", "役割"], ["targets", "対象"], ["who", "この人は何ができるか"]];
export const SELF_LOSS_WARNING = "外すと、あなた自身では戻せません。ほかの roles.assign を持つ人に頼むことになります";

// 1つの変更：op と args は試しに当てる API へ、method・path・body は保存の API へ（どちらも同じ中身）
interface Request {
  at: string; // 確かめの欄を出す場所（欄の近くに出すため）
  op: string;
  args: Record<string, unknown>;
  method: string;
  path: string;
  body?: unknown;
  after?: (v: AccessView) => void;
}
interface Pending { req: Request; preview: AccessPreview }

export default function AccessPage({ canEdit = false }: { canEdit?: boolean }) {
  const [view, setView] = useState<AccessView | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("groups");
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    api<AccessView>("GET", "/api/admin/access")
      .then((v) => { setView(v); setError(""); })
      .catch((e) => setError(errorText(e, "権限の設定を読み込めませんでした")));
  }, [tick]);

  const changer = useChanger(setView);
  return (
    <>
      <h1 className="page-title">権限</h1>
      <div className="jobs-page">
        {error && <p className="error" role="alert">{error}</p>}
        <section className="card">
          <p className="small muted">
            権限は人に直接は与えず、グループに「役割 × 対象」を与えます。変える前に、だれのどの権限が増える・減るかを確かめられます。
            自分の権限が増える変更はできません。変更はすべて監査ログに残ります。
            {!canEdit && " あなたは見るだけです（変えるには roles.assign が要ります）。"}
          </p>
          <div className="tabs" role="tablist" aria-label="権限の区切り">
            {TABS.map(([key, label]) => (
              <button key={key} role="tab" aria-selected={tab === key} className={`tab${tab === key ? " on" : ""}`}
                onClick={() => { setTab(key); changer.cancel(); }}>{label}</button>
            ))}
          </div>
          {changer.message && <p className="notice" role="status">{changer.message}</p>}
        </section>
        {view && tab === "groups" && <Groups view={view} canEdit={canEdit} changer={changer} />}
        {view && tab === "roles" && <Roles view={view} canEdit={canEdit} changer={changer} />}
        {view && tab === "targets" && <Targets view={view} canEdit={canEdit} changer={changer} />}
        {view && tab === "who" && <Who view={view} />}
        <button className="link small" onClick={reload}>読み込み直す</button>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- 変える前の確かめ

type Changer = ReturnType<typeof useChanger>;

function useChanger(setView: (v: AccessView) => void) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [failure, setFailure] = useState<{ at: string; text: string; where: string[] } | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const cancel = () => { setPending(null); setFailure(null); };

  // 1回目：試しに当てる（保存しない）。断られる変更なら、理由を欄の近くに出す
  const propose = async (req: Request) => {
    setBusy(true);
    setMessage("");
    setFailure(null);
    try {
      const preview = await api<AccessPreview>("POST", "/api/admin/access/preview", { op: req.op, args: req.args });
      if (preview.refused) {
        setPending(null);
        setFailure({ at: req.at, text: preview.refused, where: preview.problems.map((p) => p.where) });
      } else setPending({ req, preview });
    } catch (e) {
      setPending(null);
      setFailure({ at: req.at, text: errorText(e, "確かめられませんでした"), where: [] });
    } finally {
      setBusy(false);
    }
  };

  // 2回目：保存する（自分の権限が減るときは、戻せないことを出したうえで、もう一度押してもらう）
  const save = async () => {
    if (!pending) return;
    const { req } = pending;
    setBusy(true);
    try {
      const r = await api<AccessView & { detail: string }>(req.method, req.path, req.body);
      setView(r);
      setMessage(`保存しました：${r.detail}`);
      setPending(null);
      req.after?.(r);
    } catch (e) {
      setPending(null);
      const where = e instanceof ApiError ? e.details.map((d) => d.where) : [];
      setFailure({ at: req.at, text: errorText(e, "保存できませんでした"), where });
    } finally {
      setBusy(false);
    }
  };

  // 欄の近くに出す：確かめの中身・断られた理由
  const box = (at: string): ReactNode => {
    if (failure?.at === at)
      return <p className="error" role="alert" aria-label="変えられない理由">{failure.text}</p>;
    if (pending?.req.at !== at) return null;
    const p = pending.preview;
    return (
      <div className="notice-box" role="region" aria-label="変更の確かめ">
        <p><strong>この変更：</strong>{p.detail}</p>
        <Impact p={p} />
        {p.self_loss && (
          <div className="warn" role="alert">
            <p><strong>{SELF_LOSS_WARNING}</strong></p>
            <p>あなた自身の減る権限：{p.self.lost.join("、")}</p>
          </div>
        )}
        <p>
          <button className="primary" disabled={busy} onClick={() => void save()}>
            {p.self_loss ? "戻せないことをわかったうえで保存する" : "保存する"}
          </button>{" "}
          <button disabled={busy} onClick={cancel}>やめる</button>
        </p>
      </div>
    );
  };

  const fieldError = (at: string, where: string) => (failure?.at === at && failure.where.includes(where) ? failure.text : "");
  return { propose, cancel, box, busy, message, fieldError };
}

function Impact({ p }: { p: AccessPreview }) {
  if (p.too_many)
    return (
      <>
        <p className="small">効く人が多い（{p.checked}人）ので、グループごとに出します（そのグループから来る権限）。</p>
        <ul className="small">
          {p.groups.map((g) => (
            <li key={g.group_id}>
              「{g.name}」（{g.members}人）：{g.gained.length > 0 && <>増える {g.gained.join("、")}　</>}
              {g.lost.length > 0 && <>減る {g.lost.join("、")}</>}
              {g.gained.length + g.lost.length === 0 && "変わらない"}
            </li>
          ))}
        </ul>
      </>
    );
  if (p.people.length === 0) return <p className="small muted">だれの権限も変わりません。</p>;
  return (
    <div className="table-wrap">
      <table className="table" aria-label="権限が変わる人">
        <thead><tr><th>人</th><th>増える権限</th><th>減る権限</th></tr></thead>
        <tbody>
          {p.people.map((x) => (
            <tr key={x.account_id}>
              <td>{x.name}（{x.account_id}{x.dept ? `・${x.dept}` : ""}）</td>
              <td>{x.gained.join("、") || "—"}</td>
              <td>{x.lost.join("、") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- グループ

function Groups({ view, canEdit, changer }: { view: AccessView; canEdit: boolean; changer: Changer }) {
  const [selected, setSelected] = useState(view.groups[0]?.group_id ?? "");
  const g = view.groups.find((x) => x.group_id === selected);
  return (
    <>
      <section className="card" aria-label="グループの一覧">
        <h2>グループ</h2>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>名前</th><th>入り方</th><th>メンバー数</th><th>与えている役割 × 対象</th></tr></thead>
            <tbody>
              {view.groups.map((x) => (
                <tr key={x.group_id} className={x.group_id === selected ? "selected" : ""}>
                  <td><button className="link" onClick={() => { setSelected(x.group_id); changer.cancel(); }}>{x.name}</button></td>
                  <td>{x.how === "rule" ? `決まり（${x.rule_text}）` : "手で入れる"}</td>
                  <td>{x.member_count}人</td>
                  <td>{x.grants.map((gr) => `${gr.role} × ${gr.target}`).join("、") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {g && <GroupDetail key={g.group_id} g={g} view={view} canEdit={canEdit} changer={changer} onDeleted={() => setSelected("")} />}
      {canEdit && <GroupCreate view={view} changer={changer} onCreated={setSelected} />}
    </>
  );
}

function GroupDetail({ g, view, canEdit, changer, onDeleted }: {
  g: AccessGroup; view: AccessView; canEdit: boolean; changer: Changer; onDeleted: () => void;
}) {
  const [desc, setDesc] = useState(g.description);
  const base = `/api/admin/access/groups/${encodeURIComponent(g.group_id)}`;
  const deletable = g.member_count === 0 && g.grants.length === 0;
  return (
    <section className="card" aria-label={`グループ「${g.name}」`}>
      <h2>{g.name}</h2>
      {canEdit ? (
        <p>
          <label>説明 <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
          <button disabled={changer.busy || desc === g.description} onClick={() => void changer.propose({
            at: "group", op: "group.update", args: { group_id: g.group_id, description: desc },
            method: "PATCH", path: base, body: { description: desc },
          })}>説明を直す</button>
        </p>
      ) : <p className="muted">{g.description}</p>}
      {changer.box("group")}

      <h3>メンバー</h3>
      {g.how === "rule" ? (
        <p className="notice">
          決まり（{g.rule_text}）で入るグループです。在籍している {g.rule_count}人 が入ります。メンバーは手で出し入れしません。
          決まりの人に加えて個別の人も入れたいときは、別のグループ（例「{g.name}（個別）」）を作ってください。
        </p>
      ) : (
        <>
          {g.members.length === 0 ? <p className="muted">まだだれもいません。</p> : (
            <div className="table-wrap">
              <table className="table" aria-label="手で入れたメンバー">
                <thead><tr><th>人</th><th>期限</th><th>入れた人</th>{canEdit && <th></th>}</tr></thead>
                <tbody>
                  {g.members.map((m) => (
                    <tr key={m.account_id}>
                      <td>{m.name}（{m.account_id}）{!m.in_hr && <span className="warn small">　人事データにいません</span>}</td>
                      <td>{m.expires_at ?? "期限なし"}{m.expired && <span className="warn small">　期限切れ</span>}</td>
                      <td className="small">{m.added_by}</td>
                      {canEdit && (
                        <td>
                          <button disabled={changer.busy} onClick={() => void changer.propose({
                            at: "members", op: "member.remove", args: { group_id: g.group_id, account_id: m.account_id },
                            method: "DELETE", path: `${base}/members/${encodeURIComponent(m.account_id)}`,
                          })}>外す</button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {canEdit && <MemberAdd g={g} changer={changer} />}
        </>
      )}
      {changer.box("members")}

      <h3>与えている役割 × 対象</h3>
      {g.grants.length === 0 ? <p className="muted">まだ与えていません。</p> : (
        <ul>
          {g.grants.map((gr) => (
            <li key={gr.grant_id}>
              役割「{gr.role}」× {gr.target_kind === "set" ? `まとまり「${gr.target}」` : gr.target}
              {canEdit && (
                <> <button className="small" disabled={changer.busy} onClick={() => void changer.propose({
                  at: "grants", op: "grant.remove", args: { grant_id: gr.grant_id },
                  method: "DELETE", path: `/api/admin/access/grants/${gr.grant_id}`,
                })}>外す</button></>
              )}
            </li>
          ))}
        </ul>
      )}
      {canEdit && <GrantAdd g={g} view={view} changer={changer} />}
      {changer.box("grants")}

      {canEdit && (
        <p>
          <button disabled={changer.busy || !deletable} onClick={() => void changer.propose({
            at: "group-delete", op: "group.delete", args: { group_id: g.group_id }, method: "DELETE", path: base, after: onDeleted,
          })}>このグループを消す</button>
          {!deletable && <span className="small muted">　メンバーも役割もないグループだけ消せます</span>}
        </p>
      )}
      {changer.box("group-delete")}
    </section>
  );
}

function MemberAdd({ g, changer }: { g: AccessGroup; changer: Changer }) {
  const [q, setQ] = useState("");
  const [found, setFound] = useState<AccessPerson[] | null>(null);
  const [who, setWho] = useState<AccessPerson | null>(null);
  const [expires, setExpires] = useState("");
  const [error, setError] = useState("");
  const search = async () => {
    setError("");
    try {
      const r = await api<{ people: AccessPerson[] }>("GET", `/api/admin/access/people?q=${encodeURIComponent(q.trim())}`);
      setFound(r.people);
    } catch (e) {
      setError(errorText(e, "探せませんでした"));
    }
  };
  const expiryError = changer.fieldError("members", "expires_at");
  const accountError = changer.fieldError("members", "account");
  return (
    <div className="form-row" aria-label="メンバーを足す">
      <form onSubmit={(e) => { e.preventDefault(); void search(); }}>
        <label>足す人（社員番号か名前の一部）
          <input type="text" value={q} onChange={(e) => setQ(e.target.value)} aria-invalid={!!accountError} />
        </label>
        <button type="submit" disabled={!q.trim()}>探す</button>
      </form>
      {error && <p className="error">{error}</p>}
      {found && found.length === 0 && <p className="muted small">見つかりませんでした（在籍している人だけ出ます）。</p>}
      {found && found.length > 0 && (
        <ul className="small" aria-label="見つかった人">
          {found.map((p) => (
            <li key={p.account_id}>
              <label>
                <input type="radio" name={`who-${g.group_id}`} checked={who?.account_id === p.account_id} onChange={() => setWho(p)} style={{ width: "auto" }} />
                {p.name}（{p.account_id}{p.dept ? `・${p.dept}` : ""}）
              </label>
            </li>
          ))}
        </ul>
      )}
      <p>
        <label>期限（空なら期限なし。その日の終わりまで）
          <input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} aria-invalid={!!expiryError} />
        </label>
        <button disabled={!who || changer.busy} onClick={() => who && void changer.propose({
          at: "members", op: "member.add", args: { group_id: g.group_id, account_id: who.account_id, expires_at: expires || null },
          method: "POST", path: `/api/admin/access/groups/${encodeURIComponent(g.group_id)}/members`,
          body: { account_id: who.account_id, expires_at: expires || null },
        })}>{who ? `${who.name} を足す` : "人を選んでください"}</button>
      </p>
    </div>
  );
}

function TargetSelect({ view, value, onChange, label }: { view: AccessView; value: string; onChange: (v: string) => void; label: string }) {
  const kinds = Array.from(new Set(view.targets.map((t) => t.kind_name)));
  return (
    <label>{label}{" "}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">選んでください</option>
        {kinds.map((k) => (
          <optgroup key={k} label={k}>
            {view.targets.filter((t) => t.kind_name === k).map((t) => <option key={t.target_id} value={`t:${t.target_id}`}>{t.kind_name}：{t.name}</option>)}
          </optgroup>
        ))}
        {view.target_sets.length > 0 && (
          <optgroup label="対象のまとまり">
            {view.target_sets.map((s) => <option key={s.set_id} value={`s:${s.set_id}`}>まとまり：{s.name}</option>)}
          </optgroup>
        )}
      </select>
    </label>
  );
}

function GrantAdd({ g, view, changer }: { g: AccessGroup; view: AccessView; changer: Changer }) {
  const [role, setRole] = useState("");
  const [target, setTarget] = useState("");
  const body = { role_id: role, target_id: target.startsWith("t:") ? target.slice(2) : null, set_id: target.startsWith("s:") ? target.slice(2) : null };
  return (
    <p className="form-row" aria-label="役割を与える">
      <label>役割{" "}
        <select value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="">選んでください</option>
          {view.roles.map((r) => <option key={r.role_id} value={r.role_id}>{r.name}</option>)}
        </select>
      </label>{" "}
      <TargetSelect view={view} value={target} onChange={setTarget} label="対象" />{" "}
      <button disabled={!role || !target || changer.busy} onClick={() => void changer.propose({
        at: "grants", op: "grant.add", args: { group_id: g.group_id, ...body },
        method: "POST", path: `/api/admin/access/groups/${encodeURIComponent(g.group_id)}/grants`, body,
      })}>与える</button>
    </p>
  );
}

function GroupCreate({ view, changer, onCreated }: { view: AccessView; changer: Changer; onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [how, setHow] = useState<"manual" | "all" | "departments">("manual");
  const [depts, setDepts] = useState<string[]>([]);
  const rule = how === "manual" ? null : how === "all" ? { all: true } : { departments: depts };
  const body = { name, description: desc, rule };
  const nameError = changer.fieldError("group-create", "name");
  return (
    <section className="card" aria-label="グループを作る">
      <h2>グループを作る</h2>
      <p><label>名前 <input type="text" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!nameError} /></label></p>
      <p><label>説明 <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></label></p>
      <fieldset>
        <legend>入り方</legend>
        {([["manual", "手で入れる"], ["all", "全員（決まり）"], ["departments", "部署（決まり）"]] as const).map(([k, label]) => (
          <label key={k} style={{ marginRight: "1em" }}>
            <input type="radio" name="how" checked={how === k} onChange={() => setHow(k)} style={{ width: "auto" }} /> {label}
          </label>
        ))}
        {how === "departments" && (
          <p>
            {view.departments.map((d) => (
              <label key={d} style={{ marginRight: "1em" }}>
                <input type="checkbox" style={{ width: "auto" }} checked={depts.includes(d)}
                  onChange={(e) => setDepts(e.target.checked ? [...depts, d] : depts.filter((x) => x !== d))} /> {d}
              </label>
            ))}
          </p>
        )}
      </fieldset>
      <p>
        <button disabled={!name.trim() || changer.busy} onClick={() => void changer.propose({
          at: "group-create", op: "group.create", args: body, method: "POST", path: "/api/admin/access/groups", body,
          after: (v) => { onCreated(v.groups.find((x) => x.name === name.trim())?.group_id ?? ""); setName(""); setDesc(""); },
        })}>作る</button>
      </p>
      {changer.box("group-create")}
    </section>
  );
}

// ---------------------------------------------------------------- 役割

function Roles({ view, canEdit, changer }: { view: AccessView; canEdit: boolean; changer: Changer }) {
  const [selected, setSelected] = useState(view.roles[0]?.role_id ?? "");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const r = view.roles.find((x) => x.role_id === selected);
  const body = { name, description: desc };
  return (
    <>
      <section className="card" aria-label="役割の一覧">
        <h2>役割</h2>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>名前</th><th>説明</th><th>権限</th><th>与えているグループ</th></tr></thead>
            <tbody>
              {view.roles.map((x) => (
                <tr key={x.role_id} className={x.role_id === selected ? "selected" : ""}>
                  <td><button className="link" onClick={() => { setSelected(x.role_id); changer.cancel(); }}>{x.name}</button></td>
                  <td className="small">{x.description}</td>
                  <td>{x.permissions.length}個</td>
                  <td>{x.used_by.join("、") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {r && <RoleDetail key={r.role_id} r={r} view={view} canEdit={canEdit} changer={changer} onDeleted={() => setSelected("")} />}
      {canEdit && (
        <section className="card" aria-label="役割を作る">
          <h2>役割を作る</h2>
          <p><label>名前 <input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label></p>
          <p><label>説明 <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></label></p>
          <p className="small muted">作ったあとで、一覧から選んで権限にチェックを付けます。</p>
          <button disabled={!name.trim() || changer.busy} onClick={() => void changer.propose({
            at: "role-create", op: "role.create", args: body, method: "POST", path: "/api/admin/access/roles", body,
            after: () => { setName(""); setDesc(""); },
          })}>作る</button>
          {changer.box("role-create")}
        </section>
      )}
    </>
  );
}

function RoleDetail({ r, view, canEdit, changer, onDeleted }: {
  r: AccessRole; view: AccessView; canEdit: boolean; changer: Changer; onDeleted: () => void;
}) {
  const [perms, setPerms] = useState<string[]>(r.permissions);
  const features = Array.from(new Set(view.permissions.map((p) => p.feature)));
  const changed = perms.length !== r.permissions.length || perms.some((p) => !r.permissions.includes(p));
  const path = `/api/admin/access/roles/${encodeURIComponent(r.role_id)}`;
  return (
    <section className="card" aria-label={`役割「${r.name}」`}>
      <h2>{r.name}</h2>
      <p className="muted">{r.description}　与えているグループ：{r.used_by.join("、") || "なし"}</p>
      {features.map((f) => (
        <fieldset key={f}>
          <legend>{f}</legend>
          {view.permissions.filter((p) => p.feature === f).map((p) => (
            <label key={p.name} style={{ display: "block" }}>
              <input type="checkbox" style={{ width: "auto" }} disabled={!canEdit} checked={perms.includes(p.name)}
                onChange={(e) => setPerms(e.target.checked ? [...perms, p.name] : perms.filter((x) => x !== p.name))} />{" "}
              <code>{p.name}</code>　{p.does}<span className="small muted">（効く対象：{p.targets.join("・")}）</span>
            </label>
          ))}
        </fieldset>
      ))}
      {canEdit && (
        <p>
          <button className="primary" disabled={!changed || changer.busy} onClick={() => void changer.propose({
            at: "role", op: "role.update", args: { role_id: r.role_id, permissions: perms },
            method: "PUT", path, body: { permissions: perms },
          })}>権限を確かめる</button>{" "}
          <button disabled={!changed} onClick={() => { setPerms(r.permissions); changer.cancel(); }}>元に戻す</button>{" "}
          <button disabled={changer.busy || r.used_by.length > 0} onClick={() => void changer.propose({
            at: "role", op: "role.delete", args: { role_id: r.role_id }, method: "DELETE", path, after: onDeleted,
          })}>この役割を消す</button>
          {r.used_by.length > 0 && <span className="small muted">　どのグループにも与えていない役割だけ消せます</span>}
        </p>
      )}
      {changer.box("role")}
    </section>
  );
}

// ---------------------------------------------------------------- 対象

function Targets({ view, canEdit, changer }: { view: AccessView; canEdit: boolean; changer: Changer }) {
  const [editing, setEditing] = useState<AccessTarget | null>(null);
  return (
    <>
      <section className="card" aria-label="対象の一覧">
        <h2>対象</h2>
        <p className="small muted">
          部署（人事データから）・グループ・機能は自動でそろえます。手で作るのはシステムとサーバーだけです。
          接続情報は参照名（環境変数の名前）だけを持ち、値は画面にもどこにも出しません。
        </p>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>種類</th><th>名前</th><th>作り方</th><th>宛先</th><th>接続情報の参照名</th><th>使っている</th>{canEdit && <th></th>}</tr></thead>
            <tbody>
              {view.targets.map((t) => (
                <tr key={t.target_id}>
                  <td>{t.kind_name}</td>
                  <td>{t.name}{t.description && <span className="small muted">　{t.description}</span>}
                    {t.stale && <span className="warn small">　もとの部署・グループがなくなりました</span>}</td>
                  <td>{t.auto ? "自動" : "手"}</td>
                  <td>{t.host || "—"}</td>
                  <td>{t.secret_ref ? <code>{t.secret_ref}</code> : "—"}</td>
                  <td>{t.used ? "はい" : "—"}</td>
                  {canEdit && (
                    <td>
                      {!t.auto && (
                        <>
                          <button className="small" onClick={() => { setEditing(t); changer.cancel(); }}>直す</button>{" "}
                          <button className="small" disabled={t.used || changer.busy} onClick={() => void changer.propose({
                            at: "targets", op: "target.delete", args: { target_id: t.target_id },
                            method: "DELETE", path: `/api/admin/access/targets/${encodeURIComponent(t.target_id)}`,
                          })}>消す</button>
                        </>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {changer.box("targets")}
      </section>
      {canEdit && <TargetForm key={editing?.target_id ?? "new"} view={view} changer={changer} editing={editing} onDone={() => setEditing(null)} />}
      <Sets view={view} canEdit={canEdit} changer={changer} />
    </>
  );
}

function TargetForm({ view, changer, editing, onDone }: { view: AccessView; changer: Changer; editing: AccessTarget | null; onDone: () => void }) {
  const [kind, setKind] = useState(editing?.kind ?? "system");
  const [name, setName] = useState(editing?.name ?? "");
  const [desc, setDesc] = useState(editing?.description ?? "");
  const [host, setHost] = useState(editing?.host ?? "");
  const [secret, setSecret] = useState(editing?.secret_ref ?? "");
  const at = "target-form";
  const fields = { description: desc, host, secret_ref: secret.trim() };
  const req: Request = editing
    ? { at, op: "target.update", args: { target_id: editing.target_id, ...fields }, method: "PATCH",
        path: `/api/admin/access/targets/${encodeURIComponent(editing.target_id)}`, body: fields, after: onDone }
    : { at, op: "target.create", args: { kind, name, ...fields }, method: "POST", path: "/api/admin/access/targets",
        body: { kind, name, ...fields }, after: () => { setName(""); setDesc(""); setHost(""); setSecret(""); } };
  const secretError = changer.fieldError(at, "secret_ref");
  const hostError = changer.fieldError(at, "host");
  return (
    <section className="card" aria-label={editing ? `「${editing.name}」を直す` : "システム・サーバーを作る"}>
      <h2>{editing ? `「${editing.name}」を直す` : "システム・サーバーを作る"}</h2>
      {!editing && (
        <p>
          <label>種類{" "}
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="system">システム</option>
              <option value="server">サーバー</option>
            </select>
          </label>
        </p>
      )}
      {!editing && <p><label>名前 <input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label></p>}
      <p><label>説明 <input type="text" value={desc} onChange={(e) => setDesc(e.target.value)} /></label></p>
      <p>
        <label>つながる宛先（通信とモデルの画面の宛先から）{" "}
          <select value={host} onChange={(e) => setHost(e.target.value)} aria-invalid={!!hostError}>
            <option value="">なし</option>
            {view.hosts.map((h) => <option key={h} value={h}>{h}</option>)}
          </select>
        </label>
      </p>
      <p>
        <label>接続情報の参照名（環境変数の名前だけ。例 AWS_READONLY_KEY）
          <input type="text" value={secret} onChange={(e) => setSecret(e.target.value)} aria-invalid={!!secretError} autoComplete="off" />
        </label>
        <span className="small muted">値（鍵・パスワード）はここに書きません。サーバーの環境変数に入れておきます。</span>
      </p>
      <p>
        <button disabled={changer.busy || (!editing && !name.trim())} onClick={() => void changer.propose(req)}>
          {editing ? "直す" : "作る"}
        </button>
        {editing && <> <button onClick={onDone}>やめる</button></>}
      </p>
      {changer.box(at)}
    </section>
  );
}

function Sets({ view, canEdit, changer }: { view: AccessView; canEdit: boolean; changer: Changer }) {
  const [editing, setEditing] = useState<AccessSet | null>(null);
  const [name, setName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const names = Object.fromEntries(view.targets.map((t) => [t.target_id, t.name]));
  const start = (s: AccessSet | null) => { setEditing(s); setName(s?.name ?? ""); setMembers(s?.members ?? []); changer.cancel(); };
  const choosable = view.targets.filter((t) => t.kind !== "special");
  const body = { name, members };
  return (
    <section className="card" aria-label="対象のまとまり">
      <h2>対象のまとまり</h2>
      {view.target_sets.length === 0 ? <p className="muted">まだありません。</p> : (
        <ul>
          {view.target_sets.map((s) => (
            <li key={s.set_id}>
              <strong>{s.name}</strong>：{s.members.map((m) => names[m] ?? m).join("・")}
              <span className="small muted">　与えているグループ：{s.used_by.join("、") || "なし"}</span>
              {canEdit && (
                <> <button className="small" onClick={() => start(s)}>中身を直す</button>{" "}
                  <button className="small" disabled={s.used_by.length > 0 || changer.busy} onClick={() => void changer.propose({
                    at: "sets", op: "set.delete", args: { set_id: s.set_id },
                    method: "DELETE", path: `/api/admin/access/target-sets/${encodeURIComponent(s.set_id)}`,
                  })}>消す</button></>
              )}
            </li>
          ))}
        </ul>
      )}
      {canEdit && (
        <div aria-label={editing ? `まとまり「${editing.name}」の中身` : "まとまりを作る"}>
          <h3>{editing ? `「${editing.name}」の中身を直す` : "まとまりを作る"}</h3>
          {!editing && <p><label>名前 <input type="text" value={name} onChange={(e) => setName(e.target.value)} /></label></p>}
          <p>
            {choosable.map((t) => (
              <label key={t.target_id} style={{ marginRight: "1em", display: "inline-block" }}>
                <input type="checkbox" style={{ width: "auto" }} checked={members.includes(t.target_id)}
                  onChange={(e) => setMembers(e.target.checked ? [...members, t.target_id] : members.filter((x) => x !== t.target_id))} />
                {" "}{t.kind_name}：{t.name}
              </label>
            ))}
          </p>
          <button disabled={changer.busy || members.length === 0 || (!editing && !name.trim())} onClick={() => void changer.propose(editing
            ? { at: "sets", op: "set.update", args: { set_id: editing.set_id, members }, method: "PUT",
                path: `/api/admin/access/target-sets/${encodeURIComponent(editing.set_id)}`, body: { members }, after: () => start(null) }
            : { at: "sets", op: "set.create", args: body, method: "POST", path: "/api/admin/access/target-sets", body, after: () => start(null) })}>
            {editing ? "中身を確かめる" : "作る"}
          </button>
          {editing && <> <button onClick={() => start(null)}>やめる</button></>}
        </div>
      )}
      {changer.box("sets")}
    </section>
  );
}

// ---------------------------------------------------------------- この人は何ができるか

function Who({ view }: { view: AccessView }) {
  const [q, setQ] = useState("");
  const [r, setR] = useState<AccessExplain | null>(null);
  const [error, setError] = useState("");
  const look = async (query: string) => {
    setError("");
    try {
      setR(await api<AccessExplain>("GET", `/api/admin/access/explain?q=${encodeURIComponent(query.trim())}`));
    } catch (e) {
      setError(errorText(e, "調べられませんでした"));
    }
  };
  const systems = view.accounts.filter((a) => a.kind === "system");
  const res = r?.result;
  return (
    <section className="card" aria-label="この人は何ができるか">
      <h2>この人は何ができるか</h2>
      <form onSubmit={(e) => { e.preventDefault(); void look(q); }}>
        <label>社員番号か名前 <input type="text" value={q} onChange={(e) => setQ(e.target.value)} /></label>
        <button type="submit" disabled={!q.trim()}>調べる</button>
      </form>
      <p className="small">
        システムアカウント：{systems.map((a) => (
          <button key={a.account_id} className="link" onClick={() => { setQ(a.account_id); void look(a.account_id); }}>{a.name}</button>
        ))}
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {r && !res && r.candidates.length === 0 && <p className="muted">見つかりませんでした（在籍している人だけ出ます）。</p>}
      {r && !res && r.candidates.length > 0 && (
        <>
          <p>何人か見つかりました。選んでください。</p>
          <ul aria-label="候補">
            {r.candidates.map((c) => (
              <li key={c.account_id}>
                <button className="link" onClick={() => { setQ(c.account_id); void look(c.account_id); }}>
                  {c.name}（{c.account_id}{c.dept ? `・${c.dept}` : ""}）
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {res && (
        <>
          <p><strong>{res.name}</strong>（{res.account_id}{res.dept ? `・${res.dept}` : ""}）　入っているグループ：{res.groups.join("、") || "なし"}</p>
          {res.note && <p className="notice" role="note">{res.note}</p>}
          {res.permissions.length === 0 ? <p className="muted">この仕組みで与えている権限はありません。</p> : (
            <div className="table-wrap">
              <table className="table" aria-label="持っている権限">
                <thead><tr><th>権限</th><th>できること</th><th>グループ</th><th>役割</th><th>対象</th><th>期限</th></tr></thead>
                <tbody>
                  {res.permissions.flatMap((p) => p.from.map((f, i) => (
                    <tr key={`${p.permission}-${i}`}>
                      <td>{i === 0 ? <code>{p.permission}</code> : ""}</td>
                      <td className="small">{i === 0 ? `${p.feature}：${p.does}` : ""}</td>
                      <td>{f.group}<span className="small muted">（{f.via}）</span></td>
                      <td>{f.role}</td>
                      <td>{f.target}</td>
                      <td>{f.expires_at ?? "期限なし"}</td>
                    </tr>
                  )))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
