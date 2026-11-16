// 画面で共通に使う小さな部品
import { ApiError } from "./api";

export function time(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ja-JP");
}

// 状態の印。色だけに頼らず、文字で状態を出す
const TONE: Record<string, string> = {
  完了: "ok", 対応済み: "ok", 既読: "off", 失敗: "ng", 取り消し: "off", 未対応: "ng", 未読: "run",
  待ち: "run", 実行中: "run", 対応中: "run", 受付済み: "ok", 予定: "run", 高: "ng", 中: "run", 低: "off",
};

export function Status({ s }: { s: string }) {
  return <span className={`status ${TONE[s] ?? "run"}`}>{s}</span>;
}

export function errorText(e: unknown, fallback = "処理できませんでした。しばらくしてからやり直してください"): string {
  return e instanceof ApiError ? e.message : fallback;
}

export function todayIso(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
