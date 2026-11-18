import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import Audit from "./Audit";
import SettingsPage from "./Settings";
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
    // ログイン前の「した人」は「なし」を1つだけ出す
    expect(within(rows[3]).getAllByText("なし")).toHaveLength(1);
    expect(rows[3].querySelectorAll("td")[1].textContent).toBe("なし");
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

// ---------------------------------------------------------------- S-52 設定

const status = (using = "default") => ({ using, default_changed: false, current_version: using === "saved" ? 2 : null, default_updated_at: null });
const PRESETS = {
  標準: { description: "所属部署・お名前・従業員番号のうち2つ", question: "所属部署とお名前と従業員番号を教えてください", clues: ["dept", "name", "employee_no"],
    confirm: [["name", "employee_no"], ["name", "dept"], ["employee_no", "dept"]] },
  社員コードを使わない: { description: "部署と名前で確定", question: "お名前と所属部署を教えてください", clues: ["name", "dept"], confirm: [["name", "dept"]] },
};
const detail = (key: string, values: object, over: object = {}) => ({
  key, label: { identity: "本人確認の方針", audit: "監査ログ", models: "モデル", files: "ファイル（作業用フォルダ）" }[key],
  status: status(), pinned: {}, values, running: values, applied: true, problems: [], versions: [], current: null,
  meta: {
    identity: { presets: PRESETS, clues: { employee_no: "従業員番号", name: "名前", dept: "部署" } },
    audit: { limits: { keep_days: { min: 1, max: 3650, unit: "日" } } },
    models: { error: "", catalog: [
      { name: "qwen2.5:3b-instruct", use: ["chat", "summary"], state: "ok", message: "" },
      { name: "qwen2.5:7b", use: ["chat", "summary"], state: "ok", message: "" },
      { name: "llama3.2:3b", use: ["chat"], state: "not_installed", message: "入っていません（ollama pull llama3.2:3b で入手し、models pull llama3.2:3b で登録してください）" },
      { name: "nomic-embed-text", use: ["embedding"], state: "ok", message: "" },
    ] },
    files: { limits: { work_max_gb: { min: 1, max: 10000, unit: "GB" }, file_keep_hours: { min: 1, max: 720, unit: "時間" } } },
  }[key],
  ...over,
});
const IDENTITY = { preset: "標準", ...PRESETS.標準, surname_counts: false, max_follow_ups: 2 };
delete (IDENTITY as Record<string, unknown>).description;
const settingRoutes = (over: Record<string, unknown> = {}) => {
  const d = {
    identity: detail("identity", IDENTITY), audit: detail("audit", { keep_days: 365, keep_body: false }),
    models: detail("models", { chat: "qwen2.5:3b-instruct", summary: "qwen2.5:7b" }), files: detail("files", { work_max_gb: 20, file_keep_hours: 24 }),
    ...over,
  } as Record<string, ReturnType<typeof detail>>;
  return {
    "GET /api/admin/settings": { sections: Object.values(d), pending_restart: Object.values(d).filter((s) => !s.applied).map((s) => s.key),
      links: [{ label: "PDF の仕分けルール", path: "/paper/sort", note: "仕分けルールは「PDF の仕分け」の画面で直します" }] },
    ...Object.fromEntries(Object.entries(d).map(([k, v]) => [`GET /api/admin/settings/${k}`, v])),
  };
};

