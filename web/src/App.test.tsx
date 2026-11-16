import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { fakeApi, ME_EMPLOYEE, NOT_LOGGED_IN } from "./test/fakeApi";

afterEach(() => vi.unstubAllGlobals());

describe("S-01 ログイン", () => {
  it("ログインしていなければログイン画面を出し、社員番号でログインしたらホームへ", async () => {
    let loggedIn = false;
    const { calls } = fakeApi({
      "GET /api/me": () => (loggedIn ? { json: ME_EMPLOYEE } : NOT_LOGGED_IN),
      "GET /api/auth/login": { method: "dev" },
      "POST /api/auth/dev-login": (body: { employee_no: string }) => {
        loggedIn = body.employee_no === "10004";
        return loggedIn
          ? { json: { employee_no: "10004", name: "高橋 美咲" } }
          : { status: 401, json: { error: { code: "login_failed", message: "その社員番号ではログインできません" } } };
      },
      "GET /api/jobs": { jobs: [] },
    });
    render(<App />);
    const input = await screen.findByLabelText("社員番号");
    expect(window.location.pathname).toBe("/login");

    await userEvent.type(input, "99999");
    await userEvent.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("その社員番号ではログインできません");

    await userEvent.clear(input);
    await userEvent.type(input, "10004");
    await userEvent.click(screen.getByRole("button", { name: "ログイン" }));
    expect(await screen.findByRole("heading", { name: "ホーム" })).toBeInTheDocument();
    expect(screen.getByText(/高橋 美咲 さん/)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");

    // 状態を変えるリクエストには、CSRF 対策のヘッダーが付いている
    const login = calls.filter((c) => c.method === "POST");
    expect(login.length).toBeGreaterThan(0);
    expect(login.every((c) => c.headers["X-Yanua-Client"])).toBe(true);
  });

  it("SSO が設定されていないときは、そのことを出す", async () => {
    fakeApi({
      "GET /api/me": () => NOT_LOGGED_IN,
      "GET /api/auth/login": () => ({
        status: 501,
        json: { error: { code: "sso_not_configured", message: "SSO によるログインはまだ設定されていません（K-008）" } },
      }),
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("SSO によるログインはまだ設定されていません");
    expect(screen.queryByLabelText("社員番号")).not.toBeInTheDocument();
  });

  it("ログアウトしたらログイン画面に戻る", async () => {
    let loggedIn = true;
    fakeApi({
      "GET /api/me": () => (loggedIn ? { json: ME_EMPLOYEE } : NOT_LOGGED_IN),
      "GET /api/auth/login": { method: "dev" },
      "POST /api/auth/logout": () => {
        loggedIn = false;
        return { json: { ok: true } };
      },
      "GET /api/jobs": { jobs: [] },
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "ログアウト" }));
    expect(await screen.findByText("ログアウトしました")).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/login"));
  });

  it("途中でログインが切れたら、ログイン画面に戻して理由を出す", async () => {
    window.history.replaceState(null, "", "/chat");
    fakeApi({
      "GET /api/me": ME_EMPLOYEE,
      "GET /api/auth/login": { method: "dev" },
      "POST /api/ask/stream": () => NOT_LOGGED_IN,
    });
    render(<App />);
    await userEvent.type(await screen.findByLabelText("メッセージ"), "在宅勤務は？");
    await userEvent.click(screen.getByRole("button", { name: "送る" }));
    expect(await screen.findByText("ログインの期限が切れました。もう一度ログインしてください")).toBeInTheDocument();
  });
});

describe("S-02 ホーム", () => {
  it("権限に応じたメニューと、実行中のジョブだけを出す。お知らせの API がなければ欄を出さない", async () => {
    fakeApi({
      "GET /api/me": ME_EMPLOYEE,
      "GET /api/jobs": {
        jobs: [
          { job_id: "j1", kind: "ingest", status: "実行中", progress: 40, stage: "取り込みの準備", created_at: "" },
          { job_id: "j2", kind: "ingest", status: "完了", progress: 100, stage: "完了", created_at: "" },
        ],
      },
    });
    render(<App />);
    expect(await screen.findByRole("link", { name: /チャット/ })).toHaveAttribute("href", "/chat");
    expect(await screen.findByText("文書の取り込み")).toBeInTheDocument();
    expect(screen.getByText(/実行中・取り込みの準備/)).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar")).toHaveLength(1);
    await waitFor(() => expect(screen.queryByRole("heading", { name: "お知らせ" })).not.toBeInTheDocument());
  });

  it("使える操作がなければ、メニューにチャットを出さない", async () => {
    fakeApi({ "GET /api/me": { ...ME_EMPLOYEE, permissions: [] } });
    render(<App />);
    expect(await screen.findByText(/使えるメニューがありません/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /チャット/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "実行中のジョブ" })).not.toBeInTheDocument();
  });

  it("お知らせの API があれば、未読の件数を出す", async () => {
    fakeApi({
      "GET /api/me": ME_EMPLOYEE,
      "GET /api/jobs": { jobs: [] },
      "GET /api/notifications?unread=1": { notifications: [{}, {}, {}] },
    });
    render(<App />);
    expect(await screen.findByText("未読のお知らせが 3 件あります。")).toBeInTheDocument();
    expect(screen.getByText("実行中のジョブはありません。")).toBeInTheDocument();
  });

  it("メニューからチャットに移れる", async () => {
    fakeApi({ "GET /api/me": ME_EMPLOYEE, "GET /api/jobs": { jobs: [] } });
    render(<App />);
    await userEvent.click(await screen.findByRole("link", { name: /チャット/ }));
    expect(window.location.pathname).toBe("/chat");
    expect(await screen.findByRole("tab", { name: "社内文書に質問" })).toBeInTheDocument();
  });
});
