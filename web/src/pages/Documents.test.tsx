import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import DocumentsPage from "./Documents";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

beforeEach(() => window.history.replaceState(null, "", "/"));
afterEach(() => vi.unstubAllGlobals());

const ADMIN = { ...ME_EMPLOYEE, employee_no: "10005", name: "田中 健", dept: "情報システム部", roles: ["社員", "管理者"],
  permissions: [...ME_EMPLOYEE.permissions, "documents.manage"] };

const doc = (id: string, title: string, owner: string, targets: [string, string][], over: object = {}) => ({
  doc_id: id, title, path: `${id}.md`, updated: "", owner, required: "documents.view", external: false, indexed_at: "2026-11-19T01:00:00+00:00",
  source: "/x", removed_at: null, removed_by: "", targets: targets.map((t) => t[0]), target_names: targets.map((t) => t[1]),
  decided_by: "file", ...over,
});
const VIEW = {
  documents: [
    doc("A01", "就業規則（抜粋）", "総務部", [["special:all", "全社"]]),
    doc("K01", "月次決算の手順", "経理部", [["dept:経理部", "経理部"]]),
    doc("P01", "会社案内", "総務部", [["special:all", "全社"]], { external: true }),
  ],
  removed: false,
  publish_targets: [
    { target_id: "special:all", kind: "全社", name: "全社" },
    { target_id: "dept:経理部", kind: "部署", name: "経理部" },
    { target_id: "dept:総務部", kind: "部署", name: "総務部" },
    { target_id: "group:G-hr", kind: "グループ", name: "人事" },
  ],
  permissions: [{ name: "documents.view", does: "文書を見る" }, { name: "infra.docs.view", does: "構成書を見る" }],
  owners: ["経理部", "総務部"],
};

