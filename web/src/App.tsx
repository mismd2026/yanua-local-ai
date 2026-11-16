import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Me, setLoggedOutHandler } from "./api";
import { navigate, usePath } from "./router";
import Login from "./pages/Login";
import Home from "./pages/Home";
import Chat from "./pages/Chat";
import Jobs from "./pages/Jobs";

type State = { kind: "loading" } | { kind: "out"; notice?: string } | { kind: "in"; me: Me } | { kind: "error"; message: string };

export default function App() {
  const path = usePath();
  const [state, setState] = useState<State>({ kind: "loading" });

  const loadMe = useCallback(async () => {
    try {
      setState({ kind: "in", me: await api<Me>("GET", "/api/me") });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setState({ kind: "out" });
      else setState({ kind: "error", message: e instanceof ApiError ? e.message : "画面を表示できませんでした" });
    }
  }, []);

  useEffect(() => {
    // 操作のない時間が長くてログインが切れたら、どの画面からでもログイン画面に戻す
    setLoggedOutHandler(() => setState({ kind: "out", notice: "ログインの期限が切れました。もう一度ログインしてください" }));
    void loadMe();
  }, [loadMe]);

  // ログイン画面の URL はログインしていないときだけ。ログインしたらホームへ
  useEffect(() => {
    if (state.kind === "out" && path !== "/login") navigate("/login");
    if (state.kind === "in" && path === "/login") navigate("/");
  }, [state.kind, path]);

  const logout = async () => {
    try {
      await api("POST", "/api/auth/logout");
    } finally {
      setState({ kind: "out", notice: "ログアウトしました" });
    }
  };

  if (state.kind === "loading") return <p className="center muted">読み込んでいます…</p>;
  if (state.kind === "error")
    return (
      <div className="center">
        <p role="alert" className="error">{state.message}</p>
        <button onClick={() => void loadMe()}>やり直す</button>
      </div>
    );
  if (state.kind === "out") return <Login notice={state.notice} onLoggedIn={loadMe} />;

  const { me } = state;
  return (
    <div className="layout">
      <header className="topbar">
        <a href="/" className="brand" onClick={(e) => { e.preventDefault(); navigate("/"); }}>Yanua</a>
        <span className="who">
          {me.dept}　{me.name} さん
          <span className="roles" aria-label="役割">{rolesLabel(me.roles)}</span>
        </span>
        <button className="link" onClick={() => void logout()}>ログアウト</button>
      </header>
      <main>
        {path === "/chat" ? <Chat me={me} /> : path === "/jobs" ? <Jobs me={me} /> : path === "/" ? <Home me={me} /> : <NotFound />}
      </main>
    </div>
  );
}

// 全員が持つ「社員」は、ほかの役割があるときは省く（例：情シス・管理者）
export function rolesLabel(roles: string[]): string {
  const extra = roles.filter((r) => r !== "社員");
  return (extra.length ? extra : roles).join("・");
}

function NotFound() {
  return (
    <section className="card">
      <h1>このページはありません</h1>
      <p><a href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }}>ホームに戻る</a></p>
    </section>
  );
}
