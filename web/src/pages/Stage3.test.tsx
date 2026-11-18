import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import Meetings from "./Meetings";
import PaperConvert from "./PaperConvert";
import PaperSort from "./PaperSort";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

beforeEach(() => window.history.replaceState(null, "", "/"));
afterEach(() => vi.unstubAllGlobals());

const EMP = { ...ME_EMPLOYEE, permissions: [...ME_EMPLOYEE.permissions, "meetings"] };
const GA = { ...EMP, employee_no: "10002", dept: "総務部", roles: ["社員", "総務"], permissions: [...EMP.permissions, "paper", "visits.all"] };

const job = (over: object = {}) => ({
  job_id: "j1", kind: "paper-plan", status: "完了", progress: 100, stage: "完了", result: null, error: null,
  created_at: "2026-11-16T10:00:00+09:00", ...over,
});

const INBOX = { files: [
  { name: "scan_0001.pdf", size: 12000, modified: "2026-11-16T09:00:00+09:00" },
  { name: "scan_0005.pdf", size: 9000, modified: "2026-11-16T09:01:00+09:00" },
] };
const RULES = { rules: [{ name: "請求書", templates: ["xlsx"] }, { name: "見積書", templates: [] }] };

const item = (over: object = {}) => ({
  index: 0, file: "scan_0001.pdf", rule: "請求書", by: "rule", fields: { date: "20261110", company: "株式会社サンプル印刷", amount: "132000" },
  dest: "経理/請求書/2026/11/20261110_請求書_株式会社サンプル印刷_132000円.pdf", ocr: false, error: null, ...over,
});

describe("メニュー", () => {
  it("総務には PDF の仕分け・変換と議事録を出す", async () => {
    fakeApi({ "GET /api/me": GA, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    render(<App />);
    expect(await screen.findByRole("link", { name: /PDF の仕分け/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /PDF の変換/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /議事録/ })).toBeInTheDocument();
  });

  it("社員には議事録だけを出し、PDF の画面は URL を開いても中身を出さない", async () => {
    fakeApi({ "GET /api/me": EMP, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    render(<App />);
    expect(await screen.findByRole("link", { name: /議事録/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /PDF の/ })).not.toBeInTheDocument();
  });

  it("PDF の仕分けの URL を社員が開いても、受信箱を読まない", async () => {
    window.history.replaceState(null, "", "/paper/sort");
    const { calls } = fakeApi({ "GET /api/me": EMP });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/paper"))).toBe(false);
  });
});

describe("S-20 PDF の仕分け", () => {
  it("予定を作り、ルールを選び直し、確かめてから実行する", async () => {
    let items = [item(), item({ index: 1, file: "scan_0005.pdf", rule: "", by: "none", fields: {}, dest: "未分類/scan_0005.pdf" })];
    let executed = false;
    const { calls } = fakeApi({
      "GET /api/paper/inbox": INBOX,
      "GET /api/paper/rules": RULES,
      "GET /api/paper/history?limit=100": () => ({ json: { records: executed ? [{ time: "2026-11-16T10:05:00+09:00", source: "scan_0001.pdf", dest: items[0].dest, rule: "請求書", by: "rule", ocr: "", date: "", company: "", amount: "" }] : [] } }),
      "POST /api/paper/plans": job({ status: "待ち", progress: 0 }),
      "GET /api/jobs/j1": job({ result: { plan_id: "j1", count: 2, errors: 0 } }),
      "GET /api/paper/plans/j1": () => ({ json: { plan_id: "j1", created_at: "2026-11-16T10:00:00+09:00", items, executed_at: null, result: null } }),
      "PUT /api/paper/plans/j1/items/1": (body: { rule: string }) => {
        items = [items[0], item({ index: 1, file: "scan_0005.pdf", rule: body.rule, by: "forced", fields: {}, dest: "総務/見積書/scan_0005.pdf" })];
        return { json: items[1] };
      },
      "POST /api/paper/plans/j1/execute": () => {
        executed = true;
        return { json: { placed: [items[0].dest, items[1].dest], skipped: [], moved: false } };
      },
    });
    const user = userEvent.setup();
    render(<PaperSort />);
    expect(await screen.findByText("まだ記録はありません。")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "2件の仕分けの予定を作る" }));
    // すべて選んでいるときは、空で送る（受信箱のすべて）
    expect(calls.find((c) => c.path === "/api/paper/plans")?.body).toEqual({ files: [], use_llm: false });

    expect(await screen.findByText(items[0].dest)).toBeInTheDocument();
    expect(window.location.search).toBe("?plan=j1");
    expect(screen.getByText("相手先：株式会社サンプル印刷")).toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "scan_0005.pdf のルール" }), "見積書");
    expect(await screen.findByText("総務/見積書/scan_0005.pdf")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ rule: "見積書" });

    // 実行は確かめてから。押しただけでは送らない
    await user.click(screen.getByRole("button", { name: "実行する" }));
    expect(calls.some((c) => c.path.endsWith("/execute"))).toBe(false);
    expect(screen.getByText("2件を保存先にコピーします。よろしいですか？")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "はい、実行する" }));
    expect(await screen.findByText("2件をコピーしました。")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/execute"))?.body).toEqual({ move: false });
    // 実行したあとは、ルールを選び直せない。記録に出る
    expect(screen.queryByRole("combobox", { name: /のルール/ })).not.toBeInTheDocument();
    const history = screen.getByRole("heading", { name: "仕分けの記録" }).closest("section")!;
    expect(await within(history as HTMLElement).findByRole("cell", { name: /^scan_0001.pdf/ })).toBeInTheDocument();
  });

  it("選んだファイルだけで予定を作る", async () => {
    const { calls } = fakeApi({
      "GET /api/paper/inbox": INBOX, "GET /api/paper/history?limit=100": { records: [] },
      "POST /api/paper/plans": job({ status: "待ち" }), "GET /api/jobs/j1": job({ status: "失敗", error: "仕分けルールを読めません" }),
    });
    const user = userEvent.setup();
    render(<PaperSort />);
    await user.click(await screen.findByRole("checkbox", { name: "scan_0001.pdf を選ぶ" }));
    await user.click(screen.getByRole("button", { name: "1件の仕分けの予定を作る" }));
    expect(calls.find((c) => c.path === "/api/paper/plans")?.body).toEqual({ files: ["scan_0005.pdf"], use_llm: false });
    expect(await screen.findByText("仕分けルールを読めません")).toBeInTheDocument();
  });
});