describe("S-50 文書", () => {
  it("documents.manage を持つ人のメニューにだけ出し、持たない人が URL を開いても読まない", async () => {
    fakeApi({ "GET /api/me": ADMIN, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("link", { name: /^文書/ })).toBeInTheDocument();
    unmount();
    window.history.replaceState(null, "", "/admin/documents");
    const { calls } = fakeApi({ "GET /api/me": ME_EMPLOYEE });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/admin/documents"))).toBe(false);
  });

  it("一覧：題名・持ち主・公開先・要る権限・取り込んだ日。持ち主と公開先で絞り込む", async () => {
    fakeApi({ "GET /api/admin/documents": VIEW, "GET /api/documents/ingest/sources": { sources: [] } });
    const user = userEvent.setup();
    render(<DocumentsPage />);
    const list = await screen.findByRole("region", { name: "文書の一覧" });
    const rows = within(list).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("就業規則（抜粋）（A01）総務部全社documents.view2026-11-19");
    expect(rows[3]).toHaveTextContent("会社案内（P01）総務部全社 ＋社外公開");
    await user.selectOptions(within(list).getByLabelText("持ち主の部署"), "経理部");
    expect(within(list).getAllByRole("row")).toHaveLength(2);
    await user.selectOptions(within(list).getByLabelText("持ち主の部署"), "");
    await user.selectOptions(within(list).getByLabelText("公開先"), "全社");
    expect(within(list).getAllByRole("row").map((r) => r.textContent)).toEqual(expect.arrayContaining([expect.stringContaining("A01")]));
    expect(within(list).getAllByRole("row")).toHaveLength(3);
    expect(within(within(list).getByLabelText("公開先")).getAllByRole("option").map((o) => o.textContent))
      .toEqual(["すべて", "全社", "部署：経理部", "部署：総務部", "グループ：人事"]);
  });

  it("公開先を直す前に、見られる人の増減をグループごとに出し、保存は2回目に押したとき", async () => {
    const impact = { doc_id: "K01", detail: "月次決算の手順の公開先 経理部→全社", checked: 6, too_many: false, gain: 5, lose: 0,
      groups: [{ name: "社員", members: 6, gain: 5, lose: 0, can_see: 6 }] };
    const { calls } = fakeApi({
      "GET /api/admin/documents": VIEW, "GET /api/documents/ingest/sources": { sources: [] },
      "POST /api/admin/documents/preview": impact,
      "PATCH /api/admin/documents/K01": { ok: true, detail: impact.detail },
    });
    const user = userEvent.setup();
    render(<DocumentsPage />);
    await user.click(await screen.findByRole("button", { name: "月次決算の手順" }));
    const editor = screen.getByRole("region", { name: "文書「月次決算の手順」" });
    await user.click(within(editor).getByLabelText("部署：経理部"));
    await user.click(within(editor).getByLabelText("全社"));
    await user.click(within(editor).getByRole("button", { name: "変更を確かめる" }));
    const box = await within(editor).findByRole("region", { name: "変更の確かめ" });
    expect(box).toHaveTextContent("この変更：月次決算の手順の公開先 経理部→全社");
    expect(box).toHaveTextContent("見られるようになる人：5人 見られなくなる人：0人");
    expect(within(box).getByRole("table", { name: "グループごとの人数" })).toHaveTextContent("社員6人5人0人6人");
    expect(calls.find((c) => c.path === "/api/admin/documents/preview")?.body).toEqual(
      { doc_id: "K01", action: "publish", targets: ["special:all"], required: "documents.view", external: false });
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    await user.click(within(box).getByRole("button", { name: "保存する" }));
    expect(await screen.findByRole("status")).toHaveTextContent("保存しました：月次決算の手順の公開先 経理部→全社");
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ targets: ["special:all"], required: "documents.view", external: false });
  });

  it("外す・外したものから戻す", async () => {
    const removedDoc = doc("K02", "支払処理", "経理部", [["dept:経理部", "経理部"]], { removed_at: "2026-11-19T02:00:00+00:00", removed_by: "10005" });
    const { calls } = fakeApi({
      "GET /api/admin/documents": VIEW, "GET /api/documents/ingest/sources": { sources: [] },
      "GET /api/admin/documents?removed=true": { ...VIEW, removed: true, documents: [removedDoc] },
      "POST /api/admin/documents/preview": (body: { action: string }) => ({ json: { doc_id: "x", detail: body.action === "remove" ? "月次決算の手順を外した" : "支払処理を戻した",
        checked: 6, too_many: false, gain: body.action === "restore" ? 1 : 0, lose: body.action === "remove" ? 1 : 0, groups: [] } }),
      "POST /api/admin/documents/K01/remove": { ok: true, detail: "月次決算の手順を外した" },
      "POST /api/admin/documents/K02/restore": { ok: true, detail: "支払処理を戻した" },
    });
    const user = userEvent.setup();
    render(<DocumentsPage />);
    await user.click(await screen.findByRole("button", { name: "月次決算の手順" }));
    await user.click(screen.getByRole("button", { name: "この文書を外す" }));
    const box = await screen.findByRole("region", { name: "変更の確かめ" });
    expect(box).toHaveTextContent("見られなくなる人：1人");
    await user.click(within(box).getByRole("button", { name: "外す" }));
    expect(await screen.findByRole("status")).toHaveTextContent("保存しました：月次決算の手順を外した");

    await user.click(screen.getByRole("tab", { name: "外したもの" }));
    await user.click(await screen.findByRole("button", { name: "支払処理" }));
    expect(screen.getByRole("region", { name: "文書「支払処理」" })).toHaveTextContent("この文書は外しています（2026-11-19、10005）");
    await user.click(screen.getByRole("button", { name: "この文書を戻す" }));
    await user.click(within(await screen.findByRole("region", { name: "変更の確かめ" })).getByRole("button", { name: "戻す" }));
    expect(await screen.findByRole("status")).toHaveTextContent("保存しました：支払処理を戻した");
    expect(calls.filter((c) => c.method === "POST" && !c.path.endsWith("preview")).map((c) => c.path))
      .toEqual(["/api/admin/documents/K01/remove", "/api/admin/documents/K02/restore"]);
  });

  it("取り込み：ここで選ぶときは、持ち主の部署・公開先・要る権限を送る", async () => {
    const { calls } = fakeApi({
      "GET /api/admin/documents": VIEW,
      "GET /api/documents/ingest/sources": { sources: [{ path: "/data/docs/keiri", label: "docs/keiri" }] },
      "POST /api/documents/ingest": () => ({ status: 202, json: { job_id: "j1", kind: "ingest", status: "待ち", progress: 0, stage: "" } }),
      "GET /api/jobs/j1": { job_id: "j1", kind: "ingest", status: "完了", progress: 100, stage: "完了",
        result: { added: ["X01"], updated: [], unchanged: [], deleted: [], errors: [] } },
    });
    const user = userEvent.setup();
    render(<DocumentsPage />);
    const form = await screen.findByRole("region", { name: "文書の取り込み" });
    await within(form).findByRole("option", { name: "docs/keiri" });
    await user.click(within(form).getByLabelText("ここで選ぶ"));
    await user.selectOptions(within(form).getByLabelText("持ち主の部署"), "経理部");
    await user.click(within(form).getByLabelText("部署：経理部"));
    await user.selectOptions(within(form).getByLabelText("見るのに要る権限"), "infra.docs.view");
    await user.click(within(form).getByRole("button", { name: "取り込みを始める" }));
    expect(await within(form).findByRole("list", { name: "取り込みの結果" })).toHaveTextContent("足した：1件 X01");
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/documents/ingest")?.body).toEqual(
      { source: "/data/docs/keiri", mode: "chosen", owner: "経理部", targets: ["dept:経理部"], required: "infra.docs.view", external: false });
  });
});
