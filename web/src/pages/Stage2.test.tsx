import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import Notifications from "./Notifications";
import Reception from "./Reception";
import Tickets from "./Tickets";
import Visits from "./Visits";
import VisitsAll from "./VisitsAll";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

afterEach(() => vi.unstubAllGlobals());

const GA = { ...ME_EMPLOYEE, employee_no: "10002", dept: "総務部", roles: ["社員", "総務"],
  permissions: [...ME_EMPLOYEE.permissions, "visits.all", "reception.records"] };
const IT = { ...ME_EMPLOYEE, employee_no: "10005", dept: "情報システム部", roles: ["社員", "情シス", "管理者"],
  permissions: [...ME_EMPLOYEE.permissions, "helpdesk.handle", "kiosk.devices", "jobs.all", "documents.manage"] };

const visit = (over: object = {}) => ({
  visit_id: 1, date: "2099-01-10", time: "15:00", room: "5階 第2会議室", host_employee_no: "10004", host_name: "高橋 美咲",
  host_dept: "営業部", host_contact: "", visitor_company: "ABC商事", visitor_name: "木村 誠", purpose: "", checked_in_at: null,
  cancelled: false, ...over,
});

describe("メニューの出し分け", () => {
  it("役割に応じてメニューを出し、ホームに未読のお知らせの件数を出す", async () => {
    fakeApi({ "GET /api/me": GA, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [{}, {}], unread: 2 } });
    render(<App />);
    expect(await screen.findByRole("link", { name: /全社の来訪予定/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /受付の記録/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /問い合わせ票/ })).not.toBeInTheDocument();
    expect(await screen.findByText("2")).toHaveClass("count");
  });

  it("情シスには問い合わせ票を出し、総務の画面は出さない", async () => {
    fakeApi({ "GET /api/me": IT, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    render(<App />);
    expect(await screen.findByRole("link", { name: /問い合わせ票/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /全社の来訪予定/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /受付の記録/ })).not.toBeInTheDocument();
  });

  it("権限のない画面の URL を直接開いても、中身を出さない", async () => {
    window.history.replaceState(null, "", "/tickets");
    const { calls } = fakeApi({ "GET /api/me": ME_EMPLOYEE });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/tickets"))).toBe(false);
  });
});

describe("S-11 お知らせ", () => {
  it("未読を出し、1件ずつ・すべて既読にできる", async () => {
    let read = new Set<number>();
    const items = () => [
      { notification_id: 1, created_at: "2026-11-16T10:00:00+09:00", kind: "来訪", message: "ABC商事 木村 誠様が受付に到着しました。", ref: "", read: read.has(1), to: "あなた" },
      { notification_id: 2, created_at: "2026-11-16T10:05:00+09:00", kind: "取次", message: "【取次】お客様：見積もりの件", ref: "", read: read.has(2), to: "営業部" },
    ];
    const { calls } = fakeApi({
      "GET /api/notifications?unread=1": () => ({ json: { notifications: items().filter((n) => !n.read), unread: 2 - read.size } }),
      "GET /api/notifications": () => ({ json: { notifications: items(), unread: 2 - read.size } }),
      "POST /api/notifications/read": (body: { ids: number[]; all?: boolean }) => {
        (body.all ? [1, 2] : body.ids).forEach((i) => read.add(i));
        return { json: { read: 1, unread: 2 - read.size } };
      },
    });
    render(<Notifications />);
    expect(await screen.findByText("未読 2 件")).toBeInTheDocument();
    expect(screen.getByText(/営業部あて/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "既読にする" })[0]);
    expect(await screen.findByText("未読 1 件")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ ids: [1] });
    await userEvent.click(screen.getByRole("button", { name: "すべて既読にする" }));
    expect(await screen.findByText("未読のお知らせはありません。")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("未読だけ"));
    expect(await screen.findAllByText("既読")).toHaveLength(2);
  });
});

describe("S-12 来訪予定（自分の）", () => {
  it("登録するとコードと案内文を1回だけ出し、閉じたらもう出ない", async () => {
    let list: object[] = [];
    const { calls } = fakeApi({
      "GET /api/visits": () => ({ json: { visits: list } }),
      "POST /api/visits": (body: object) => {
        list = [visit(body)];
        return { status: 201, json: { visit: visit(body), code: "HJ6P-W9XE", invitation: "ABC商事 木村 誠様\n…\nインビテーションコード：HJ6P-W9XE" } };
      },
    });
    render(<Visits />);
    expect(await screen.findByText("来訪予定はありません。")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("日付"), { target: { value: "2099-01-10" } });
    fireEvent.change(screen.getByLabelText("時刻"), { target: { value: "15:00" } });
    await userEvent.type(screen.getByLabelText("会議室"), "5階 第2会議室");
    await userEvent.type(screen.getByLabelText("お客様の会社名"), "ABC商事");
    await userEvent.type(screen.getByLabelText("お客様のお名前"), "木村 誠");
    await userEvent.click(screen.getByRole("button", { name: "登録してコードを作る" }));

    const box = await screen.findByRole("region", { name: "インビテーションコード" });
    expect(within(box).getByText("HJ6P-W9XE")).toBeInTheDocument();
    expect(within(box).getByText("コードはこのときだけ表示します。")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ date: "2099-01-10", time: "15:00", visitor_company: "ABC商事" });
    // 一覧にはコードを出さない
    const table = await screen.findByRole("table");
    expect(within(table).getByText(/ABC商事 木村 誠様/)).toBeInTheDocument();
    expect(within(table).queryByText(/HJ6P/)).not.toBeInTheDocument();
    await userEvent.click(within(box).getByRole("button", { name: "閉じる" }));
    expect(screen.queryByText("HJ6P-W9XE")).not.toBeInTheDocument();
  });

  it("取り消しは、画面の上でもう一度押して確かめる", async () => {
    let cancelled = false;
    const { calls } = fakeApi({
      "GET /api/visits": () => ({ json: { visits: [visit({ cancelled })] } }),
      "DELETE /api/visits/1": () => {
        cancelled = true;
        return { json: { ok: true } };
      },
    });
    render(<Visits />);
    await userEvent.click(await screen.findByRole("button", { name: /の予定を取り消す/ }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "取り消す" }));
    expect(await screen.findByText("取り消し")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(1);
  });
});

