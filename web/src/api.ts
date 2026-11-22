// API の呼び出し。エラーはすべて {"error": {"code", "message"}} の形で返ってくる（docs/screens.md「エラーの形」）。
// 入力の誤りには details（[{where, message}]）が付くことがある。画面は where の項目の横に出す

export const CLIENT_HEADER = "X-Yanua-Client";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: { where: string; message: string }[] = [],
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
      Array.isArray(err?.details) ? err.details : [],
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
  owner?: string;
  status: string;
  progress: number;
  stage: string;
  params?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

// ジョブの種類の表示名（yanua/api/app.py で register したもの）
export const JOB_NAMES: Record<string, string> = {
  ingest: "文書の取り込み",
  "paper-plan": "PDF の仕分けの予定",
  "paper-convert": "PDF の変換",
  meeting: "議事録",
  hr: "人事データ",
};
export const jobName = (kind: string) => JOB_NAMES[kind] ?? kind;

export const RUNNING_STATUSES = ["待ち", "実行中"];

export interface Notification {
  notification_id: number;
  created_at: string;
  kind: string;
  message: string;
  ref: string;
  read: boolean;
  to: string;
}

export interface Visit {
  visit_id: number;
  date: string;
  time: string;
  room: string;
  host_employee_no: string;
  host_name: string;
  host_dept: string;
  host_contact: string;
  visitor_company: string;
  visitor_name: string;
  purpose: string;
  checked_in_at: string | null;
  cancelled: boolean;
}

export interface CreatedVisit {
  visit: Visit;
  code: string;
  invitation: string;
}

export interface ReceptionRecord {
  record_id: string;
  created_at: string;
  channel: string;
  category: string;
  route: string;
  priority: string;
  purpose: string;
  visitor: { company: string; name: string };
  contact: string;
  details: string;
  host: { employee_no: string; name: string } | null;
  candidates: string[];
  resolved: boolean;
  status: string;
  note: string;
  status_updated_at: string | null;
  status_updated_by: string | null;
}

export interface Ticket {
  ticket_id: string;
  created_at: string;
  category: string;
  priority: string;
  problem: string;
  requester: { employee_no: string; name: string; dept: string };
  status: string;
  note: string;
  status_updated_at: string | null;
  status_updated_by: string | null;
  detail?: Record<string, unknown>;
}

export const TICKET_STATUSES = ["未対応", "対応中", "対応済み"];

// ------------------------------------------------------------ 段階3：ファイルの受け渡し・PDF・議事録

export interface WorkFile {
  file_id: string;
  kind: string;
  name: string;
  size: number;
  created_at: string;
  expires_at: string;
}

// ファイルのアップロード。本文にファイルをそのまま送る（multipart は使わない。docs/screens.md）。
// 録画は大きいので、送った量を onProgress（0〜100）で知らせる。そのため fetch ではなく XMLHttpRequest を使う
export function uploadFile(kind: "pdf" | "recording" | "segments" | "hr", file: File, onProgress?: (pct: number) => void): Promise<WorkFile> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/uploads?kind=${kind}&name=${encodeURIComponent(file.name)}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader(CLIENT_HEADER, "web");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    if (onProgress && xhr.upload) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total > 0) onProgress(Math.floor((100 * e.loaded) / e.total));
      };
    }
    xhr.onerror = () => reject(new ApiError(0, "network", "サーバーに接続できません。しばらくしてからやり直してください"));
    xhr.onload = () => {
      let data: any = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        // 本文が JSON でないとき（途中で切れたなど）は、下で共通の文にする
      }
      if (xhr.status >= 200 && xhr.status < 300 && data) {
        onProgress?.(100);
        resolve(data as WorkFile);
        return;
      }
      const err = data?.error;
      const e = new ApiError(xhr.status, err?.code ?? "error", err?.message ?? "アップロードできませんでした。しばらくしてからやり直してください");
      if (xhr.status === 401 && e.code === "not_logged_in") onLoggedOut();
      reject(e);
    };
    xhr.send(file);
  });
}

export const fileUrl = (fileId: string) => `/api/files/${encodeURIComponent(fileId)}`;

export interface InboxFile {
  name: string;
  size: number;
  modified: string;
}

export interface PaperRule {
  name: string;
  templates: string[]; // この様式がある変換先（docx・xlsx）
}

export interface PlanItem {
  index: number;
  file: string;
  rule: string;
  by: string; // 決め方（rule：ルールで / forced：選び直した / llm：LLM が選んだ / none：当たらない）
  fields: Record<string, string>;
  dest: string;
  ocr: boolean;
  error: string | null;
  missing?: string[]; // 名前に使う項目のうち、読み取れずに「不明」にしたもの
}

export interface PlanResult {
  placed: string[];
  renamed?: { file: string; planned: string; placed: string }[]; // 同じ名前があったので、番号を付けて置いたもの
  skipped: { file: string; reason: string }[];
  moved: boolean;
}

export interface Plan {
  plan_id: string;
  created_at: string;
  items: PlanItem[];
  executed_at: string | null;
  result: PlanResult | null;
}

