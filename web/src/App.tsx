import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Me, setLoggedOutHandler } from "./api";
import { navigate, usePath } from "./router";
import Login from "./pages/Login";
import Home from "./pages/Home";
import Chat from "./pages/Chat";
import Jobs from "./pages/Jobs";
import Notifications from "./pages/Notifications";
import Reception from "./pages/Reception";
import Tickets from "./pages/Tickets";
import Visits from "./pages/Visits";
import VisitsAll from "./pages/VisitsAll";
import Meetings from "./pages/Meetings";
import PaperConvert from "./pages/PaperConvert";
import PaperSort from "./pages/PaperSort";
import Audit from "./pages/Audit";
import SettingsPage from "./pages/Settings";
import NetworkPage from "./pages/Network";
import AccessPage from "./pages/Access";
import DocumentsPage from "./pages/Documents";

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
        <Page path={path} me={me} />
      </main>
    </div>
  );
}

// 画面の URL → 画面。使えるかどうか（permissions）はメニューと同じ決まりで見る。守りは API の側
function Page({ path, me }: { path: string; me: Me }) {
  const can = (p: string) => me.permissions.includes(p);
  switch (path) {
    case "/":
      return <Home me={me} />;
    case "/chat":
      return <Chat me={me} />;
    case "/jobs":
      return <Jobs me={me} />;
    case "/notifications":
      return can("notifications") ? <Notifications /> : <NoPermission />;
    case "/visits":
      return can("visits.own") ? <Visits /> : <NoPermission />;
    case "/visits/all":
      return can("visits.all") ? <VisitsAll /> : <NoPermission />;
    case "/reception":
      return can("reception.records") ? <Reception /> : <NoPermission />;
    case "/tickets":
      return can("helpdesk.handle") ? <Tickets /> : <NoPermission />;
    case "/meetings":
      return can("meetings") ? <Meetings /> : <NoPermission />;
    case "/paper/sort":
      return can("paper") ? <PaperSort canRules={can("paper.rules")} /> : <NoPermission />;
    case "/paper/convert":
      return can("paper") ? <PaperConvert /> : <NoPermission />;
    case "/admin/settings":
      return can("settings.view") ? <SettingsPage canEdit={can("settings.edit")} /> : <NoPermission />;
    case "/admin/network":
      return can("network.view") ? <NetworkPage canEdit={can("network.edit")} /> : <NoPermission />;
    case "/admin/documents":
      return can("documents.manage") ? <DocumentsPage /> : <NoPermission />;
    case "/admin/access":
      return can("roles.view") ? <AccessPage canEdit={can("roles.assign")} /> : <NoPermission />;
    case "/admin/audit":
      return can("audit.view") ? <Audit /> : <NoPermission />;
    default:
      return <NotFound />;
  }
}

function NoPermission() {
  return (
    <section className="card">
      <h1>この画面を使う権限がありません</h1>
      <p><a href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }}>ホームに戻る</a></p>
    </section>
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