describe("S-52 設定", () => {
  const SETTINGS_ADMIN = { ...ADMIN, permissions: [...ADMIN.permissions, "settings.view", "settings.edit"] };

  it("管理者のメニューにだけ出し、社員が URL を開いても読まない", async () => {
    fakeApi({ "GET /api/me": SETTINGS_ADMIN, "GET /api/jobs": { jobs: [] }, "GET /api/notifications?unread=1": { notifications: [], unread: 0 } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("link", { name: /^設定/ })).toBeInTheDocument();
    unmount();
    window.history.replaceState(null, "", "/admin/settings");
    const { calls } = fakeApi({ "GET /api/me": ME_EMPLOYEE });
    render(<App />);
    expect(await screen.findByText("この画面を使う権限がありません")).toBeInTheDocument();
    expect(calls.some((c) => c.path.startsWith("/api/admin/settings"))).toBe(false);
  });

  it("4つの区切りと仕分けルールへのリンクを出し、再起動待ちを上にまとめる", async () => {
    fakeApi(settingRoutes({ files: detail("files", { work_max_gb: 30, file_keep_hours: 24 }, { status: status("saved"), applied: false,
      running: { work_max_gb: 50, file_keep_hours: 24 } }) }));
    render(<SettingsPage canEdit />);
    for (const name of ["本人確認の方針", "監査ログ", "モデル", "ファイル（作業用フォルダ）"])
      expect(await screen.findByRole("region", { name })).toBeInTheDocument();
    expect(await screen.findByRole("status", { name: "再起動待ち" })).toHaveTextContent("ファイル（作業用フォルダ）");
    expect(within(screen.getByRole("region", { name: "ファイル（作業用フォルダ）" })).getByText("再起動すると使われます")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "PDF の仕分けルールへ" })).toHaveAttribute("href", "/paper/sort");
  });

  it("本文を残すは画面の中で確かめてから入れ、誤りは欄の下に出して保存しない", async () => {
    let saved: unknown = null;
    const { calls } = fakeApi({
      ...settingRoutes(),
      "PUT /api/admin/settings/audit": (body: { values: { keep_days: number } }) => {
        if (body.values.keep_days > 3650)
          return { status: 400, json: { error: { code: "invalid_setting", message: "設定に直すところがあります",
            details: [{ where: "keep_days", message: "1〜3650日の間にしてください" }] } } };
        saved = body.values;
        return { json: { ...detail("audit", body.values, { status: status("saved") }), version: 2 } };
      },
    });
    const user = userEvent.setup();
    render(<SettingsPage canEdit />);
    const box = await screen.findByRole("region", { name: "監査ログ" });
    const days = await within(box).findByLabelText("保存期間");
    await user.clear(days);
    await user.type(days, "5000");
    await user.click(within(box).getByRole("button", { name: "保存する" }));
    expect(await within(box).findByText("1〜3650日の間にしてください")).toBeInTheDocument();
    expect(saved).toBeNull();

    await user.click(within(box).getByLabelText("質問の本文も残す"));
    expect(within(box).getByText("質問の本文が記録に残り、管理者が読めるようになります。")).toBeInTheDocument();
    expect(within(box).getByLabelText("質問の本文も残す")).not.toBeChecked();          // 確かめるまでは入らない
    await user.click(within(box).getByRole("button", { name: "はい、残す" }));
    expect(within(box).getByLabelText("質問の本文も残す")).toBeChecked();
    await user.clear(days);
    await user.type(days, "30");
    await user.click(within(box).getByRole("button", { name: "保存する" }));
    expect(await within(box).findByText(/保存しました（版 2）。再起動しなくても、もう使われています。/)).toBeInTheDocument();
    expect(saved).toEqual({ keep_days: 30, keep_body: true });
    expect(calls.filter((c) => c.method === "PUT")).toHaveLength(2);
  });

  it("起動の指定が勝つ項目は変えられないことを出す", async () => {
    fakeApi(settingRoutes({ audit: detail("audit", { keep_days: 365, keep_body: false }, { pinned: { keep_body: "--audit-body" },
      running: { keep_days: 365, keep_body: true } }) }));
    render(<SettingsPage canEdit />);
    const box = await screen.findByRole("region", { name: "監査ログ" });
    expect(await within(box).findByLabelText("質問の本文も残す")).toBeDisabled();
    expect(within(box).getByText(/起動の指定（--audit-body）が勝っているので、画面では変えられません。今は「true」で動いています/)).toBeInTheDocument();
    expect(within(box).getByLabelText("保存期間")).toBeEnabled();
  });

  it("本人確認：型を選ぶと中身が入り、保存前の方針で言い方を試せる", async () => {
    const { calls } = fakeApi({
      ...settingRoutes(),
      "POST /api/admin/settings/identity/try": { identified: false, matched: [], ask: ["所属部署"], question: "所属部署を教えていただけますか？", note: "確定に足りる手がかりがそろいませんでした" },
    });
    const user = userEvent.setup();
    render(<SettingsPage canEdit />);
    const box = await screen.findByRole("region", { name: "本人確認の方針" });
    await user.selectOptions(await within(box).findByLabelText("型"), "社員コードを使わない");
    expect(within(box).getByLabelText("最初の聞き方")).toHaveValue("お名前と所属部署を教えてください");
    expect(within(box).getByLabelText("従業員番号")).not.toBeChecked();
    expect(within(box).getByLabelText("名前＋部署")).toBeChecked();
    expect(within(box).getByLabelText("名前＋従業員番号")).not.toBeChecked();
    await user.type(within(box).getByLabelText("試す（言い方を1行。保存はしません）"), "高橋美咲です");
    await user.click(within(box).getByRole("button", { name: "試す" }));
    expect(await within(box).findByText(/特定できませんでした。聞き返すもの：所属部署/)).toBeInTheDocument();
    const sent = calls.find((c) => c.path === "/api/admin/settings/identity/try")?.body;
    expect(sent).toEqual({ said: "高橋美咲です", values: expect.objectContaining({ preset: "社員コードを使わない", clues: ["name", "dept"], confirm: [["name", "dept"]] }) });
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("モデルは目録にあり使えるものだけを選べ、版に戻すは確かめてから", async () => {
    fakeApi({
      ...settingRoutes({ models: detail("models", { chat: "qwen2.5:3b-instruct", summary: "qwen2.5:7b" }, { status: status("saved"), current: 2,
        versions: [{ version: 2, saved_at: "2026-11-18T10:00:00+09:00", saved_by: "10005", saved_by_name: "田中 健", action: "モデルを保存した" },
          { version: 1, saved_at: "2026-11-18T09:00:00+09:00", saved_by: "（既定）", saved_by_name: "（既定）", action: "保存の前の中身（既定）" }] }) }),
      "POST /api/admin/settings/models/history/1/restore": { ...detail("models", { chat: "qwen2.5:3b-instruct", summary: "qwen2.5:7b" }), version: 3 },
    });
    const user = userEvent.setup();
    render(<SettingsPage canEdit />);
    const box = await screen.findByRole("region", { name: "モデル" });
    const chat = await within(box).findByLabelText("チャット・ヘルプデスク・受付のモデル");
    // 目録にあってチャットに使えるものだけ（埋め込み用は出さない）。入っていないものは理由つきで選ばせない
    const options = within(chat).getAllByRole("option") as HTMLOptionElement[];
    expect(options.map((o) => o.textContent)).toEqual(["qwen2.5:3b-instruct", "qwen2.5:7b", "llama3.2:3b（入っていません）"]);
    expect(options.map((o) => o.disabled)).toEqual([false, false, true]);
    expect(within(box).getByText(/llama3.2:3b：入っていません（ollama pull llama3.2:3b で入手し/)).toBeInTheDocument();
    const summary = within(box).getByLabelText("議事録の要約のモデル");
    expect(within(summary).getAllByRole("option").map((o) => o.textContent)).toEqual(["qwen2.5:3b-instruct", "qwen2.5:7b"]);
    await user.click(within(box).getByRole("button", { name: "モデルを版 1 に戻す" }));
    expect(within(box).getByText("今の設定を、版 1 の中身にします。")).toBeInTheDocument();
    await user.click(within(box).getByRole("button", { name: "はい、戻す" }));
    expect(await within(box).findByText(/版 1 に戻しました（版 3）/)).toBeInTheDocument();
  });

  it("見るだけの権限なら、保存・版に戻す・既定に戻すを出さない", async () => {
    fakeApi(settingRoutes({ files: detail("files", { work_max_gb: 20, file_keep_hours: 24 }, { status: status("saved"),
      versions: [{ version: 1, saved_at: "2026-11-18T09:00:00+09:00", saved_by: "（既定）", saved_by_name: "（既定）", action: "x" }] }) }));
    render(<SettingsPage />);
    const box = await screen.findByRole("region", { name: "ファイル（作業用フォルダ）" });
    expect(await within(box).findByLabelText("作業用フォルダの合計の上限")).toBeDisabled();
    expect(within(box).queryByRole("button", { name: /保存する|既定に戻す|この版に戻す|戻す$/ })).not.toBeInTheDocument();
  });
});