export interface SortRecord {
  time: string;
  source: string;
  dest: string;
  rule: string;
  by: string;
  ocr: string;
  date: string;
  company: string;
  amount: string;
}

export interface MeetingSegment {
  start: number;
  end: number;
  text: string;
  speaker: string;
}

export interface Todo {
  who: string;
  what: string;
  due: string;
}

export interface MeetingSummary {
  summary: string;
  points: string[];
  decisions: string[];
  todos: Todo[];
  next_meeting: string;
}

export interface MeetingListItem {
  meeting_id: string;
  job_id: string | null;
  title: string;
  date: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface Meeting extends MeetingListItem {
  place: string;
  tool: string;
  participants: string[];
  error: string | null;
  segments: MeetingSegment[];
  summary: MeetingSummary | null;
  media_name: string;
  unknown_speakers: number;
  attendees: string[]; // 出席者（会議ツールの画面に出た名前と指定の出席者。画面で直したらその内容）
  summary_note: string | null; // 要約を作れなかったときの文
}

export const MEETING_TOOLS: [string, string][] = [
  ["zoom", "Zoom"], ["teams", "Teams"], ["meet", "Google Meet"], ["none", "録画の画面から読まない（録音など）"],
];

// 仕分けルールの下書き（yanua/paper/rules.py の draft_rule が作る形）。画面で直してから保存する
export interface RuleDraft {
  name: string;
  keywords: { any: string[]; all: string[]; none: string[] };
  fields: Record<string, string>;
  filename: string;
  folder: string;
  priority: number;
}

// 設定のファイル（yanua/settingfiles.py）の今の状態。using：saved（画面で保存した写し）/ default（既定）
export interface SettingStatus {
  using: "saved" | "default";
  default_changed: boolean;
  current_version: number | null;
  default_updated_at: string | null;
}

export interface RuleVersion {
  version: number;
  saved_at: string;
  saved_by: string;
  saved_by_name: string;
  action: string;
}

// ファイル名と保存先で使える項目（yanua/paper/rules.py の BASE_FIELDS）
export const RULE_FIELDS: [string, string][] = [
  ["date", "日付（YYYYMMDD）"], ["yyyy", "年"], ["mm", "月"], ["dd", "日"], ["amount", "金額（数字だけ）"],
  ["company", "相手先"], ["to", "宛先（御中）"], ["rule", "ルール名"], ["original", "元のファイル名"],
];

// 監査ログ（S-55。K-009）
export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  actor_name: string;
  actor_kind: string;   // 社員・受付端末・なし・システム
  action: string;
  target: string;
  result: "成功" | "失敗" | "拒否";
  ip: string;
  detail: string;
  body: string | null;
}

export interface AuditPage {
  entries: AuditEntry[];
  total: number;
  page: number;
  per_page: number;
  actions: string[];
  keep_days: number;
  keep_body: boolean;
}

// 設定の画面（S-52。K-042）
export type SettingKey = "identity" | "audit" | "models" | "files" | "hr" | "access";

export interface SettingSection {
  key: SettingKey;
  label: string;
  status: SettingStatus;
  pinned: Record<string, string>;           // 起動の指定が勝つ項目 → 起動の指定の名前
  values: Record<string, unknown> | null;   // 保存したもの（写しか既定）。読めないときは null
  running: Record<string, unknown>;         // 今動いている値
  applied: boolean;                         // false なら、再起動すると使われる
  problems: { where: string; message: string }[];
}

export interface SettingDetail extends SettingSection {
  meta: {
    presets?: Record<string, { description: string; question: string; clues: string[]; confirm: string[][] }>;
    clues?: Record<string, string>;
    // 目録（config/network.json）にある Ollama のモデル。state が ok のものだけ選べる。message は使えない理由
    catalog?: { name: string; use: string[]; state: "ok" | "not_installed" | "not_registered" | "changed"; message: string }[];
    error?: string;
    limits?: Record<string, { min: number; max: number; unit: string }>;
  };
  versions: RuleVersion[];
  current: number | null;
}

export interface SettingsOverview {
  sections: SettingSection[];
  pending_restart: SettingKey[];
  links: { label: string; path: string; note: string }[];
}

// 通信とモデル（S-53。K-052）
export interface NetHost { host: string; kind: "default" | "added"; purpose: string; new: boolean; on: boolean }
export interface UpdateCheck { mode: "none" | "manual" | "daily" | "weekly"; time: string; weekday: number }
export interface CheckResult { name: string; kind: string; result: string; current: string; latest: string; message: string }
export interface NetworkView {
  hosts: NetHost[];
  new_defaults: { host: string; purpose: string }[];
  removed_defaults: string[];
  error: string;
  update_check: UpdateCheck;
  schedule: string;
  modes: Record<string, string>;
  status: SettingStatus;
  current: number | null;
  versions: RuleVersion[];
  last_check: { checked_at: string; by: string; results: CheckResult[] } | null;
}
export interface ModelRow {
  name: string; kind: "whisper" | "ollama"; use: string[]; revision: string; recorded: string; files?: number;
  state: "match" | "differs" | "not_installed" | "not_registered" | "unverified" | "unknown"; message: string;
  latest: string; check: string; check_message: string; verified_at?: string;
}

