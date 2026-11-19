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
    id: "S-13",
    path: "/meetings",
    label: "議事録",
    description: "会議の録画から議事録を作れます。話し手・決まったこと・宿題を直して、Word で出せます。",
    needs: ["meetings"],
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
    needs: ["helpdesk.handle"],
  },
  {
    id: "S-20",
    path: "/paper/sort",
    label: "PDF の仕分け",
    description: "受信箱のスキャンした PDF を、ルールに沿って名前を付けてフォルダに分けます。実行の前に予定を確かめられます。",
    needs: ["paper"],
  },
  {
    id: "S-21",
    path: "/paper/convert",
    label: "PDF の変換",
    description: "PDF を Word・Excel や、会社の様式にします。",
    needs: ["paper"],
  },
  {
    id: "S-50",
    path: "/admin/documents",
    label: "文書",
    description: "文書ごとに、だれに見せるか（公開先と見るのに要る権限）を決められます。取り込み・外す・戻すもここでできます。",
    needs: ["documents.manage"],
  },
  {
    id: "S-52",
    path: "/admin/settings",
    label: "設定",
    description: "本人確認の方針・監査ログ・使うモデル・作業用フォルダの設定を確かめて変えられます。前の版にも戻せます。",
    needs: ["settings.view"],
  },
  {
    id: "S-53",
    path: "/admin/network",
    label: "通信とモデル",
    description: "Yanua が社外に通信してよい宛先と、いつ更新を確かめるか、モデルの版と照合の状態を確かめて変えられます。",
    needs: ["network.view"],
  },
  {
    id: "S-54",
    path: "/admin/access",
    label: "権限",
    description: "グループ・役割・対象で、だれが何をできるかを確かめて変えられます。変える前に、だれの権限が増える・減るかを確かめられます。",
    needs: ["roles.view"],
  },
  {
    id: "S-55",
    path: "/admin/audit",
    label: "監査ログ",
    description: "だれが・いつ・何をしたか（ログイン・権限で断ったこと・ジョブ・設定の変更・ダウンロードなど）を確かめられます。",
    needs: ["audit.view"],
  },
];

export function menuFor(permissions: string[]): MenuItem[] {
  return MENU.filter((m) => m.needs.some((p) => permissions.includes(p)));
}
