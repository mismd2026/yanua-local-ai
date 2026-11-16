import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Kiosk from "./Kiosk";
import { formatCode } from "./kioskApi";
import { fakeApi } from "../test/fakeApi";

const VISIT = { date: "2026-11-16", time: "15:00", room: "5階 第2会議室", host_name: "高橋 美咲", host_contact: "受付にお声がけください",
  visitor_company: "ABC商事", visitor_name: "木村 誠" };
const FAILED = { ok: false, message: "恐れ入りますが、コードを確認できませんでした。", visit: null };

beforeEach(() => window.localStorage.setItem("yanua-kiosk-token", "yk_test"));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("受付端末", () => {
  it("メニューや社内の画面へのリンクを置かない", () => {
    fakeApi({});
    render(<Kiosk />);
    expect(screen.getByRole("heading", { name: "いらっしゃいませ" })).toBeInTheDocument();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.queryByText(/ログイン|ホーム|メニュー/)).not.toBeInTheDocument();
  });

  it("コードで受け付け、予定と入館票の印刷を出す。端末トークンだけで呼び、クッキーは送らない", async () => {
    const { calls } = fakeApi({
      "POST /api/kiosk/check-in": (body: { code: string }) => ({
        json: body.code === "HJ6P-W9XE" ? { ok: true, message: "ABC商事 木村 誠様、お待ちしておりました。", visit: VISIT } : FAILED,
      }),
    });
    const print = vi.fn();
    vi.stubGlobal("print", print);
    render(<Kiosk />);
    await userEvent.click(screen.getByRole("button", { name: /お持ちの方/ }));
    const input = screen.getByLabelText("インビテーションコード");
    await userEvent.type(input, "zzzzzzzz");
    expect(input).toHaveValue("ZZZZ-ZZZZ");
    await userEvent.click(screen.getByRole("button", { name: "確認する" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("コードを確認できませんでした");
    expect(input).toHaveValue("");
    expect(screen.queryByText(/受付の者をお呼びください/)).not.toBeInTheDocument();

    await userEvent.type(input, "hj6p w9xe");
    await userEvent.click(screen.getByRole("button", { name: "確認する" }));
    expect(await screen.findByText("ABC商事 木村 誠様、お待ちしておりました。")).toBeInTheDocument();
    expect(screen.getAllByText("5階 第2会議室").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "入館票を印刷する" }));
    expect(print).toHaveBeenCalled();

    expect(calls.every((c) => c.headers.Authorization === "Bearer yk_test" && c.headers["X-Yanua-Client"])).toBe(true);
    expect(calls.map((c) => c.body.code)).toEqual(["ZZZZ-ZZZZ", "HJ6P-W9XE"]);
  });

  it("コードを持っていない方は会話で受け付ける", async () => {
    fakeApi({
      "POST /api/kiosk/sessions": () => ({ status: 201, json: { session_id: "s1", finished: false, reply: "いらっしゃいませ。ご用件をお聞かせください。", visit: null } }),
      "POST /api/kiosk/sessions/s1/messages": () => ({ json: { session_id: "s1", finished: true, reply: "担当の者に確認いたしますので、少々お待ちください。", visit: null } }),
    });
    render(<Kiosk />);
    await userEvent.click(screen.getByRole("button", { name: /お持ちでない方/ }));
    expect(await screen.findByText("いらっしゃいませ。ご用件をお聞かせください。")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("お返事"), "山田様とお約束です{Enter}");
    expect(await screen.findByText("担当の者に確認いたしますので、少々お待ちください。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "終わる" })).toBeInTheDocument();
  });

  it("一定時間触られなければ、最初の画面に戻り、入力を残さない", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fakeApi({});
    render(<Kiosk idleMs={5000} />);
    await userEvent.click(screen.getByRole("button", { name: /お持ちの方/ }));
    await userEvent.type(screen.getByLabelText("インビテーションコード"), "ABCD");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5100);
    });
    expect(screen.getByRole("heading", { name: "いらっしゃいませ" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /お持ちの方/ }));
    expect(screen.getByLabelText("インビテーションコード")).toHaveValue("");
  });

  it("登録されていない端末になったら、設定の画面に戻す", async () => {
    fakeApi({
      "POST /api/kiosk/check-in": () => ({ status: 401, json: { error: { code: "kiosk_not_registered", message: "登録されていません" } } }),
    });
    render(<Kiosk />);
    await userEvent.click(screen.getByRole("button", { name: /お持ちの方/ }));
    await userEvent.type(screen.getByLabelText("インビテーションコード"), "ABCDEFGH");
    await userEvent.click(screen.getByRole("button", { name: "確認する" }));
    expect(await screen.findByRole("heading", { name: "受付端末の設定" })).toBeInTheDocument();
    expect(window.localStorage.getItem("yanua-kiosk-token")).toBeNull();
  });

  it("コードの確認ができないことが続いたら、受付の者を呼ぶ案内を出す（返事の文言は同じまま）。最初の画面に戻っても残す", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = fakeApi({ "POST /api/kiosk/check-in": () => ({ json: FAILED }) });
    render(<Kiosk />);
    await userEvent.click(screen.getByRole("button", { name: /お持ちの方/ }));
    const input = screen.getByLabelText("インビテーションコード");
    for (let i = 1; i <= 3; i++) {
      await userEvent.type(input, "ZZZZZZZZ");
      await userEvent.click(screen.getByRole("button", { name: "確認する" }));
      await screen.findByText("恐れ入りますが、コードを確認できませんでした。");
      if (i < 3) expect(screen.queryByText(/受付の者をお呼びください/)).not.toBeInTheDocument();
    }
    expect(await screen.findByText("お手数ですが、受付の者をお呼びください。")).toBeInTheDocument();
    // サーバーの返事の文言は、そのまま出している
    expect(screen.getByText("恐れ入りますが、コードを確認できませんでした。")).toBeInTheDocument();
    expect(calls).toHaveLength(3);

    await userEvent.click(screen.getByRole("button", { name: "最初に戻る" }));
    expect(screen.getByText("お手数ですが、受付の者をお呼びください。")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(66_000);
    });
    expect(screen.queryByText(/受付の者をお呼びください/)).not.toBeInTheDocument();
  });

  it("コードの入力のゆれをそろえる", () => {
    expect(formatCode("ｈｊ６ｐ－ｗ９ｘｅ")).toBe("HJ6P-W9XE");
    expect(formatCode("abc")).toBe("ABC");
    expect(formatCode("ABCD-EFGH-IJ")).toBe("ABCD-EFGH");
  });
});
