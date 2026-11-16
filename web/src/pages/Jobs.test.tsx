import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import Jobs from "./Jobs";
import { fakeApi, ME_EMPLOYEE } from "../test/fakeApi";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const ADMIN = {
  ...ME_EMPLOYEE,
  employee_no: "10005",
  roles: ["社員", "情シス", "管理者"],
  permissions: [...ME_EMPLOYEE.permissions, "documents.ingest", "jobs.all"],
};

const job = (over: object) => ({
  job_id: "j1", kind: "ingest", owner: "10005", status: "完了", progress: 100, stage: "完了",
  params: { source: "/srv/demo/company/public" }, result: { added: ["P01", "P02"], updated: [], unchanged: [], deleted: [], errors: [] },
  error: null, created_at: "2026-11-16T03:00:00+00:00", started_at: "2026-11-16T03:00:01+00:00", finished_at: "2026-11-16T03:00:05+00:00",
  ...over,
});

describe("S-03 ジョブの一覧", () => {
  it("自分のジョブを一覧し、詳しい内容（結果）を開ける。社員には取り込みも「すべての人」も出さない", async () => {
    fakeApi({ "GET /api/jobs": { jobs: [job({})] }, "GET /api/jobs/j1": job({}) });
    render(<Jobs me={ME_EMPLOYEE} />);
    expect(await screen.findByText("完了")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "すべての人のジョブ" })).not.toBeInTheDocument();
    expect(screen.queryByText("文書の取り込み（管理者）")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /の詳しい内容/ }));
    const detail = await screen.findByRole("complementary", { name: "ジョブの詳しい内容" });
    expect(await within(detail).findByText("追加：2 件")).toBeInTheDocument();
    expect(within(detail).getByText("/srv/demo/company/public")).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: "このジョブを取り消す" })).not.toBeInTheDocument();
  });

  it("実行中のジョブは取り消せる", async () => {
    let status = "実行中";
    const { calls } = fakeApi({
      "GET /api/jobs": () => ({ json: { jobs: [job({ status, progress: 40, result: null })] } }),
      "GET /api/jobs/j1": () => ({ json: job({ status, progress: 40, result: null }) }),
      "DELETE /api/jobs/j1": () => {
        status = "取り消し";
        return { json: job({ status }) };
      },
    });
    window.history.replaceState(null, "", "/jobs?job=j1");
    render(<Jobs me={ME_EMPLOYEE} />);
    const detail = await screen.findByRole("complementary", { name: "ジョブの詳しい内容" });
    await userEvent.click(await within(detail).findByRole("button", { name: "このジョブを取り消す" }));
    await waitFor(() => expect(within(detail).getByText("取り消し")).toBeInTheDocument());
    expect(calls.find((c) => c.method === "DELETE")?.headers["X-Yanua-Client"]).toBeTruthy();
  });

  it("実行中のジョブがあるあいだだけ、数秒ごとに読み直す", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    const { calls } = fakeApi({
      "GET /api/jobs": () => {
        n += 1;
        return { json: { jobs: [job({ status: n < 2 ? "実行中" : "完了", progress: n < 2 ? 50 : 100 })] } };
      },
    });
    render(<Jobs me={ME_EMPLOYEE} />);
    expect(await screen.findByText("実行中")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(3100);
    expect(await screen.findByText("完了")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(10000);
    expect(calls.filter((c) => c.path === "/api/jobs")).toHaveLength(2);
  });

  it("管理者は、すべての人のジョブに切り替えられ、取り込みを始められる", async () => {
    const { calls } = fakeApi({
      "GET /api/jobs": { jobs: [] },
      "GET /api/jobs?all=true": { jobs: [job({ owner: "10004" })] },
      "GET /api/documents/ingest/sources": {
        sources: [
          { path: "/srv/demo/company", label: "demo/company" },
          { path: "/srv/demo/company/public", label: "demo/company/public" },
        ],
      },
      "POST /api/documents/ingest": () => ({ status: 202, json: job({ job_id: "j9", status: "待ち", progress: 0, result: null, started_at: null }) }),
      "GET /api/jobs/j9": job({ job_id: "j9", status: "待ち", progress: 0, result: null, started_at: null }),
    });
    render(<Jobs me={ADMIN} />);
    expect(await screen.findByText("ジョブはありません。")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "すべての人のジョブ" }));
    expect(await screen.findByRole("columnheader", { name: "依頼した人" })).toBeInTheDocument();
    expect(screen.getByText("10004")).toBeInTheDocument();

    await userEvent.selectOptions(await screen.findByLabelText("フォルダ"), "/srv/demo/company/public");
    await userEvent.click(screen.getByRole("button", { name: "取り込みを始める" }));
    expect(await screen.findByText("取り込みを受け付けました。")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ source: "/srv/demo/company/public" });
    const detail = await screen.findByRole("complementary", { name: "ジョブの詳しい内容" });
    expect(await within(detail).findByText("まだ始まっていません")).toBeInTheDocument();
  });
});
