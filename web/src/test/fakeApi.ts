// テスト用の偽の API。「メソッド パス」ごとに返事を決め、呼ばれた内容を記録する
import { vi } from "vitest";

export type Handler = (body: any, headers: Record<string, string>) => { status?: number; json: unknown } | SseReply;

// Server-Sent Events の返事。gate を渡すと、その手前までを送ってから、gate が解けるのを待つ
export interface SseReply {
  sse: [string, unknown][];
  gate?: { at: number; wait: Promise<void> };
}

export function sse(events: [string, unknown][], gate?: SseReply["gate"]): Handler {
  return () => ({ sse: events, gate });
}

function sseBody(r: SseReply): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      controller.enqueue(enc.encode(": ping\n\n"));
      for (const [i, [event, data]] of r.sse.entries()) {
        if (r.gate && i === r.gate.at) await r.gate.wait;
        controller.enqueue(enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      }
      controller.close();
    },
  });
}

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
      if ("sse" in r) return new Response(sseBody(r), { status: 200, headers: { "Content-Type": "text/event-stream" } });
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
