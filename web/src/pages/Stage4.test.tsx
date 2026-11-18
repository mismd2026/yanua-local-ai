import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import Audit from "./Audit";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

beforeEach(() => window.history.replaceState(null, "", "/"));
afterEach(() => vi.unstubAllGlobals());

const ADMIN = { ...ME_EMPLOYEE, employee_no: "10005", name: "田中 健", dept: "情報システム部", roles: ["社員", "管理者"],
  permissions: [...ME_EMPLOYEE.permissions, "audit.view"] };

const entry = (id: number, over: object = {}) => ({
  id, at: "2026-11-18T01:00:00+00:00", actor: "10004", actor_name: "高橋 美咲", actor_kind: "社員", action: "auth.login",
  target: "10004", result: "成功", ip: "192.168.1.20", detail: "開発用ログイン", body: null, ...over,
});
const page = (entries: object[], over: object = {}) => ({
  entries, total: entries.length, page: 1, per_page: 50, actions: ["access.denied", "audit.view", "auth.login", "kiosk.locked"],
  keep_days: 365, keep_body: false, ...over,
});

describe("S-55 監査ログ", () => {
  it("管理者のメニューにだけ出し、社員が URL を開いても読まない", async () => {
    fakeApi({ "GET /api/me": ADMIN, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("link", { name: /監査ログ/ })).toBeInTheDocument();
    unmount();

    window.history.replaceState(null, "", "/admin/audit");
    const { calls } = fakeApi({ "GET /api/me": ME_EMPLOYEE });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/audit"))).toBe(false);
  });

  it("新しい順に出し、期間・した人・操作の種類・結果で絞り込み、ページを送る", async () => {
    const first = page([
      entry(3, { action: "kiosk.locked", actor: "KD-1A2B", actor_name: "受付1", actor_kind: "受付端末", result: "拒否", ip: "",
        detail: "コードを1分に10回間違えたので、しばらく照合を止めました" }),
      entry(2, { action: "access.denied", result: "拒否", target: "GET /api/audit", detail: "操作「audit.view」の権限がありません" }),
      entry(1, { actor: "なし", actor_name: "なし", actor_kind: "なし", result: "失敗", target: "99999" }),
    ], { total: 120 });
    const { calls } = fakeApi({
      "GET /api/audit?page=1&per_page=50": first,
      "GET /api/audit?page=2&per_page=50": page([entry(9)], { total: 120, page: 2 }),
      "GET /api/audit?page=1&per_page=50&since=2026-11-01&until=2026-11-18&actor=%E9%AB%98%E6%A9%8B&action=auth&result=%E5%A4%B1%E6%95%97":
        page([entry(1, { result: "失敗" })]),
    });
    const user = userEvent.setup();
    render(<Audit />);
    const list = await screen.findByRole("region", { name: "監査ログの一覧" });
    const rows = await within(list).findAllByRole("row");
    expect(rows[1]).toHaveTextContent("受付1");
    expect(rows[1]).toHaveTextContent("受付端末");
    expect(rows[1]).toHaveTextContent("拒否");
    expect(rows[2]).toHaveTextContent("高橋 美咲（10004）");
    expect(rows[2]).toHaveTextContent("操作「audit.view」の権限がありません");
    expect(rows[3]).toHaveTextContent("なし");
    expect(screen.getByRole("status")).toHaveTextContent("120件（1〜50件目）");
    expect(screen.getByText(/365日を過ぎた記録は自動で消え/)).toBeInTheDocument();
    expect(screen.getByText(/本文や文書の中身は残していません/)).toBeInTheDocument();
    // 消す・書き換えるボタンはない
    expect(screen.queryByRole("button", { name: /削除|書き換え|記録を消す/ })).not.toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "次のページ" })[0]);
    expect(await screen.findByText("120件（51〜100件目）")).toBeInTheDocument();
    expect(screen.getAllByText("2 / 3 ページ")).toHaveLength(2);
    expect(calls.some((c) => c.path === "/api/audit?page=2&per_page=50")).toBe(true);

    fireEvent.change(screen.getByLabelText("期間の始め"), { target: { value: "2026-11-01" } });
    fireEvent.change(screen.getByLabelText("期間の終わり（その日まで）"), { target: { value: "2026-11-18" } });
    await user.type(screen.getByLabelText("した人（社員番号・名前の一部・端末）"), "高橋");
    // 操作の種類は、記録にある頭（auth.…）でも、名前そのものでも選べる
    const kinds = screen.getByLabelText("操作の種類");
    expect(within(kinds).getByRole("option", { name: "ログイン・ログアウト（auth.…）" })).toBeInTheDocument();
    expect(within(kinds).getByRole("option", { name: "kiosk.locked" })).toBeInTheDocument();
    expect(within(kinds).queryByRole("option", { name: /ジョブ/ })).not.toBeInTheDocument();   // 記録にない種類は出さない
    await user.selectOptions(kinds, "auth");
    await user.selectOptions(screen.getByLabelText("結果"), "失敗");
    await user.click(screen.getByRole("button", { name: "絞り込む" }));
    expect(await screen.findByText("1件", { exact: false })).toBeInTheDocument();
    expect(calls.at(-1)?.path).toContain("since=2026-11-01&until=2026-11-18");
  });

  it("期間の始めが終わりより後なら、読み込まずに知らせる", async () => {
    const { calls } = fakeApi({ "GET /api/audit?page=1&per_page=50": page([]) });
    const user = userEvent.setup();
    render(<Audit />);
    expect(await screen.findByText("当てはまる記録はありません。")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("期間の始め"), { target: { value: "2026-11-18" } });
    fireEvent.change(screen.getByLabelText("期間の終わり（その日まで）"), { target: { value: "2026-11-01" } });
    await user.click(screen.getByRole("button", { name: "絞り込む" }));
    expect(screen.getByRole("alert")).toHaveTextContent("期間の始めは、終わりより前の日にしてください");
    expect(calls.filter((c) => c.path.startsWith("/api/audit"))).toHaveLength(1);
  });

  it("権限がなくて断られたら、サーバーの文を出す", async () => {
    fakeApi({ "GET /api/audit?page=1&per_page=50": () => ({ status: 403, json: { error: { code: "forbidden", message: "この操作をする権限がありません" } } }) });
    render(<Audit />);
    expect(await screen.findByRole("alert")).toHaveTextContent("この操作をする権限がありません");
  });
});