describe("S-20 読み取れなかった項目と、同じ置き先", () => {
  it("「不明」が入る行と、同じ置き先の行に注意を出し、番号を付けて置いたものを出す", async () => {
    const dest = "総務/見積書/不明/不明_見積書_不明_不明円.pdf";
    const items = [0, 1].map((i) => item({ index: i, file: `scan_${i}.pdf`, rule: "見積書", by: "forced", fields: {}, dest, missing: ["date", "company", "amount"] }));
    window.history.replaceState(null, "", "/paper/sort?plan=p1");
    fakeApi({
      "GET /api/paper/rules": RULES, "GET /api/paper/history?limit=100": { records: [] },
      "GET /api/paper/plans/p1": { plan_id: "p1", created_at: "2026-11-16T10:00:00+09:00", items, executed_at: null, result: null },
      "POST /api/paper/plans/p1/execute": { placed: [dest, dest.replace(".pdf", "_2.pdf")], skipped: [], moved: false,
        renamed: [{ file: "scan_1.pdf", planned: dest, placed: dest.replace(".pdf", "_2.pdf") }] },
    });
    const user = userEvent.setup();
    render(<PaperSort />);
    expect(await screen.findByText("⚠ 注意がある行が2件あります（表の「置く先」を見てください）。")).toBeInTheDocument();
    expect(screen.getAllByText(/読み取れなかった項目（日付・相手先・金額）は「不明」になります/)).toHaveLength(2);
    expect(screen.getAllByText(/同じ置き先がほかにもあります。実行すると、上書きせずに番号/)).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "実行する" }));
    await user.click(screen.getByRole("button", { name: "はい、実行する" }));
    expect(await screen.findByText(/番号を付けて置いたもの（1件）/)).toBeInTheDocument();
    expect(screen.getByText(`scan_1.pdf → ${dest.replace(".pdf", "_2.pdf")}`)).toBeInTheDocument();
  });
});

