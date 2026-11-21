import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import HrPage from "./Hr";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

beforeEach(() => window.history.replaceState(null, "", "/"));
afterEach(() => vi.unstubAllGlobals());

const HR_PERSON = { ...ME_EMPLOYEE, employee_no: "10006", name: "伊藤 直子", dept: "人事部", roles: ["社員", "人事"],
  permissions: [...ME_EMPLOYEE.permissions, "hr.view", "hr.import"] };
const MAPPING = {
  columns: { employee_no: "職員コード", name: "漢字氏名", dept: "所属名", status: "在職区分" },
  status_values: { 在職: "在籍", 休業: "休職", 退社: "退職" },
};
const STATUS = {
  last: { imported_at: "2026-11-20T01:00:00+00:00", source: "hr.csv", active: 6, inactive: 1, age_days: 0 },
  history: [{ imported_at: "2026-11-20T01:00:00+00:00", source: "hr.csv", kind: "取り込み", by: "10006", added: 7, changed: 0, removed: 0, scope: "一部のファイル" }],
  backups: [{ backup_id: 1, saved_at: "2026-11-20T01:00:00+00:00", source: "（前の取り込み）", total: 0, active: 0 }],
  mappings: { うちの人事アプリ: MAPPING },
  problems: [],
  fields: { employee_no: "従業員番号", name: "氏名", kana: "よみ", dept: "部署", dept_code: "部署番号", status: "在籍の状態", email: "メールアドレス" },
  required: ["employee_no", "name", "dept"],
  statuses: ["在籍", "休職", "退職"],
  leave: { login: true, permissions: "そのまま" },
  can_import: true, can_view: true, keep: 3,
};
const HEADERS = ["職員コード", "漢字氏名", "所属名", "在職区分", "住所", "給与"];
const INSPECT = { headers: HEADERS, sheets: [], draft: { employee_no: "職員コード", name: "漢字氏名", dept: "所属名", status: "在職区分" }, status_values: [] };
const VALUES = { ...INSPECT, status_values: [{ value: "在職", guess: "在籍" }, { value: "休業", guess: "休職" }, { value: "退社", guess: "退職" }] };
const PREVIEW = {
  source: "hr.csv", rows: 7, errors: [], error_count: 0, read_columns: ["職員コード", "漢字氏名", "所属名", "在職区分"],
  ignored_columns: ["住所", "給与"], counts: { 在籍: 5, 休職: 1, 退職: 1 },
  added: { count: 0, first: [] },
  changed: { count: 1, first: [{ employee_no: "10004", name: "高橋 美咲", dept: "総務部", status: "在籍", changes: ["部署 営業部→総務部"] }], by_field: { 部署: 1 } },
  removed: { count: 0, first: [] },
  groups: [{ group_id: "G-general", name: "総務", join: 1, leave: 0 }], lockout: false, ok: true, retired_members: 0,
  scope: "partial", scope_label: "一部のファイル", current_total: 7, removal_warning: null,
};
const job = (id: string, result: object) => ({ job_id: id, kind: "hr", status: "完了", progress: 100, stage: "完了", result, created_at: "" });
const queued = (id: string) => () => ({ status: 202, json: { job_id: id, kind: "hr", status: "待ち", progress: 0, stage: "", created_at: "" } });
const UPLOAD = { "POST /api/uploads?kind=hr&name=hr.csv": () => ({ status: 201, json: { file_id: "f1", kind: "hr", name: "hr.csv", size: 10, created_at: "", expires_at: "" } }) };
const inspect = (body: { status_column: string | null }) => ({ json: body.status_column ? VALUES : INSPECT });

async function pickFile(user: ReturnType<typeof userEvent.setup>) {
  const form = await screen.findByRole("region", { name: "人事データの取り込み" });
  await user.upload(within(form).getByLabelText(/ファイル/), new File(["x"], "hr.csv", { type: "text/csv" }));
  await within(form).findByRole("table", { name: "列の対応づけ" });
  return form;
}

