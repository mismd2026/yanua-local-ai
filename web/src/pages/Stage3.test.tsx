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