describe("S-21 PDF の変換", () => {
  it("PDF を送って会社の様式に変換し、結果をダウンロードできる", async () => {
    const { calls } = fakeApi({
      "GET /api/paper/inbox": INBOX, "GET /api/paper/rules": RULES,
      "POST /api/uploads?kind=pdf&name=%E8%AB%8B%E6%B1%82%E6%9B%B8.pdf": () => ({ status: 201, json: { file_id: "u1", kind: "pdf", name: "請求書.pdf", size: 5, created_at: "", expires_at: "" } }),
      "POST /api/paper/convert": job({ job_id: "j2", kind: "paper-convert", status: "待ち" }),
      "GET /api/jobs/j2": job({ job_id: "j2", kind: "paper-convert", result: {
        files: [{ file_id: "r1", kind: "result", name: "請求書.xlsx", size: 8000, created_at: "", expires_at: "2026-11-17T10:00:00+00:00" }],
        message: "「請求書」の様式（請求書.xlsx）に差し込みました。" } }),
    });
    const user = userEvent.setup();
    render(<PaperConvert />);
    await user.upload(await screen.findByLabelText("変換する PDF"), new File(["%PDF-"], "請求書.pdf", { type: "application/pdf" }));
    await user.selectOptions(screen.getByLabelText("変換先"), "xlsx");
    await user.click(screen.getByRole("checkbox", { name: "会社の様式に差し込む" }));
    // 様式があるルールだけを選べる
    const rule = screen.getByRole("combobox", { name: "様式を使うルール" });
    expect(within(rule).queryByText("見積書の様式")).not.toBeInTheDocument();
    await user.selectOptions(rule, "請求書");
    await user.click(screen.getByRole("button", { name: "変換する" }));

    const link = await screen.findByRole("link", { name: "請求書.xlsx" });
    expect(link).toHaveAttribute("href", "/api/files/r1");
    expect(screen.getByText(/様式（請求書.xlsx）に差し込みました/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/paper/convert")?.body).toEqual({ to: "xlsx", form: true, rule: "請求書", file_id: "u1" });
    expect(calls.find((c) => c.path.startsWith("/api/uploads"))?.headers["X-Yanua-Client"]).toBe("web");
  });

  it("作業用の場所がいっぱいのときは、サーバーの文を出す", async () => {
    fakeApi({
      "GET /api/paper/inbox": INBOX, "GET /api/paper/rules": RULES,
      "POST /api/uploads?kind=pdf&name=a.pdf": () => ({ status: 507, json: { error: { code: "work_full", message: "サーバーの作業用の場所がいっぱいです。" } } }),
    });
    const user = userEvent.setup();
    render(<PaperConvert />);
    await user.upload(await screen.findByLabelText("変換する PDF"), new File(["%PDF-"], "a.pdf", { type: "application/pdf" }));
    await user.click(screen.getByRole("button", { name: "変換する" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("作業用の場所がいっぱいです");
  });
});

const meeting = (over: object = {}) => ({
  meeting_id: "m1", job_id: "j3", title: "工事の打ち合わせ", date: "2026-11-16", status: "確認待ち", created_at: "2026-11-16T10:00:00+09:00",
  updated_at: "2026-11-16T10:10:00+09:00", place: "3階 会議室", tool: "zoom", participants: [], error: null, media_name: "会議.mp4",
  segments: [
    { start: 0, end: 3, text: "それでは始めます。", speaker: "佐藤 一郎" },
    { start: 3, end: 6, text: "工事は土曜日に行うことにしました。", speaker: "不明" },
    { start: 6, end: 9, text: "承知しました。", speaker: "不明" },
  ],
  summary: { summary: "工事の日程を決めた。", points: ["工事の日程"], decisions: ["工事は土曜日"], todos: [{ who: "佐藤 一郎", what: "業者に連絡", due: "11/20" }], next_meeting: "" },
  unknown_speakers: 2, attendees: ["佐藤 一郎"], summary_note: null, ...over,
});

describe("S-13 議事録", () => {
  it("録画を送って議事録づくりを始め、終わったら確認の画面にする", async () => {
    let status = "処理中";
    const { calls } = fakeApi({
      "GET /api/meetings": { meetings: [] },
      "POST /api/uploads?kind=recording&name=%E4%BC%9A%E8%AD%B0.mp4": () => ({ status: 201, json: { file_id: "rec1", kind: "recording", name: "会議.mp4", size: 3, created_at: "", expires_at: "" } }),
      "POST /api/meetings": () => ({ status: 202, json: { ...job({ job_id: "j3", kind: "meeting", status: "待ち" }), meeting_id: "m1" } }),
      "GET /api/meetings/m1": () => ({ json: meeting({ status, segments: status === "処理中" ? [] : meeting().segments }) }),
      "GET /api/jobs/j3": () => {
        status = "確認待ち";
        return { json: job({ job_id: "j3", kind: "meeting" }) };
      },
    });
    const user = userEvent.setup();
    render(<Meetings />);
    await user.upload(screen.getByLabelText("録画・録音（2GB まで）"), new File(["abc"], "会議.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("会議名"), "工事の打ち合わせ");
    await user.type(screen.getByLabelText(/出席者/), "佐藤 一郎、高橋 美咲");
    await user.click(screen.getByRole("button", { name: "議事録を作る" }));

    expect(await screen.findByText("それでは始めます。")).toBeInTheDocument();
    const body = calls.find((c) => c.path === "/api/meetings" && c.method === "POST")?.body;
    expect(body).toMatchObject({ upload_id: "rec1", segments_upload_id: null, title: "工事の打ち合わせ", tool: "zoom",
      participants: ["佐藤 一郎", "高橋 美咲"], summary: true });
    expect(window.location.search).toBe("?id=m1");
  });

  it("「不明」の話し手をまとめて直して保存する。保存するまでは Word を出せない", async () => {
    window.history.replaceState(null, "", "/meetings?id=m1");
    const { calls } = fakeApi({
      "GET /api/meetings": { meetings: [] },
      "GET /api/meetings/m1": meeting(),
      "PUT /api/meetings/m1": (body: { segments: { speaker: string }[] }) => ({
        json: meeting({ updated_at: "2026-11-16T10:20:00+09:00", unknown_speakers: 0,
          segments: meeting().segments.map((s, i) => ({ ...s, speaker: body.segments[i].speaker })) }),
      }),
    });
    const user = userEvent.setup();
    render(<Meetings />);
    expect(await screen.findByText(/話し手が「不明」の発言/)).toHaveTextContent("2件");
    const word = screen.getByRole("link", { name: "議事録を Word で出す" });
    expect(word).toHaveAttribute("href", "/api/meetings/m1/minutes.docx");
    expect(word).toHaveAttribute("aria-disabled", "false");

    await user.click(screen.getByRole("button", { name: "「不明」をすべて選ぶ" }));
    await user.type(screen.getByLabelText(/選んだ2件の話し手を/), "高橋 美咲");
    await user.click(screen.getByRole("button", { name: "にする" }));
    expect(screen.getByText(/話し手が「不明」の発言/)).toHaveTextContent("0件");
    expect(word).toHaveAttribute("aria-disabled", "true");

    await user.click(screen.getByRole("button", { name: "保存する" }));
    expect(await screen.findByText("保存しました。")).toBeInTheDocument();
    const put = calls.find((c) => c.method === "PUT")?.body;
    expect(put.segments.map((s: { speaker: string }) => s.speaker)).toEqual(["佐藤 一郎", "高橋 美咲", "高橋 美咲"]);
    expect(put.summary).toMatchObject({ decisions: ["工事は土曜日"], todos: [{ who: "佐藤 一郎", what: "業者に連絡", due: "11/20" }] });
    await waitFor(() => expect(screen.getByRole("link", { name: "議事録を Word で出す" })).toHaveAttribute("aria-disabled", "false"));
  });

  it("要約が空のときは注意を出し、書き始めると消える。出席者を直して保存できる", async () => {
    window.history.replaceState(null, "", "/meetings?id=m1");
    const note = "要約を作れませんでした。録画と照らして書いてください";
    const { calls } = fakeApi({
      "GET /api/meetings": { meetings: [] },
      "GET /api/meetings/m1": meeting({ summary: { summary: "", points: [], decisions: [], todos: [], next_meeting: "" }, summary_note: note }),
      "PUT /api/meetings/m1": (body: { attendees: string[] }) => ({ json: meeting({ updated_at: "2026-11-18T10:00:00+09:00", attendees: body.attendees }) }),
    });
    const user = userEvent.setup();
    render(<Meetings />);
    expect(await screen.findByText(`⚠ ${note}`)).toBeInTheDocument();
    await user.type(screen.getByLabelText("要約"), "工事の日程を決めた。");
    expect(screen.queryByText(`⚠ ${note}`)).not.toBeInTheDocument();

    const people = screen.getByLabelText("出席者（1行に1人）");
    expect(people).toHaveValue("佐藤 一郎");
    await user.type(people, "{enter}高橋 美咲");
    await user.click(screen.getByRole("button", { name: "保存する" }));
    expect(await screen.findByText("保存しました。")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body.attendees).toEqual(["佐藤 一郎", "高橋 美咲"]);
  });

  it("失敗したときは、サーバーの文だけを1回出す", async () => {
    window.history.replaceState(null, "", "/meetings?id=m1");
    const shown = "文字起こしのモデルが準備されていません。管理者に連絡してください";
    fakeApi({ "GET /api/meetings": { meetings: [] }, "GET /api/meetings/m1": meeting({ status: "失敗", error: shown, segments: [], summary: null }) });
    render(<Meetings />);
    expect(await screen.findByRole("alert")).toHaveTextContent(new RegExp(`^${shown}$`));
    expect(screen.queryByText(/models pull/)).not.toBeInTheDocument();
  });

  it("ほかの人の議事録の URL は、ないものと同じに出す", async () => {
    window.history.replaceState(null, "", "/meetings?id=other");
    fakeApi({ "GET /api/meetings": { meetings: [] } });
    render(<Meetings />);
    expect(await screen.findByRole("alert")).toHaveTextContent("見つかりません");
  });
});

describe("K-047 仕分けルールの下書き・保存・前の版", () => {
  const draft = { name: "案内", keywords: { any: ["案内"], all: [], none: [] }, fields: {}, filename: "{date}_案内_{company}.pdf", folder: "案内/{yyyy}/{mm}", priority: 0 };
  const planRoutes = (items: object[]) => ({
    "GET /api/paper/rules": RULES, "GET /api/paper/history?limit=100": { records: [] },
    "GET /api/paper/plans/p1": { plan_id: "p1", created_at: "2026-11-18T10:00:00+09:00", items, executed_at: null, result: null },
  });

  it("未分類の行から下書きを作り、誤りを項目の横に出し、直して保存し、予定を作り直す", async () => {
    window.history.replaceState(null, "", "/paper/sort?plan=p1");
    const unsorted = item({ index: 0, file: "scan_0005.pdf", rule: "", by: "none", fields: {}, dest: "未分類/scan_0005.pdf" });
    let saves = 0;
    const { calls } = fakeApi({
      ...planRoutes([unsorted]),
      "GET /api/paper/rules/history": { versions: [], current: null },
      "POST /api/paper/rule-drafts": job({ job_id: "d1", kind: "paper-rule-draft", status: "待ち" }),
      "GET /api/jobs/d1": job({ job_id: "d1", kind: "paper-rule-draft", result: { draft, source: "scan_0005.pdf", excerpt: "会社案内のお知らせ", text: "会社案内のお知らせ" } }),
      "POST /api/paper/rules/validate": { ok: true, warnings: [{ where: "filename", message: "{date} は、この PDF からは読み取れませんでした" }] },
      "POST /api/paper/rules": (body: { rule: { filename: string } }) => {
        saves += 1;
        if (body.rule.filename.includes("{nothing}"))
          return { status: 400, json: { error: { code: "invalid_rule", message: "ルールに直すところがあります",
            details: [{ where: "filename", message: "{nothing} という項目はありません" }] } } };
        return { status: 201, json: { version: 2, rules: [...RULES.rules, { name: "案内", templates: [] }] } };
      },
      "POST /api/paper/plans": job({ job_id: "j9", status: "待ち" }),
      "GET /api/jobs/j9": job({ job_id: "j9", result: { plan_id: "p2", count: 1, errors: 0 } }),
      "GET /api/paper/plans/p2": { plan_id: "p2", created_at: "2026-11-18T10:05:00+09:00", items: [{ ...unsorted, rule: "案内", by: "rule", dest: "案内/2026/11/x.pdf" }], executed_at: null, result: null },
    });
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const user = userEvent.setup();
    render(<PaperSort canRules />);
    await user.click(await screen.findByRole("button", { name: "scan_0005.pdf からルールの下書きを作る" }));
    // 開いた下書きの欄まで、画面を動かす
    expect(scroll).toHaveBeenCalled();
    expect(scroll.mock.contexts[0]).toBe(screen.getByRole("region", { name: "ルールの下書き" }));
    await user.click(screen.getByRole("button", { name: "下書きを作る" }));
    expect(calls.find((c) => c.path === "/api/paper/rule-drafts")?.body).toEqual({ plan_id: "p1", index: 0, name: null, use_llm: false });

    const filename = await screen.findByLabelText("ファイル名");
    expect(screen.getByLabelText("ルールの名前（書類の種類）")).toHaveValue("案内");
    await user.clear(filename);
    await user.type(filename, "{{date}_{{nothing}.pdf");
    await user.click(screen.getByRole("button", { name: "保存する" }));
    expect(await screen.findByText("{nothing} という項目はありません")).toBeInTheDocument();
    expect(screen.getByText("ルールに直すところがあります（1件）")).toBeInTheDocument();

    await user.clear(filename);
    await user.type(filename, "{{date}_案内.pdf");
    // 確かめると、PDF から読み取れない項目に注意を出す（誤りではないので保存はできる）
    await user.click(screen.getByRole("button", { name: "確かめる" }));
    expect(await screen.findByText(/{date} は、この PDF からは読み取れませんでした/)).toBeInTheDocument();
    expect(screen.getByText("誤りはありません。注意を見てから保存してください。")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/paper/rules/validate")?.body.sample_text).toBe("会社案内のお知らせ");
    await user.click(screen.getByRole("button", { name: "保存する" }));
    expect(await screen.findByText(/ルールを保存しました（版 2）/)).toBeInTheDocument();
    expect(saves).toBe(2);
    expect(calls.filter((c) => c.path === "/api/paper/rules" && c.method === "POST").pop()?.body.rule).toMatchObject({ name: "案内", filename: "{date}_案内.pdf", keywords: { any: ["案内"] } });

    // 保存したルールを、予定を作り直して使う
    await user.click(screen.getByRole("button", { name: "この予定のファイルで、予定を作り直す" }));
    expect(calls.find((c) => c.path === "/api/paper/plans" && c.method === "POST")?.body).toEqual({ files: ["scan_0005.pdf"], use_llm: false });
    expect(await screen.findByText("案内/2026/11/x.pdf")).toBeInTheDocument();
    expect(window.location.search).toBe("?plan=p2");
  });

  it("前の版に、確かめてから戻す", async () => {
    window.history.replaceState(null, "", "/paper/sort?plan=p1");
    let current = 3;
    const { calls } = fakeApi({
      ...planRoutes([item()]),
      "GET /api/paper/rules/history": () => ({ json: { current, versions: [
        { version: 3, saved_at: "2026-11-18T10:00:00+09:00", saved_by: "10002", saved_by_name: "佐藤 花子", action: "ルール「案内」を足した" },
        { version: 2, saved_at: "2026-11-18T09:00:00+09:00", saved_by: "（ファイル）", saved_by_name: "（ファイル）", action: "保存の前の中身（ファイルの版）" },
      ] } }),
      "POST /api/paper/rules/history/2/restore": () => {
        current = 4;
        return { json: { version: 4, rules: RULES.rules } };
      },
    });
    const user = userEvent.setup();
    render(<PaperSort canRules />);
    expect(await screen.findByText("ルール「案内」を足した")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "版 3 に戻す" })).not.toBeInTheDocument();   // 今の版には戻せない
    await user.click(screen.getByRole("button", { name: "版 2 に戻す" }));
    expect(calls.some((c) => c.path.endsWith("/restore"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "はい、戻す" }));
    expect(await screen.findByText("版 2 の中身に戻しました（新しい版 4）。")).toBeInTheDocument();
    // 保存のときと同じく、作り直しの知らせとボタンを出す
    expect(screen.getByText(/ルールを版 2 の中身に戻しました（新しい版 4）。予定を作り直すと、新しいルールが使われます。/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "この予定のファイルで、予定を作り直す" })).toBeInTheDocument();
  });

  it("画面で保存したあとに既定のルールが更新されたら知らせ、確かめてから既定に戻す", async () => {
    window.history.replaceState(null, "", "/paper/sort?plan=p1");
    let using: "saved" | "default" = "saved";
    const status = () => ({ using, default_changed: using === "saved", current_version: using === "saved" ? 2 : 3, default_updated_at: "2026-11-18T12:00:00+09:00" });
    const { calls } = fakeApi({
      ...planRoutes([item()]),
      "GET /api/paper/rules/status": () => ({ json: status() }),
      "GET /api/paper/rules/history": () => ({ json: { current: status().current_version, status: status(), versions: [
        { version: 2, saved_at: "2026-11-18T10:00:00+09:00", saved_by: "10002", saved_by_name: "佐藤 花子", action: "ルール「案内」を足した" },
        { version: 1, saved_at: "2026-11-18T10:00:00+09:00", saved_by: "（既定）", saved_by_name: "（既定）", action: "保存の前の中身（既定）" },
      ] } }),
      "POST /api/paper/rules/reset": () => {
        using = "default";
        return { json: { version: 3, rules: RULES.rules } };
      },
    });
    const user = userEvent.setup();
    render(<PaperSort canRules />);
    expect(await screen.findByText(/既定のルールが更新されています/)).toBeInTheDocument();
    expect(await screen.findByText("画面で保存したもの")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "既定に戻す" }));
    expect(calls.some((c) => c.path === "/api/paper/rules/reset")).toBe(false);
    await user.click(screen.getByRole("button", { name: "はい、既定に戻す" }));
    expect(await screen.findByText(/既定のルールに戻しました（版 3）/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/既定のルールが更新されています/)).not.toBeInTheDocument());
    expect(screen.getByText("既定")).toBeInTheDocument();
    expect(screen.getByText(/ルールを既定に戻しました（版 3）。予定を作り直すと、新しいルールが使われます。/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "この予定のファイルで、予定を作り直す" })).toBeInTheDocument();
  });

  it("予定のあとに使ったルールがなくなっていたら、実行を止めて作り直しを促す", async () => {
    window.history.replaceState(null, "", "/paper/sort?plan=p1");
    fakeApi({
      ...planRoutes([item({ rule: "案内", dest: "案内/2026/11/x.pdf" })]),
      "POST /api/paper/plans/p1/execute": () => ({ status: 409, json: { error: { code: "rules_gone",
        message: "予定を作ったあとに、使ったルールがなくなりました。予定を作り直してから実行してください",
        details: [{ where: "scan_0001.pdf", message: "ルール「案内」は今のルールにありません" }] } } }),
    });
    const user = userEvent.setup();
    render(<PaperSort />);
    expect(screen.queryByRole("button", { name: "この予定のファイルで、予定を作り直す" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "実行する" }));
    await user.click(screen.getByRole("button", { name: "はい、実行する" }));
    expect(await screen.findByText(/使ったルールがなくなりました/)).toBeInTheDocument();
    expect(screen.getByText("scan_0001.pdf：ルール「案内」は今のルールにありません")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "この予定のファイルで、予定を作り直す" })).toBeInTheDocument();
  });

  it("ルールの権限がなければ、下書きのボタンと版を出さない", async () => {
    window.history.replaceState(null, "", "/paper/sort?plan=p1");
    const { calls } = fakeApi(planRoutes([item({ rule: "", by: "none" })]));
    render(<PaperSort />);
    expect(await screen.findByText(item().dest)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ルールの下書き/ })).not.toBeInTheDocument();
    expect(calls.some((c) => c.path.includes("/rules/history"))).toBe(false);
  });
});
