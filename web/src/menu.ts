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
];

export function menuFor(permissions: string[]): MenuItem[] {
  return MENU.filter((m) => m.needs.some((p) => permissions.includes(p)));
}