describe("S-22 全社の来訪予定", () => {
  it("日付ごとの一覧と、取り込みの結果（コードはこのときだけ）を出す", async () => {
    const { calls } = fakeApi({
      "GET /api/visits?date=2099-01-10": { visits: [visit(), visit({ visit_id: 2, host_name: "山田 太郎", cancelled: true })] },
      "POST /api/visits/import": { kind: "visits", errors: [], added: [{ visit: visit({ visit_id: 3 }), code: "MRKT-7Q4A" }] },
    });
    render(<VisitsAll />);
    fireEvent.change(screen.getByLabelText("日付"), { target: { value: "2099-01-10" } });
    expect(await screen.findByText("予定 1 件（受付済み 0 件）")).toBeInTheDocument();
    expect(screen.getByText("山田 太郎")).toBeInTheDocument();

    // 「ファイルを選ぶ」の文字は画面に1つだけ（読み上げ用の名前は別）
    expect(screen.getAllByText("ファイルを選ぶ")).toHaveLength(1);
    const file = new File(["date,time,company,visitor,host_employee_no,room\n"], "visits.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByLabelText(/取り込むファイルを選ぶ/), file);
    expect(await screen.findByText("MRKT-7Q4A")).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST");
    expect(post?.headers["Content-Type"]).toBe("text/csv");
    expect(post?.body).toContain("host_employee_no");
    expect(post?.headers["X-Yanua-Client"]).toBeTruthy();
  });
});

describe("S-23 受付の記録", () => {
  it("未対応の記録を出し、対応済みにできる", async () => {
    let done = false;
    const rec = () => ({
      record_id: "RC-20261116-0001", created_at: "2026-11-16T10:00:00+09:00", channel: "電話", category: "取次", route: "総務部",
      priority: "中", purpose: "佐藤さんにお約束", visitor: { company: "A社", name: "B" }, contact: "03-0000-0000", details: "",
      host: null, candidates: ["10001", "10006"], resolved: false, status: done ? "対応済み" : "未対応", note: "",
      status_updated_at: done ? "2026-11-16T11:00:00+09:00" : null, status_updated_by: done ? "10002" : null,
    });
    fakeApi({
      "GET /api/reception/records?status=%E6%9C%AA%E5%AF%BE%E5%BF%9C": () => ({ json: { records: done ? [] : [rec()] } }),
      "GET /api/reception/records": () => ({ json: { records: [rec()] } }),
      "POST /api/reception/records/RC-20261116-0001/done": () => {
        done = true;
        return { json: rec() };
      },
    });
    render(<Reception />);
    expect(await screen.findByText(/A社 B：佐藤さんにお約束/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "詳しい内容" }));
    expect(screen.getByText(/同じ姓の社員がいます/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "対応済みにする" }));
    expect(await screen.findByText("未対応の記録はありません。")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "すべて" }));
    expect(await screen.findByText("対応済み")).toBeInTheDocument();
  });
});

describe("S-30 問い合わせ票", () => {
  it("優先度で絞り込み、中身を開いて状態とメモを保存できる", async () => {
    const t = { ticket_id: "HD-20261116-0001", created_at: "2026-11-16T10:00:00+09:00", category: "印刷", priority: "高",
      problem: "複合機が止まっている", requester: { employee_no: "10004", name: "高橋 美咲", dept: "営業部" }, status: "未対応",
      note: "", status_updated_at: null, status_updated_by: null };
    const { calls } = fakeApi({
      "GET /api/tickets": { tickets: [t] },
      "GET /api/tickets?priority=%E9%AB%98": { tickets: [t] },
      "GET /api/tickets/HD-20261116-0001": { ...t, detail: { problem: "複合機が止まっている", contact: "内線4100", tried: ["電源を入れ直す"] } },
      "PATCH /api/tickets/HD-20261116-0001": (body: { status: string; note: string }) => ({
        json: { ...t, ...body, status_updated_by: "10005", status_updated_at: "2026-11-16T11:00:00+09:00", detail: {} } }),
    });
    render(<Tickets />);
    await userEvent.selectOptions(await screen.findByLabelText("優先度"), "高");
    await waitFor(() => expect(calls.some((c) => c.path === "/api/tickets?priority=%E9%AB%98")).toBe(true));
    await userEvent.click(await screen.findByRole("button", { name: "HD-20261116-0001" }));
    const detail = await screen.findByRole("complementary", { name: "問い合わせ票の中身" });
    expect(await within(detail).findByText("内線4100")).toBeInTheDocument();
    expect(within(detail).getByText("電源を入れ直す")).toBeInTheDocument();
    await userEvent.selectOptions(within(detail).getByLabelText("状態"), "対応中");
    await userEvent.type(within(detail).getByLabelText("対応のメモ"), "現地で確認");
    await userEvent.click(within(detail).getByRole("button", { name: "保存する" }));
    expect(await within(detail).findByText("保存しました。")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ status: "対応中", note: "現地で確認" });
  });
});
