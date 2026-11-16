// API の呼び出し。エラーはすべて {"error": {"code", "message"}} の形で返ってくる（docs/screens.md「エラーの形」）。

export const CLIENT_HEADER = "X-Yanua-Client";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

// ログインが切れたとき（401 not_logged_in）に呼ぶ。App がログイン画面に戻す
let onLoggedOut: () => void = () => {};
export function setLoggedOutHandler(fn: () => void): void {
  onLoggedOut = fn;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  // GET 以外は、画面からの操作であることを示すヘッダーを付ける（CSRF 対策。サーバーが確かめる）
  if (method !== "GET") headers[CLIENT_HEADER] = "web";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "サーバーに接続できません。しばらくしてからやり直してください");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = data?.error;
    const e = new ApiError(
      res.status,
      err?.code ?? "error",
      err?.message ?? "処理できませんでした。しばらくしてからやり直してください",
    );
    if (res.status === 401 && e.code === "not_logged_in") onLoggedOut();
    throw e;
  }
  return data as T;
}

// Server-Sent Events で受け取る。届いた順に onEvent を呼ぶ。
// 送り始める前のエラー（ログイン・権限・形）は api() と同じく ApiError にする
export async function apiStream(path: string, body: unknown, onEvent: (event: string, data: any) => void): Promise<void> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { Accept: "text/event-stream", "Content-Type": "application/json", [CLIENT_HEADER]: "web" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "サーバーに接続できません。しばらくしてからやり直してください");
  }
  if (!res.ok || !res.body) {
    const err = (await res.json().catch(() => null))?.error;
    const e = new ApiError(res.status, err?.code ?? "error", err?.message ?? "処理できませんでした。しばらくしてからやり直してください");
    if (res.status === 401 && e.code === "not_logged_in") onLoggedOut();
    throw e;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const flush = (block: string) => {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      // ":" で始まる行は接続を保つための空の行なので読み飛ばす
    }
    if (data.length) onEvent(event, JSON.parse(data.join("\n")));
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let i: number;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        flush(buf.slice(0, i));
        buf = buf.slice(i + 2);
      }
    }
  } catch {
    throw new ApiError(0, "network", "途中で接続が切れました。もう一度送ってください");
  }
  if (buf.trim()) flush(buf);
}

// ------------------------------------------------------------ API の形

export interface Me {
  employee_no: string;
  name: string;
  dept: string;
  roles: string[];
  permissions: string[];
}

export interface Source {
  doc_id: string;
  title: string;
  section: string;
}

export interface AskResult {
  answer: string;
  sources: Source[];
  refused: boolean;
  elapsed_sec: number;
}

export interface ChatReply {
  session_id: string;
  finished: boolean;
  reply: string;
  ticket_id?: string | null;
  sources?: Source[];
}

export interface DocumentBody {
  doc_id: string;
  title: string;
  updated: string;
  sections: { section: string; text: string }[];
}

export interface Job {
  job_id: string;
  kind: string;
  status: string;
  progress: number;
  stage: string;
  created_at: string;
}

export const RUNNING_STATUSES = ["待ち", "実行中"];