describe("S-51 人事データ", () => {
  it("hr.import か hr.view を持つ人のメニューにだけ出し、持たない人が URL を開いても読まない", async () => {
    fakeApi({ "GET /api/me": HR_PERSON, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("link", { name: /^人事データ/ })).toBeInTheDocument();
    unmount();
    window.history.replaceState(null, "", "/admin/hr");
    const { calls } = fakeApi({ "GET /api/me": ME_EMPLOYEE });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/hr"))).toBe(false);
  });

  it("上げたファイルに合う保存した対応づけを選び、在籍状態の対応を出す。試して、確かめてから確定する", async () => {
    const { calls } = fakeApi({
      "GET /api/hr/status": STATUS, ...UPLOAD, "POST /api/hr/inspect": inspect,
      "POST /api/hr/preview": queued("p1"), "GET /api/jobs/p1": job("p1", PREVIEW),
      "POST /api/hr/imports": queued("c1"), "GET /api/jobs/c1": job("c1", { ...PREVIEW, committed: true }),
    });
    const user = userEvent.setup();
    render(<HrPage />);
    const form = await pickFile(user);
    expect(within(form).getByLabelText("保存した対応づけ")).toHaveValue("うちの人事アプリ");
    expect(within(form).getByLabelText("部署")).toHaveValue("所属名");
    expect(within(form).getByText("読まない列：住所・給与")).toBeInTheDocument();
    expect(await within(form).findByLabelText("在籍状態「休業」")).toHaveValue("休職");
    await user.click(within(form).getByRole("button", { name: "試す（保存しない）" }));
    const box = await within(form).findByRole("region", { name: "試しの結果" });
    expect(box).toHaveTextContent("在籍 5人・休職 1人・退職 1人");
    expect(box).toHaveTextContent("取り込みの種類：一部のファイル（ファイルにいない人は変えません）");
    expect(box).toHaveTextContent("読まなかった列：住所・給与");
    expect(within(box).getByRole("list", { name: "変わる人" })).toHaveTextContent("変わる人：1人 （先頭：10004 高橋 美咲［部署 営業部→総務部］）");
    expect(within(box).getByRole("table", { name: "グループごとの人数" })).toHaveTextContent("総務1人0人");
    expect(calls.find((c) => c.path === "/api/hr/preview")?.body).toEqual({ file_id: "f1", scope: "partial", mapping: MAPPING });
    expect(calls.some((c) => c.path === "/api/hr/imports")).toBe(false);
    await user.click(within(box).getByRole("button", { name: "確定する" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("確定しました：入る 0人・変わる 1人・いなくなる 0人");
    expect(calls.find((c) => c.path === "/api/hr/imports")?.body).toEqual({ file_id: "f1", scope: "partial", mapping: MAPPING, confirm_removal: false });
  });

  it("誤りは行番号つきで出し、確定できない。試したあとに対応づけを変えたら、もう一度試すまで確定できない", async () => {
    const bad = { ...PREVIEW, ok: false, error_count: 2, errors: [{ line: 5, message: "在籍状態「出向」を、在籍・休職・退職のどれに当てるか決めていません" },
      { line: 9, message: "社員番号 10003 が重複しています（4行目と同じ）" }] };
    let n = 0;
    fakeApi({
      "GET /api/hr/status": STATUS, ...UPLOAD, "POST /api/hr/inspect": inspect,
      "POST /api/hr/preview": () => queued(`p${++n}`)(),
      "GET /api/jobs/p1": job("p1", bad), "GET /api/jobs/p2": job("p2", PREVIEW),
    });
    const user = userEvent.setup();
    render(<HrPage />);
    const form = await pickFile(user);
    await user.click(within(form).getByRole("button", { name: "試す（保存しない）" }));
    const box = await within(form).findByRole("region", { name: "試しの結果" });
    expect(within(box).getByRole("list", { name: "誤り" })).toHaveTextContent("5行目：在籍状態「出向」を");
    expect(within(box).getByRole("list", { name: "誤り" })).toHaveTextContent("9行目：社員番号 10003 が重複しています（4行目と同じ）");
    expect(within(box).getByRole("button", { name: "確定する" })).toBeDisabled();
    await user.click(within(form).getByRole("button", { name: "試す（保存しない）" }));
    await waitFor(() => expect(within(form).getByRole("button", { name: "確定する" })).toBeEnabled());
    await user.selectOptions(within(form).getByLabelText("よみ"), "住所");
    expect(within(form).getByRole("button", { name: "確定する" })).toBeDisabled();
    expect(within(form).getByText("対応づけかファイルを変えたので、もう一度試してから確定してください。")).toBeInTheDocument();
  });

  it("全員のファイルで、いなくなる人が10%を超えるときは、出してからもう一度押させる", async () => {
    const many = { ...PREVIEW, scope: "all", scope_label: "全員のファイル", removed: { count: 4, first: [] },
      removal_warning: { count: 4, percent: 57.1, message: "いなくなる人が 4人（全体の 57.1%）います" } };
    const { calls } = fakeApi({
      "GET /api/hr/status": STATUS, ...UPLOAD, "POST /api/hr/inspect": inspect,
      "POST /api/hr/preview": queued("p1"), "GET /api/jobs/p1": job("p1", many),
      "POST /api/hr/imports": queued("c1"), "GET /api/jobs/c1": job("c1", { ...many, committed: true }),
    });
    const user = userEvent.setup();
    render(<HrPage />);
    const form = await pickFile(user);
    await user.click(within(form).getByLabelText("全員のファイル（ファイルにいない人は、いなくなる人として扱う）"));
    await user.click(within(form).getByRole("button", { name: "試す（保存しない）" }));
    const box = await within(form).findByRole("region", { name: "試しの結果" });
    expect(box).toHaveTextContent("取り込みの種類：全員のファイル");
    expect(calls.find((c) => c.path === "/api/hr/preview")?.body.scope).toBe("all");
    await user.click(within(box).getByRole("button", { name: "確定する" }));
    expect(within(box).getByRole("alert")).toHaveTextContent("いなくなる人が 4人（全体の 57.1%）います（今の 7人のうち）。よければ、もう一度押してください。");
    expect(calls.some((c) => c.path === "/api/hr/imports")).toBe(false);
    await user.click(within(box).getByRole("button", { name: "確定する" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("確定しました");
    expect(calls.find((c) => c.path === "/api/hr/imports")?.body).toMatchObject({ scope: "all", confirm_removal: true });
  });

  it("対応づけに名前を付けて保存する", async () => {
    const { calls } = fakeApi({
      "GET /api/hr/status": { ...STATUS, mappings: {} }, ...UPLOAD, "POST /api/hr/inspect": inspect,
      "PUT /api/hr/mappings/%E6%96%B0%E3%81%97%E3%81%84%E4%BA%BA%E4%BA%8B": { ok: true, version: 2, mappings: {} },
    });
    const user = userEvent.setup();
    render(<HrPage />);
    const form = await pickFile(user);
    expect(within(form).getByLabelText("保存した対応づけ")).toHaveValue("");
    await within(form).findByLabelText("在籍状態「退社」");
    await user.type(within(form).getByLabelText("この対応づけに名前を付けて保存"), "新しい人事");
    await user.click(within(form).getByRole("button", { name: "保存する" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("対応づけ「新しい人事」を保存しました");
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual(MAPPING);
  });

  it("1つ前に戻す：まず試しの結果を出し、もう一度押して戻す", async () => {
    const { calls } = fakeApi({
      "GET /api/hr/status": STATUS,
      "POST /api/hr/restore/preview": queued("r1"), "GET /api/jobs/r1": job("r1", PREVIEW),
      "POST /api/hr/restore": queued("r2"), "GET /api/jobs/r2": job("r2", { ...PREVIEW, committed: true, kind: "戻す" }),
    });
    const user = userEvent.setup();
    render(<HrPage />);
    const area = await screen.findByRole("region", { name: "前の取り込みに戻す" });
    expect(within(area).getByRole("table", { name: "取り込みの記録" })).toHaveTextContent("取り込み一部のファイルhr.csv100067人・0人・0人");
    await user.click(within(area).getByRole("button", { name: "1つ前に戻す（まず試す）" }));
    const box = await within(area).findByRole("region", { name: "戻す前の試し" });
    expect(calls.some((c) => c.path === "/api/hr/restore")).toBe(false);
    await user.click(within(box).getByRole("button", { name: "戻す" }));
    expect(await within(area).findByRole("status")).toHaveTextContent("1つ前の取り込みに戻しました");
  });

  it("一覧：hr.view だけの人には取り込みを出さない。番号・名前・部署で絞り込む", async () => {
    const list = { employees: [{ employee_no: "10003", name: "鈴木 一郎", dept: "営業部", status: "在籍", changed_at: "2026-11-20" }],
      total: 1, departments: ["営業部"] };
    const { calls } = fakeApi({
      "GET /api/hr/status": { ...STATUS, can_import: false, mappings: {}, history: [], backups: [] },
      "GET /api/hr/employees?q=&dept=&offset=0": list,
      "GET /api/hr/employees?q=&dept=%E5%96%B6%E6%A5%AD%E9%83%A8&offset=0": list,
    });
    const user = userEvent.setup();
    render(<HrPage />);
    const area = await screen.findByRole("region", { name: "社員の一覧" });
    expect(screen.queryByRole("region", { name: "人事データの取り込み" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "取り込み" })).toBeNull();
    expect(await within(area).findByRole("table")).toHaveTextContent("10003鈴木 一郎営業部在籍2026-11-20");
    await user.selectOptions(within(area).getByLabelText("部署"), "営業部");
    await waitFor(() => expect(calls.some((c) => c.path.includes("dept=%E5%96%B6"))).toBe(true));
  });

  it("人事データの注意（読めない対応づけなど）を画面の上に出す", async () => {
    fakeApi({ "GET /api/hr/status": { ...STATUS, problems: [{ where: "対応づけ", message: "JSON として読めません" }] } });
    render(<HrPage />);
    expect(await screen.findByRole("alert", { name: "人事データの注意" })).toHaveTextContent("対応づけ：JSON として読めません");
  });
});
