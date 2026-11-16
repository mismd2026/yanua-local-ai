import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import Chat from "./Chat";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

afterEach(() => vi.unstubAllGlobals());

const ANSWER = {
  answer: "在宅勤務は週3日までです[1]。",
  sources: [{ doc_id: "A01", title: "就業規則", section: "在宅勤務" }],
  refused: false,
  elapsed_sec: 1.2,
};

describe("S-10 チャット：社内文書に質問", () => {
  it("質問すると、出典つきの回答を出し、出典を開ける", async () => {
    const { calls } = fakeApi({
      "POST /api/ask": ANSWER,
      "GET /api/documents/A01": {
        doc_id: "A01",
        title: "就業規則",
        updated: "2026-04-01",
        sections: [
          { section: "勤務時間", text: "9時から18時です。" },
          { section: "在宅勤務", text: "在宅勤務は週3日までとする。" },
        ],
      },
    });
    render(<Chat me={ME_EMPLOYEE} />);
    await userEvent.type(screen.getByLabelText("メッセージ"), "在宅勤務は週に何日？{Enter}");
    expect(await screen.findByText("在宅勤務は週3日までです[1]。")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/ask")?.body).toEqual({ question: "在宅勤務は週に何日？" });

    await userEvent.click(screen.getByRole("button", { name: "就業規則（在宅勤務）" }));
    const panel = await screen.findByRole("complementary", { name: "出典" });
    expect(await within(panel).findByText("在宅勤務は週3日までとする。")).toBeInTheDocument();
    // 回答のもとになった節を目立たせる
    expect(within(panel).getByRole("heading", { name: "在宅勤務" }).closest("section")).toHaveClass("hit");
    await userEvent.click(within(panel).getByRole("button", { name: "閉じる" }));
    expect(screen.queryByRole("complementary", { name: "出典" })).not.toBeInTheDocument();
  });

  it("答えられないときは出典を出さない", async () => {
    fakeApi({
      "POST /api/ask": { answer: "社内の資料からは見つかりませんでした。", sources: [{ doc_id: "A01", title: "就業規則", section: "" }], refused: true, elapsed_sec: 0.5 },
    });
    render(<Chat me={ME_EMPLOYEE} />);
    await userEvent.type(screen.getByLabelText("メッセージ"), "社食のメニューは？{Enter}");
    expect(await screen.findByText("社内の資料からは見つかりませんでした。")).toBeInTheDocument();
    expect(screen.queryByText("出典：")).not.toBeInTheDocument();
  });

  it("エラーのときは、サーバーの文言をそのまま出す", async () => {
    fakeApi({
      "POST /api/ask": () => ({ status: 502, json: { error: { code: "llm_unavailable", message: "回答を作るAIに接続できません。しばらくしてからやり直してください" } } }),
    });
    render(<Chat me={ME_EMPLOYEE} />);
    await userEvent.type(screen.getByLabelText("メッセージ"), "在宅勤務は？{Enter}");
    expect(await screen.findByText("回答を作るAIに接続できません。しばらくしてからやり直してください")).toBeInTheDocument();
  });

  it("ヘルプデスクの権限しかない人には、質問のタブを出さない", async () => {
    fakeApi({});
    render(<Chat me={{ ...ME_EMPLOYEE, permissions: ["helpdesk"] }} />);
    expect(screen.queryByRole("tab", { name: "社内文書に質問" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "相談を始める" })).toBeVisible();
  });
});

describe("S-10 チャット：ヘルプデスク", () => {
  it("相談を始め、やりとりして、問い合わせ票の番号を出す", async () => {
    const replies = [
      { session_id: "s1", finished: false, reply: "高橋さん、ありがとうございます。どのようなことでお困りですか？" },
      { session_id: "s1", finished: false, reply: "わかる範囲で教えてください：場所。" },
      { session_id: "s1", finished: true, reply: "受付番号 T-0001 で引き継ぎました（優先度：高）。担当者から連絡します。", ticket_id: "T-0001", sources: [] },
    ];
    const { calls } = fakeApi({
      "POST /api/chat/sessions": () => ({ status: 201, json: replies[0] }),
      "POST /api/chat/sessions/s1/messages": () => ({ json: calls.filter((c) => c.path.endsWith("/messages")).length === 1 ? replies[1] : replies[2] }),
    });
    render(<Chat me={ME_EMPLOYEE} />);
    await userEvent.click(screen.getByRole("tab", { name: "ヘルプデスクに相談" }));
    await userEvent.click(screen.getByRole("button", { name: "相談を始める" }));
    expect(await screen.findByText(/どのようなことでお困りですか/)).toBeInTheDocument();
    expect(calls[0].body).toEqual({ flow: "helpdesk" });

    const box = screen.getAllByLabelText("メッセージ").find((el) => el.offsetParent !== null || !el.closest("[hidden]"))!;
    await userEvent.type(box, "複合機が止まっています{Enter}");
    expect(await screen.findByText("わかる範囲で教えてください：場所。")).toBeInTheDocument();
    await userEvent.type(box, "本社3階です{Enter}");
    expect(await screen.findByText("受付番号 T-0001")).toBeInTheDocument();
    expect(screen.getByText("相談が終わりました。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新しく相談する" })).toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/api/chat/sessions/s1/messages").map((c) => c.body.text)).toEqual(["複合機が止まっています", "本社3階です"]);
  });

  it("時間が切れて会話が消えていたら、新しく始めてもらう", async () => {
    fakeApi({
      "POST /api/chat/sessions": () => ({ status: 201, json: { session_id: "s2", finished: false, reply: "どのようなことでお困りですか？" } }),
    });
    render(<Chat me={{ ...ME_EMPLOYEE, permissions: ["helpdesk"] }} />);
    await userEvent.click(screen.getByRole("button", { name: "相談を始める" }));
    await userEvent.type(await screen.findByLabelText("メッセージ"), "はい{Enter}");
    expect(await screen.findByText(/この相談は終わりました。新しく始めてください/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新しく相談する" })).toBeInTheDocument();
  });

  it("相談をやめると、会話を消す", async () => {
    const { calls } = fakeApi({
      "POST /api/chat/sessions": () => ({ status: 201, json: { session_id: "s3", finished: false, reply: "どのようなことでお困りですか？" } }),
      "DELETE /api/chat/sessions/s3": { ok: true },
    });
    render(<Chat me={{ ...ME_EMPLOYEE, permissions: ["helpdesk"] }} />);
    await userEvent.click(screen.getByRole("button", { name: "相談を始める" }));
    await userEvent.click(await screen.findByRole("button", { name: "相談をやめる" }));
    expect(await screen.findByText("相談をやめました。")).toBeInTheDocument();
    const del = calls.find((c) => c.method === "DELETE");
    expect(del?.path).toBe("/api/chat/sessions/s3");
    expect(del?.headers["X-Yanua-Client"]).toBeTruthy();
  });
});
