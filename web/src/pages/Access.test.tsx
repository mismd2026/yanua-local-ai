import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import AccessPage, { SELF_LOSS_WARNING } from "./Access";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

beforeEach(() => window.history.replaceState(null, "", "/"));
afterEach(() => vi.unstubAllGlobals());

const ADMIN = { ...ME_EMPLOYEE, employee_no: "10005", name: "田中 健", dept: "情報システム部", roles: ["社員", "管理者"],
  permissions: [...ME_EMPLOYEE.permissions, "roles.view", "roles.assign", "audit.view"] };

const grant = (id: number, group: string, role: string, target: string, over: object = {}) => ({
  grant_id: id, group_id: group, role_id: `R-${role}`, target_id: `t-${target}`, set_id: null, role, target, target_kind: "special", ...over,
});
const VIEW = {
  permissions: [
    { name: "ask", feature: "チャット", does: "社内文書に質問する", targets: ["機能"] },
    { name: "audit.view", feature: "監査", does: "監査ログを見る（S-55）", targets: ["機能"] },
    { name: "roles.view", feature: "権限", does: "グループ・役割・対象を見る（S-54）", targets: ["機能"] },
  ],
  roles: [
    { role_id: "R-社員", name: "社員", description: "全員が持つ", permissions: ["ask"], used_by: ["社員"] },
    { role_id: "R-監査", name: "監査", description: "監査ログ", permissions: ["audit.view", "roles.view"], used_by: ["監査"] },
  ],
  groups: [
    { group_id: "G-employees", name: "社員", description: "ログインした人の全員", rule: { all: true }, how: "rule", rule_text: "全員",
      rule_count: 6, member_count: 6, members: [], grants: [grant(1, "G-employees", "社員", "メンバー自身の部署")] },
    { group_id: "G-audit", name: "監査", description: "監査ログを見る人", rule: null, how: "manual", rule_text: "手で入れる",
      rule_count: 0, member_count: 1, grants: [grant(2, "G-audit", "監査", "全社")],
      members: [{ group_id: "G-audit", account_id: "10001", expires_at: "2026-12-31", added_by: "10005", added_at: "", name: "山田 太郎",
        in_hr: true, expired: false }] },
  ],
  targets: [
    { target_id: "special:all", kind: "special", name: "全社", ref: "", description: "", host: "", secret_ref: "", kind_name: "特別",
      auto: true, used: true, stale: false },
    { target_id: "dept:営業部", kind: "department", name: "営業部", ref: "営業部", description: "", host: "", secret_ref: "", kind_name: "部署",
      auto: true, used: false, stale: false },
    { target_id: "system:ab12", kind: "system", name: "本番の AWS", ref: "system:ab12", description: "読み取りだけ", host: "huggingface.co",
      secret_ref: "AWS_READONLY_KEY", kind_name: "システム", auto: false, used: false, stale: false },
  ],
  target_sets: [],
  accounts: [{ account_id: "sys:kiosk", kind: "system", name: "受付端末" }, { account_id: "sys:update-check", kind: "system", name: "更新の確認（S-53）" }],
  departments: ["営業部", "総務部"],
  hosts: ["huggingface.co", "registry.ollama.ai"],
};
const NO_CHANGE = { refused: null, problems: [], self: { gained: [], lost: [] }, self_loss: false, people: [], checked: 0, too_many: false, groups: [] };