// S-54 権限（役割 × 対象 × グループ。docs/access-model.md）
export interface AccessPermission { name: string; feature: string; does: string; targets: string[] }
export interface AccessRole { role_id: string; name: string; description: string; permissions: string[]; used_by: string[] }
export interface AccessMember {
  group_id: string; account_id: string; expires_at: string | null; added_by: string; added_at: string;
  name: string; in_hr: boolean; expired: boolean;
}
export interface AccessGrant {
  grant_id: number; group_id: string; role_id: string; target_id: string | null; set_id: string | null;
  role: string; target: string; target_kind: string;
}
export interface AccessGroup {
  group_id: string; name: string; description: string; rule: { all?: boolean; departments?: string[] } | null;
  how: "rule" | "manual"; rule_text: string; rule_count: number; member_count: number;
  members: AccessMember[]; grants: AccessGrant[];
}
export interface AccessTarget {
  target_id: string; kind: string; name: string; ref: string; description: string; host: string; secret_ref: string;
  kind_name: string; auto: boolean; used: boolean; stale: boolean;
}
export interface AccessSet { set_id: string; name: string; members: string[]; used_by: string[] }
export interface AccessView {
  permissions: AccessPermission[]; roles: AccessRole[]; groups: AccessGroup[]; targets: AccessTarget[];
  target_sets: AccessSet[]; accounts: { account_id: string; kind: string; name: string }[]; departments: string[]; hosts: string[]; title_levels?: Record<string, string>;
}
export interface AccessPerson { account_id: string; name: string; dept: string; kind: "person" | "system" }
export interface AccessPreview {
  detail: string | null; refused: string | null; problems: { where: string; message: string }[];
  self: { gained: string[]; lost: string[] }; self_loss: boolean;
  people: { account_id: string; name: string; dept: string; gained: string[]; lost: string[] }[];
  checked: number; too_many: boolean; groups: { group_id: string; name: string; members: number; gained: string[]; lost: string[] }[];
}
export interface AccessSource { group: string; role: string; target: string; via: string; expires_at: string | null; reason: string }
export interface AccessExplain {
  q: string; candidates: AccessPerson[];
  result: (AccessPerson & { note: string; groups: string[]; permissions: { permission: string; feature: string; does: string; from: AccessSource[] }[] }) | null;
}

// S-50 文書（公開先 × 見るのに要る権限）
export interface AdminDocument {
  doc_id: string; title: string; path: string; updated: string; owner: string; required: string; external: boolean;
  indexed_at: string; source: string; removed_at: string | null; removed_by: string; targets: string[]; target_names: string[];
  decided_by: "file" | "screen";
}
export interface PublishTarget { target_id: string; kind: string; name: string }
export interface AdminDocuments {
  documents: AdminDocument[]; removed: boolean; publish_targets: PublishTarget[];
  permissions: { name: string; does: string }[]; owners: string[];
}
export interface DocImpact {
  doc_id: string; detail: string; checked: number; too_many: boolean; gain: number; lose: number;
  groups: { name: string; members: number; gain: number; lose: number; can_see: number }[];
}

// S-51 人事データ
export type HrStatus = "在籍" | "休職" | "退職";
export interface HrMapping { columns: Record<string, string>; status_values?: Record<string, string>; employment_values?: Record<string, string>; sheet?: string }
export interface HrOverview {
  last: { imported_at: string; source: string; active: number; inactive: number; age_days: number } | null;
  history: { imported_at: string; source: string; kind: string; by: string; added: number; changed: number; removed: number; scope: string }[];
  backups: { backup_id: number; saved_at: string; source: string; total: number; active: number }[];
  mappings: Record<string, HrMapping>;
  problems: { where: string; message: string }[];
  fields: Record<string, string>;
  required: string[];
  statuses: HrStatus[];
  employments: string[];
  leave: { login: boolean; permissions: string };
  can_import: boolean;
  can_view: boolean;
  keep: number;
}
export interface HrInspect { headers: string[]; sheets: string[]; draft: Record<string, string>; status_values: { value: string; guess: string }[]; employment_values?: { value: string; guess: string }[] }
export interface HrPerson { employee_no: string; name: string; dept: string; status: string; changes?: string[] }
export interface HrPreview {
  source: string;
  rows: number;
  errors: { line: number; message: string }[];
  error_count: number;
  read_columns: string[];
  ignored_columns: string[];
  counts: Record<string, number>;
  added: { count: number; first: HrPerson[] };
  changed: { count: number; first: HrPerson[]; by_field: Record<string, number> };
  removed: { count: number; first: HrPerson[] };
  groups: { group_id: string; name: string; join: number; leave: number }[];
  lockout: boolean;
  ok: boolean;
  retired_members: number;
  scope: "partial" | "all";
  scope_label: string;
  current_total: number;
  removal_warning: { count: number; percent: number; message: string } | null;
  committed?: boolean;
  reason?: string;
  kind?: string;
}
export interface HrEmployees {
  employees: { employee_no: string; name: string; dept: string; status: string; changed_at: string }[];
  total: number;
  departments: string[];
}
