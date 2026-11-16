// S-11 お知らせ。自分あて・部署あての一覧、既読にする
import { useCallback, useEffect, useState } from "react";
import { api, Notification } from "../api";
import { errorText, Status, time } from "../ui";

export default function Notifications() {
  const [onlyUnread, setOnlyUnread] = useState(true);
  const [items, setItems] = useState<Notification[] | null>(null);
  const [unread, setUnread] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api<{ notifications: Notification[]; unread: number }>(
        "GET", onlyUnread ? "/api/notifications?unread=1" : "/api/notifications");
      setItems(r.notifications);
      setUnread(r.unread);
      setError("");
    } catch (e) {
      setError(errorText(e, "お知らせを読み込めませんでした"));
    }
  }, [onlyUnread]);

  useEffect(() => {
    void load();
  }, [load]);

  const read = async (body: { ids?: number[]; all?: boolean }) => {
    setBusy(true);
    try {
      await api("POST", "/api/notifications/read", body);
      await load();
    } catch (e) {
      setError(errorText(e, "既読にできませんでした"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h1 className="page-title">お知らせ</h1>
      <section className="card">
        <div className="row-between">
          <p className="muted" role="status">未読 {unread} 件</p>
          <div className="toolbar">
            <label className="check">
              <input type="checkbox" checked={onlyUnread} onChange={(e) => setOnlyUnread(e.target.checked)} /> 未読だけ
            </label>
            <button onClick={() => void read({ all: true })} disabled={busy || unread === 0}>すべて既読にする</button>
          </div>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {!error && items === null && <p className="muted">読み込んでいます…</p>}
        {items?.length === 0 && <p className="muted">{onlyUnread ? "未読のお知らせはありません。" : "お知らせはありません。"}</p>}
        {items && items.length > 0 && (
          <ul className="notes">
            {items.map((n) => (
              <li key={n.notification_id} className={n.read ? "read" : "unread"}>
                <div className="note-head">
                  <Status s={n.read ? "既読" : "未読"} />
                  <span className="small muted">{n.kind}・{n.to === "あなた" ? "あなたあて" : `${n.to}あて`}・{time(n.created_at)}</span>
                </div>
                <p className="text">{n.message}</p>
                {!n.read && (
                  <button className="link" onClick={() => void read({ ids: [n.notification_id] })} disabled={busy}>
                    既読にする
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
