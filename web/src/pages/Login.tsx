// S-01 ログイン。今は開発用ログイン（社員番号を入れる）だけ。SSO は K-008 でつなぐ
import { FormEvent, useEffect, useState } from "react";
import { api, ApiError } from "../api";

type Method = { kind: "loading" } | { kind: "dev" } | { kind: "unavailable"; message: string };

export default function Login({ notice, onLoggedIn }: { notice?: string; onLoggedIn: () => void | Promise<void> }) {
  const [method, setMethod] = useState<Method>({ kind: "loading" });
  const [employeeNo, setEmployeeNo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // 「ログアウトしました」などの知らせは、ログインを試したら消す（エラーと並ばないように）
  const [showNotice, setShowNotice] = useState(true);

  useEffect(() => {
    api<{ method: string }>("GET", "/api/auth/login")
      .then((r) =>
        setMethod(r.method === "dev" ? { kind: "dev" } : { kind: "unavailable", message: "ログインの方法を確かめられませんでした" }),
      )
      .catch((e) =>
        setMethod({ kind: "unavailable", message: e instanceof ApiError ? e.message : "ログインの方法を確かめられませんでした" }),
      );
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setShowNotice(false);
    const no = employeeNo.trim();
    if (!no) {
      setError("社員番号を入れてください");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api("POST", "/api/auth/dev-login", { employee_no: no });
      await onLoggedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "ログインできませんでした");
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <section className="card">
        <h1>Yanua にログイン</h1>
        {notice && showNotice && <p className="notice" role="status">{notice}</p>}
        {method.kind === "loading" && <p className="muted">読み込んでいます…</p>}
        {method.kind === "unavailable" && <p className="error" role="alert">{method.message}</p>}
        {method.kind === "dev" && (
          <form onSubmit={submit}>
            <p className="muted small">開発用のログインです。社員番号を入れてください（本番では会社のアカウントでログインします）。</p>
            <label htmlFor="employee_no">社員番号</label>
            <input
              id="employee_no"
              inputMode="numeric"
              autoComplete="username"
              autoFocus
              maxLength={32}
              value={employeeNo}
              onChange={(e) => setEmployeeNo(e.target.value)}
            />
            {error && <p className="error" role="alert">{error}</p>}
            <button type="submit" className="primary" disabled={busy}>
              {busy ? "ログインしています…" : "ログイン"}
            </button>
          </form>
        )}
      </section>
    </main>
  );
}
