// ホームのメニュー。出すかどうかは /api/me の permissions で決める（使いやすさのため。守りは API の側）。
// 段階2以降の画面は、できあがったらここに足す（docs/screens.md「4. 画面の一覧」）。

export interface MenuItem {
  id: string;
  path: string;
  label: string;
  description: string;
  needs: string[]; // どれか1つの操作が許されていれば出す
}

export const MENU: MenuItem[] = [
  {
    id: "S-10",
    path: "/chat",
    label: "チャット",
    description: "社内の規程や手順を質問できます。パソコンや機器のお困りごとは、ヘルプデスクに相談できます。",
    needs: ["ask", "helpdesk"],
  },
  {
    id: "S-03",
    path: "/jobs",
    label: "ジョブ",
    description: "文書の取り込みなど、時間のかかる処理の進み具合と結果を見られます。取り消しもできます。",
    needs: ["jobs.own"],
  },
  {
    id: "S-11",
    path: "/notifications",
    label: "お知らせ",
    description: "あなたあて・部署あてのお知らせ（来訪・取次など）を見て、既読にできます。",
    needs: ["notifications"],
  },
  {
    id: "S-12",
    path: "/visits",
    label: "来訪予定",
    description: "お客様の来訪予定を登録して、受付で使うインビテーションコードと案内文を作れます。",
    needs: ["visits.own"],
  },
  {
    id: "S-22",
    path: "/visits/all",
    label: "全社の来訪予定",
    description: "日付ごとに全社の来訪予定を見られます。CSV やカレンダーの書き出しを取り込めます。",
    needs: ["visits.all"],
  },
  {
    id: "S-23",
    path: "/reception",
    label: "受付の記録",
    description: "電話・Web・来訪の受付の記録を見て、対応済みにできます。",
    needs: ["reception.records"],
  },
  {
    id: "S-30",
    path: "/tickets",
    label: "問い合わせ票",
    description: "ヘルプデスクから引き継がれた問い合わせ票を、優先度・状態で絞り込んで対応できます。",
    needs: ["tickets"],
  },
];

export function menuFor(permissions: string[]): MenuItem[] {
  return MENU.filter((m) => m.needs.some((p) => permissions.includes(p)));
}
