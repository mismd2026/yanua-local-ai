// テスト用の偽の API。「メソッド パス」ごとに返事を決め、呼ばれた内容を記録する
import { vi } from "vitest";

export type Handler = (body: any, headers: Record<string, string>) => { status?: number; json: unknown };

export interface Call {
  method: string;
  path: string;
  body: any;
  headers: Record<string, string>;
}

export function fakeApi(routes: Record<string, Handler | unknown>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = String(input);
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path, body, headers });
    const route = routes[`${method} ${path}`];
    let status = 200;
    let json: unknown;
    if (route === undefined) {
      status = 404;
      json = { error: { code: "not_found", message: "見つかりません" } };
    } else if (typeof route === "function") {
      const r = (route as Handler)(body, headers);
      status = r.status ?? 200;
      json = r.json;
    } else {
      json = route;
    }
    return new Response(JSON.stringify(json), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

export const ME_EMPLOYEE = {
  employee_no: "10004",
  name: "高橋 美咲",
  dept: "営業部",
  roles: ["社員"],
  permissions: ["ask", "documents.list", "helpdesk", "jobs.own", "search"],
};

export const NOT_LOGGED_IN = { status: 401, json: { error: { code: "not_logged_in", message: "ログインしてください" } } };
