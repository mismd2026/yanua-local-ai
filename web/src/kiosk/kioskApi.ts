// 受付端末の API の呼び出し。端末トークン（Authorization: Bearer）だけで呼ぶ。社内のログインのクッキーは使わない
const TOKEN_KEY = "yanua-kiosk-token";

export class KioskError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export function loadToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 保存できない環境（プライベートモードなど）では、この画面を開いている間だけ使う
  }
}

export interface KioskVisit {
  date: string;
  time: string;
  room: string;
  host_name: string;
  host_contact: string;
  visitor_company: string;
  visitor_name: string;
}

export interface KioskReply {
  session_id: string;
  finished: boolean;
  reply: string;
  visit: KioskVisit | null;
  pass_id: string | null;
}

export interface CheckInReply {
  ok: boolean;
  message: string;
  visit: KioskVisit | null;
  pass_id: string | null;
}

export async function kiosk<T>(token: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "X-Yanua-Client": "kiosk",
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      credentials: "omit",
      body: JSON.stringify(body ?? {}),
    });
  } catch {
    throw new KioskError(0, "network", "ただいま受付の機械が使えません。恐れ入りますが、受付の者にお声がけください。");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new KioskError(res.status, data?.error?.code ?? "error",
      data?.error?.message ?? "恐れ入りますが、受付の者にお声がけください。");
  }
  return data as T;
}

// コードの入力のゆれをそろえて「ABCD-EFGH」の形にする（0 O 1 I は使っていない）
export function formatCode(text: string): string {
  const c = text.normalize("NFKC").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return c.length > 4 ? `${c.slice(0, 4)}-${c.slice(4)}` : c;
}