describe("S-54 権限", () => {
  it("roles.view を持つ人のメニューにだけ出し、持たない人が URL を開いても読まない", async () => {
    fakeApi({ "GET /api/me": ADMIN, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("link", { name: /^権限/ })).toBeInTheDocument();
    unmount();

    window.history.replaceState(null, "", "/admin/access");
    const { calls } = fakeApi({ "GET /api/me": ME_EMPLOYEE });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/admin/access"))).toBe(false);
  });

  it("グループの一覧と中身。決まりで入るグループには、メンバーの出し入れのボタンを出さない", async () => {
    fakeApi({ "GET /api/admin/access": VIEW });
    const user = userEvent.setup();
    render(<AccessPage canEdit />);
    const list = await screen.findByRole("region", { name: "グループの一覧" });
    const rows = within(list).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("社員決まり（全員）6人社員 × メンバー自身の部署");
    expect(rows[2]).toHaveTextContent("監査手で入れる1人監査 × 全社");
    const everyone = screen.getByRole("region", { name: "グループ「社員」" });
    expect(everyone).toHaveTextContent("決まり（全員）で入るグループです。在籍している 6人 が入ります");
    expect(within(everyone).queryByLabelText(/足す人/)).toBeNull();
    expect(within(everyone).queryByRole("button", { name: "このグループを消す" })).toBeDisabled();

    await user.click(within(list).getByRole("button", { name: "監査" }));
    const audit = screen.getByRole("region", { name: "グループ「監査」" });
    expect(within(audit).getByRole("table", { name: "手で入れたメンバー" })).toHaveTextContent("山田 太郎（10001）2026-12-31");
    expect(within(audit).getByLabelText(/足す人/)).toBeInTheDocument();
  });

  it("メンバーを足す前に、だれのどの権限が増えるかを出し、保存は2回目に押したとき", async () => {
    const preview = { ...NO_CHANGE, detail: "監査 に 高橋 美咲 を追加（期限 2026-12-01）",
      people: [{ account_id: "10004", name: "高橋 美咲", dept: "営業部", gained: ["audit.view × 全社（期限 2026-12-01）"], lost: [] }] };
    const { calls } = fakeApi({
      "GET /api/admin/access": VIEW,
      "GET /api/admin/access/people?q=%E9%AB%98%E6%A9%8B": { people: [{ account_id: "10004", name: "高橋 美咲", dept: "営業部", kind: "person" }] },
      "POST /api/admin/access/preview": preview,
      "POST /api/admin/access/groups/G-audit/members": () => ({ status: 201, json: { ...VIEW, ok: true, detail: preview.detail } }),
    });
    const user = userEvent.setup();
    render(<AccessPage canEdit />);
    await user.click(within(await screen.findByRole("region", { name: "グループの一覧" })).getByRole("button", { name: "監査" }));
    const audit = screen.getByRole("region", { name: "グループ「監査」" });
    await user.type(within(audit).getByLabelText(/足す人/), "高橋");
    await user.click(within(audit).getByRole("button", { name: "探す" }));
    await user.click(await within(audit).findByLabelText(/高橋 美咲（10004・営業部）/));
    await user.type(within(audit).getByLabelText(/期限/), "2026-12-01");
    await user.click(within(audit).getByRole("button", { name: "高橋 美咲 を足す" }));

    const box = await within(audit).findByRole("region", { name: "変更の確かめ" });
    expect(box).toHaveTextContent("この変更：監査 に 高橋 美咲 を追加（期限 2026-12-01）");
    expect(within(box).getByRole("table", { name: "権限が変わる人" })).toHaveTextContent("高橋 美咲（10004・営業部）audit.view × 全社（期限 2026-12-01）");
    expect(calls.find((c) => c.path === "/api/admin/access/preview")?.body).toEqual(
      { op: "member.add", args: { group_id: "G-audit", account_id: "10004", expires_at: "2026-12-01" } });
    expect(calls.some((c) => c.path.endsWith("/members"))).toBe(false);          // 試しに当てただけ。まだ保存していない

    await user.click(within(box).getByRole("button", { name: "保存する" }));
    expect(await screen.findByRole("status")).toHaveTextContent("保存しました：監査 に 高橋 美咲 を追加");
    const saved = calls.find((c) => c.method === "POST" && c.path === "/api/admin/access/groups/G-audit/members");
    expect(saved?.body).toEqual({ account_id: "10004", expires_at: "2026-12-01" });
  });

  it("自分の権限が減る変更は「あなた自身では戻せません」と出して、もう一度押させる", async () => {
    const preview = { ...NO_CHANGE, detail: "社員 から 役割「社員」× メンバー自身の部署 を外した",
      self: { gained: [], lost: ["ask × メンバー自身の部署"] }, self_loss: true,
      people: [{ account_id: "10005", name: "田中 健", dept: "情報システム部", gained: [], lost: ["ask × メンバー自身の部署"] }] };
    const { calls } = fakeApi({
      "GET /api/admin/access": VIEW,
      "POST /api/admin/access/preview": preview,
      "DELETE /api/admin/access/grants/1": { ...VIEW, ok: true, detail: preview.detail },
    });
    const user = userEvent.setup();
    render(<AccessPage canEdit />);
    const everyone = await screen.findByRole("region", { name: "グループ「社員」" });
    await user.click(within(everyone).getByRole("button", { name: "外す" }));
    const box = await within(everyone).findByRole("region", { name: "変更の確かめ" });
    expect(within(box).getByRole("alert")).toHaveTextContent(SELF_LOSS_WARNING);
    expect(within(box).getByRole("alert")).toHaveTextContent("あなた自身の減る権限：ask × メンバー自身の部署");
    expect(within(box).queryByRole("button", { name: "保存する" })).toBeNull();
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await user.click(within(box).getByRole("button", { name: "戻せないことをわかったうえで保存する" }));
    expect(await screen.findByRole("status")).toHaveTextContent("保存しました");
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual(["/api/admin/access/grants/1"]);
  });

  it("断られる変更は、理由を欄の近くに出し、保存のボタンを出さない", async () => {
    const { calls } = fakeApi({
      "GET /api/admin/access": VIEW,
      "POST /api/admin/access/preview": { ...NO_CHANGE, detail: null, refused: "自分の権限が増える変更はできません（増える権限：audit.view）",
        problems: [{ where: "", message: "自分の権限が増える変更です。ほかの roles.assign を持つ人に頼んでください" }] },
    });
    const user = userEvent.setup();
    render(<AccessPage canEdit />);
    await user.click(within(await screen.findByRole("region", { name: "グループの一覧" })).getByRole("button", { name: "監査" }));
    const audit = screen.getByRole("region", { name: "グループ「監査」" });
    await user.selectOptions(within(audit).getByLabelText("役割"), "R-社員");
    await user.selectOptions(within(audit).getByLabelText("対象"), "t:special:all");
    await user.click(within(audit).getByRole("button", { name: "与える" }));
    expect(await within(audit).findByRole("alert", { name: "変えられない理由" })).toHaveTextContent("自分の権限が増える変更はできません（増える権限：audit.view）");
    expect(within(audit).queryByRole("button", { name: /保存する/ })).toBeNull();
    expect(calls.find((c) => c.path === "/api/admin/access/preview")?.body).toEqual(
      { op: "grant.add", args: { group_id: "G-audit", role_id: "R-社員", target_id: "special:all", set_id: null } });
    expect(calls.some((c) => c.path.endsWith("/grants"))).toBe(false);
  });

  it("見るだけの人（roles.view だけ）には、変えるボタンを出さない", async () => {
    fakeApi({ "GET /api/admin/access": VIEW });
    const user = userEvent.setup();
    render(<AccessPage />);
    await screen.findByRole("region", { name: "グループの一覧" });
    expect(screen.queryByRole("button", { name: "外す" })).toBeNull();
    expect(screen.queryByRole("region", { name: "グループを作る" })).toBeNull();
    await user.click(screen.getByRole("tab", { name: "役割" }));
    const role = screen.getByRole("region", { name: "役割「社員」" });
    expect(within(role).getByRole("group", { name: "チャット" })).toHaveTextContent("ask 社内文書に質問する（効く対象：機能）");
    expect(within(role).getByRole("checkbox", { name: /ask/ })).toBeChecked();
    expect(within(role).getByRole("checkbox", { name: /ask/ })).toBeDisabled();
  });

  it("対象：自動の対象とシステム。接続情報は参照名だけを出す", async () => {
    const { calls } = fakeApi({
      "GET /api/admin/access": VIEW,
      "POST /api/admin/access/preview": { ...NO_CHANGE, detail: "サーバー「社内 DB」を作った" },
      "POST /api/admin/access/targets": () => ({ status: 201, json: { ...VIEW, ok: true, detail: "サーバー「社内 DB」を作った" } }),
    });
    const user = userEvent.setup();
    render(<AccessPage canEdit />);
    await user.click(await screen.findByRole("tab", { name: "対象" }));
    const list = screen.getByRole("region", { name: "対象の一覧" });
    expect(within(list).getAllByRole("row")[2]).toHaveTextContent("部署営業部自動");
    expect(within(list).getAllByRole("row")[3]).toHaveTextContent("システム本番の AWS 読み取りだけ手huggingface.coAWS_READONLY_KEY");
    const form = screen.getByRole("region", { name: "システム・サーバーを作る" });
    await user.selectOptions(within(form).getByLabelText("種類"), "server");
    await user.type(within(form).getByLabelText("名前"), "社内 DB");
    await user.selectOptions(within(form).getByLabelText(/つながる宛先/), "registry.ollama.ai");
    await user.type(within(form).getByLabelText(/接続情報の参照名/), "DB_READONLY");
    await user.click(within(form).getByRole("button", { name: "作る" }));
    await user.click(await within(form).findByRole("button", { name: "保存する" }));
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/admin/access/targets")?.body).toEqual(
      { kind: "server", name: "社内 DB", description: "", host: "registry.ollama.ai", secret_ref: "DB_READONLY" });
  });

  it("この人は何ができるか：権限ごとに、グループ・役割・対象・期限を出す。システムアカウントも選べる", async () => {
    const result = { account_id: "10001", name: "山田 太郎", dept: "経理部", kind: "person", groups: ["社員", "監査"],
      permissions: [{ permission: "audit.view", feature: "監査", does: "監査ログを見る（S-55）",
        from: [{ group: "監査", role: "監査", target: "全社", via: "手で入れた", expires_at: "2026-12-31", reason: "" }] }] };
    const { calls } = fakeApi({
      "GET /api/admin/access": VIEW,
      "GET /api/admin/access/explain?q=%E5%B1%B1%E7%94%B0": { q: "山田", candidates: [result], result },
      "GET /api/admin/access/explain?q=sys%3Akiosk": { q: "sys:kiosk", candidates: [], result: { account_id: "sys:kiosk", name: "受付端末",
        dept: "", kind: "system", groups: [], permissions: [] } },
    });
    const user = userEvent.setup();
    render(<AccessPage />);
    await user.click(await screen.findByRole("tab", { name: "この人は何ができるか" }));
    await user.type(screen.getByLabelText("社員番号か名前"), "山田");
    await user.click(screen.getByRole("button", { name: "調べる" }));
    const table = await screen.findByRole("table", { name: "持っている権限" });
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("audit.view監査：監査ログを見る（S-55）監査（手で入れた）監査全社2026-12-31");
    await user.click(screen.getByRole("button", { name: "受付端末" }));
    expect(await screen.findByText("権限はありません。")).toBeInTheDocument();
    expect(calls.map((c) => c.path)).toContain("/api/admin/access/explain?q=sys%3Akiosk");
  });
});
