// S-13 議事録（社員）。録画のアップロード → 進み具合 → 話し手・要約・宿題の確認と修正 → Word 出力
// - 議事録は作った本人だけが見られる（総務・管理者でも、ほかの人のものは見えない。API の側で守る）
// - 話し手が「不明」の発言は、選んでまとめて直せる
import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, Job, Meeting, MEETING_TOOLS, MeetingListItem, MeetingSegment, MeetingSummary, Todo, uploadFile } from "../api";
import { useJob } from "../useJob";
import { errorText, JobProgress, queryParam, setQuery, Status, time, todayIso } from "../ui";

const UNKNOWN = "不明";

export default function Meetings() {
  const [openId, setOpenId] = useState<string | null>(() => queryParam("id"));
  const [list, setList] = useState<MeetingListItem[] | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  const open = (id: string | null) => {
    setOpenId(id);
    setQuery("id", id);
  };

  useEffect(() => {
    api<{ meetings: MeetingListItem[] }>("GET", "/api/meetings")
      .then((r) => {
        setList(r.meetings);
        setError("");
      })
      .catch((e) => setError(errorText(e, "議事録の一覧を読み込めませんでした")));
  }, [tick]);

  return (
    <>
      <h1 className="page-title">議事録</h1>
      {openId ? (
        <MeetingDetail id={openId} onBack={() => { open(null); setTick((t) => t + 1); }} onChanged={() => setTick((t) => t + 1)} />
      ) : (
        <div className="jobs-page">
          <NewMeeting onCreated={(id) => { setTick((t) => t + 1); open(id); }} />
          <section className="card">
            <h2>自分の議事録</h2>
            <p className="small muted">あなたが作った議事録だけが出ます。ほかの人には見えません。</p>
            {error && <p className="error" role="alert">{error}</p>}
            {!error && list === null && <p className="muted">読み込んでいます…</p>}
            {list?.length === 0 && <p className="muted">まだ議事録はありません。</p>}
            {list && list.length > 0 && (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>会議名</th><th>開催日</th><th>状態</th><th>作った時刻</th></tr></thead>
                  <tbody>
                    {list.map((m) => (
                      <tr key={m.meeting_id}>
                        <td><button className="link" onClick={() => open(m.meeting_id)}>{m.title}</button></td>
                        <td className="nowrap">{m.date}</td>
                        <td><Status s={m.status} /></td>
                        <td className="small">{time(m.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}

const lines = (s: string) => s.split(/[\n、,]/).map((x) => x.trim()).filter(Boolean);

function NewMeeting({ onCreated }: { onCreated: (meetingId: string) => void }) {
  const [media, setMedia] = useState<File | null>(null);
  const [segments, setSegments] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [day, setDay] = useState(todayIso());
  const [place, setPlace] = useState("");
  const [tool, setTool] = useState("zoom");
  const [people, setPeople] = useState("");
  const [summary, setSummary] = useState(true);
  const [stage, setStage] = useState("");
  const [pct, setPct] = useState<number | null>(null);
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    if (!media) return setError("録画・録音のファイルを選んでください");
    if (!title.trim()) return setError("会議名を入れてください");
    try {
      setStage("録画を送っています");
      setPct(0);
      const up = await uploadFile("recording", media, setPct);
      let segId: string | null = null;
      if (segments) {
        setStage("文字起こし済みの発言を送っています");
        setPct(0);
        segId = (await uploadFile("segments", segments, setPct)).file_id;
      }
      setStage("議事録づくりを始めています");
      setPct(null);
      const r = await api<Job & { meeting_id: string }>("POST", "/api/meetings", {
        upload_id: up.file_id, segments_upload_id: segId, title: title.trim(), date: day, place: place.trim(), tool,
        participants: lines(people), summary,
      });
      onCreated(r.meeting_id);
    } catch (err) {
      setError(errorText(err, "議事録づくりを始められませんでした"));
    } finally {
      setStage("");
      setPct(null);
    }
  };

  const busy = !!stage;
  return (
    <section className="card">
      <h2>新しい議事録</h2>
      <form className="form" onSubmit={submit}>
        <div className="form-row">
          <label htmlFor="m-media">録画・録音（2GB まで）</label>
          <input id="m-media" type="file" accept="video/*,audio/*,.mp4,.m4a,.mp3,.wav,.webm,.mov,.mkv,.aac" disabled={busy}
            onChange={(e) => setMedia(e.target.files?.[0] ?? null)} />
        </div>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="m-title">会議名</label>
            <input id="m-title" type="text" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} disabled={busy} />
          </div>
          <div className="form-row">
            <label htmlFor="m-date">開催日</label>
            <input id="m-date" type="date" value={day} onChange={(e) => setDay(e.target.value)} disabled={busy} />
          </div>
          <div className="form-row">
            <label htmlFor="m-place">場所</label>
            <input id="m-place" type="text" maxLength={200} value={place} onChange={(e) => setPlace(e.target.value)} disabled={busy} />
          </div>
          <div className="form-row">
            <label htmlFor="m-tool">会議のツール</label>
            <select id="m-tool" value={tool} onChange={(e) => setTool(e.target.value)} disabled={busy}>
              {MEETING_TOOLS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
        </div>
        <p className="small muted">話し手は、録画の画面に出る名前から読み取ります。読めなかった発言は「不明」になり、あとでまとめて直せます。</p>
        <div className="form-row">
          <label htmlFor="m-people">出席者（任意。1行に1人か「、」で区切る）</label>
          <textarea id="m-people" rows={2} value={people} onChange={(e) => setPeople(e.target.value)} disabled={busy} />
        </div>
        <details>
          <summary>くわしい設定</summary>
          <div className="form spaced">
            <label className="check">
              <input type="checkbox" checked={summary} onChange={(e) => setSummary(e.target.checked)} disabled={busy} />
              要約・決まったこと・宿題を、社内の LLM で下書きする
            </label>
            <div className="form-row">
              <label htmlFor="m-segments">文字起こし済みの発言（JSON。あれば文字起こしをしません）</label>
              <input id="m-segments" type="file" accept=".json,application/json" disabled={busy}
                onChange={(e) => setSegments(e.target.files?.[0] ?? null)} />
            </div>
          </div>
        </details>
        {busy && (
          <p className="progress-cell" aria-live="polite">
            {pct !== null && <progress max={100} value={pct} aria-label="アップロードの進み具合" />}
            <span className="small">　{stage}{pct !== null && ` ${pct}%`}</span>
          </p>
        )}
        {error && <p className="error" role="alert">{error}</p>}
        <div><button type="submit" className="primary" disabled={busy}>議事録を作る</button></div>
      </form>
    </section>
  );
}

const rows = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

const EMPTY_SUMMARY: MeetingSummary = { summary: "", points: [], decisions: [], todos: [], next_meeting: "" };

function clock(sec: number): string {
  const s = Math.floor(sec);
  const pad = (n: number) => String(n).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

function MeetingDetail({ id, onBack, onChanged }: { id: string; onBack: () => void; onChanged: () => void }) {
  const [m, setM] = useState<Meeting | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api<Meeting>("GET", `/api/meetings/${encodeURIComponent(id)}`)
      .then((r) => {
        setM(r);
        setError("");
      })
      .catch((e) => setError(errorText(e, "議事録を読み込めませんでした")));
  }, [id, tick]);

  // 処理中のあいだは、ジョブの進み具合を出し、終わったら読み直す
  const processing = m?.status === "処理中";
  const { job } = useJob(processing ? m?.job_id ?? null : null, () => {
    setTick((t) => t + 1);
    onChanged();
  });

  return (
    <section className="card" aria-label="議事録">
      <div className="row-between">
        <h2>{m ? m.title : "議事録"}</h2>
        <button className="link" onClick={onBack}>一覧に戻る</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!m && !error && <p className="muted">読み込んでいます…</p>}
      {m && processing && (
        <>
          <JobProgress job={job} label="議事録づくり" />
          <p className="small muted">長い会議は時間がかかります。この画面を閉じても、処理は続きます。</p>
        </>
      )}
      {m?.status === "失敗" && (
        <>
          <p className="error" role="alert">{m.error || "議事録を作れませんでした"}</p>
          <p className="small muted">議事録を作れませんでした。直ったら、もう一度録画を送ってください。</p>
        </>
      )}
      {m?.status === "確認待ち" && <Review key={m.updated_at} m={m} saved={saved} onEdit={() => setSaved(false)} onSaved={(r) => { setM(r); setSaved(true); onChanged(); }} />}
    </section>
  );
}

function Review({ m, saved, onEdit, onSaved }: { m: Meeting; saved: boolean; onEdit: () => void; onSaved: (m: Meeting) => void }) {
  const [title, setTitle] = useState(m.title);
  const [day, setDay] = useState(m.date);
  const [place, setPlace] = useState(m.place);
  const [segs, setSegs] = useState<MeetingSegment[]>(m.segments);
  const base = m.summary ?? EMPTY_SUMMARY;
  const [summary, setSummary] = useState(base.summary);
  const [points, setPoints] = useState(base.points.join("\n"));
  const [decisions, setDecisions] = useState(base.decisions.join("\n"));
  const [todos, setTodos] = useState<Todo[]>(base.todos);
  const [next, setNext] = useState(base.next_meeting);
  const [dirty, setDirty] = useState(false);
  const [onlyUnknown, setOnlyUnknown] = useState(false);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [bulkName, setBulkName] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const change = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setDirty(true);
    setMessage("");
    onEdit();
  };
  const unknownIdx = useMemo(() => segs.flatMap((s, i) => (s.speaker === UNKNOWN || !s.speaker ? [i] : [])), [segs]);
  // 話し手の候補：読み取れた名前と出席者
  const names = useMemo(() => {
    const seen = new Set<string>();
    for (const s of segs) if (s.speaker && s.speaker !== UNKNOWN) seen.add(s.speaker);
    for (const p of m.participants) seen.add(p);
    return [...seen];
  }, [segs, m.participants]);

  const setSeg = (i: number, patch: Partial<MeetingSegment>) =>
    change(setSegs)(segs.map((s, j) => (j === i ? { ...s, ...patch } : s)));

  const togglePick = (i: number) =>
    setPicked((p) => {
      const n = new Set(p);
      if (n.has(i)) n.delete(i);
      else n.add(i);
      return n;
    });

  const applyBulk = () => {
    const name = bulkName.trim();
    if (!name || picked.size === 0) return;
    change(setSegs)(segs.map((s, i) => (picked.has(i) ? { ...s, speaker: name } : s)));
    setMessage(`${picked.size}件の話し手を「${name}」にしました。保存すると残ります。`);
    setPicked(new Set());
  };

  const save = async () => {
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const r = await api<Meeting>("PUT", `/api/meetings/${encodeURIComponent(m.meeting_id)}`, {
        title, date: day, place,
        segments: segs.map((s) => ({ speaker: s.speaker, text: s.text })),
        summary: { summary, points: rows(points), decisions: rows(decisions),
          todos: todos.filter((t) => t.what.trim()), next_meeting: next },
      });
      setDirty(false);
      onSaved(r);
    } catch (e) {
      setError(errorText(e, "保存できませんでした"));
    } finally {
      setSaving(false);
    }
  };

  const shown = segs.map((s, i) => ({ s, i })).filter(({ i }) => !onlyUnknown || unknownIdx.includes(i));

  return (
    <div className="form">
      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="r-title">会議名</label>
          <input id="r-title" type="text" maxLength={200} value={title} onChange={(e) => change(setTitle)(e.target.value)} />
        </div>
        <div className="form-row">
          <label htmlFor="r-date">開催日</label>
          <input id="r-date" type="date" value={day} onChange={(e) => change(setDay)(e.target.value)} />
        </div>
        <div className="form-row">
          <label htmlFor="r-place">場所</label>
          <input id="r-place" type="text" maxLength={200} value={place} onChange={(e) => change(setPlace)(e.target.value)} />
        </div>
      </div>
      <p className="small muted">録画：{m.media_name || "—"}　出席者：{m.attendees.join("、") || "—"}</p>

      <h3>要約</h3>
      <p className="small muted">社内の LLM の下書きです。録画と照らして直してください。</p>
      <div className="form-row">
        <label htmlFor="r-summary">要約</label>
        <textarea id="r-summary" rows={4} value={summary} onChange={(e) => change(setSummary)(e.target.value)} />
      </div>
      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="r-points">要点（1行に1つ）</label>
          <textarea id="r-points" rows={4} value={points} onChange={(e) => change(setPoints)(e.target.value)} />
        </div>
        <div className="form-row">
          <label htmlFor="r-decisions">決まったこと（1行に1つ）</label>
          <textarea id="r-decisions" rows={4} value={decisions} onChange={(e) => change(setDecisions)(e.target.value)} />
        </div>
      </div>
      <div className="form-row">
        <span className="label-like"><strong className="small">宿題</strong></span>
        <div className="table-wrap">
          <table className="table todos">
            <thead><tr><th>だれが</th><th>なにを</th><th>いつまでに</th><th></th></tr></thead>
            <tbody>
              {todos.map((t, i) => (
                <tr key={i}>
                  {(["who", "what", "due"] as const).map((k) => (
                    <td key={k}>
                      <input type="text" list="speaker-names" value={t[k]} aria-label={`宿題${i + 1}の${{ who: "だれが", what: "なにを", due: "いつまでに" }[k]}`}
                        onChange={(e) => change(setTodos)(todos.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)))} />
                    </td>
                  ))}
                  <td><button type="button" className="link" onClick={() => change(setTodos)(todos.filter((_, j) => j !== i))}>消す</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div><button type="button" onClick={() => change(setTodos)([...todos, { who: "", what: "", due: "" }])}>宿題を足す</button></div>
      </div>
      <div className="form-row">
        <label htmlFor="r-next">次回の予定</label>
        <input id="r-next" type="text" maxLength={200} value={next} onChange={(e) => change(setNext)(e.target.value)} />
      </div>

      <h3>発言と話し手</h3>
      {segs.length === 0 && (
        <p className="warn" role="note">⚠ 録画から発言を聞き取れませんでした。声が入っているか（音声が消えていないか）を確かめてください。</p>
      )}
      <datalist id="speaker-names">{names.map((n) => <option key={n} value={n} />)}</datalist>
      <div className="toolbar">
        <span>話し手が「不明」の発言：<strong>{unknownIdx.length}</strong>件</span>
        <label className="check">
          <input type="checkbox" checked={onlyUnknown} onChange={(e) => setOnlyUnknown(e.target.checked)} />
          「不明」だけを出す
        </label>
        <button type="button" onClick={() => setPicked(new Set(unknownIdx))} disabled={unknownIdx.length === 0}>「不明」をすべて選ぶ</button>
      </div>
      <div className="toolbar spaced">
        <label htmlFor="r-bulk">選んだ{picked.size}件の話し手を</label>
        <input id="r-bulk" type="text" list="speaker-names" maxLength={60} value={bulkName} onChange={(e) => setBulkName(e.target.value)} />
        <button type="button" onClick={applyBulk} disabled={picked.size === 0 || !bulkName.trim()}>にする</button>
      </div>
      <ol className="segments">
        {shown.map(({ s, i }) => (
          <li key={i} className={s.speaker === UNKNOWN ? "unknown" : undefined}>
            <label className="check small">
              <input type="checkbox" checked={picked.has(i)} onChange={() => togglePick(i)} aria-label={`${clock(s.start)} の発言を選ぶ`} />
              {clock(s.start)}
            </label>
            <input type="text" list="speaker-names" maxLength={60} value={s.speaker} aria-label={`${clock(s.start)} の話し手`}
              onChange={(e) => setSeg(i, { speaker: e.target.value })} />
            <textarea rows={2} maxLength={2000} value={s.text} aria-label={`${clock(s.start)} の発言`} onChange={(e) => setSeg(i, { text: e.target.value })} />
          </li>
        ))}
      </ol>

      <div className="sticky-actions">
        <button type="button" className="primary" disabled={saving || !dirty} onClick={() => void save()}>{saving ? "保存しています…" : "保存する"}</button>
        <a className="button-link" href={`/api/meetings/${encodeURIComponent(m.meeting_id)}/minutes.docx`} aria-disabled={dirty}
          onClick={(e) => dirty && e.preventDefault()}>議事録を Word で出す</a>
        {dirty && <span className="small muted">直したところを保存してから、Word で出してください。</span>}
        {saved && !dirty && <span role="status" className="small">保存しました。</span>}
        {message && <span role="status" className="small">{message}</span>}
        {error && <span role="alert" className="error small">{error}</span>}
      </div>
    </div>
  );
}
