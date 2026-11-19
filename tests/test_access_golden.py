"""「前後で同じことができる」テスト（S-54 の移し替え。いちばん大事）。

移し替える前のコード（役割を roles.json と roles.py の PERMISSIONS で決めていたとき）で、デモの人事データの全員について
「ログインできるか・どの API を使えるか・どの文書が見られるか」を書き出したもの（tests/golden/access_before.json）と、
今のコード（役割 × 対象 × グループ）で同じものを作って比べる。

- API の返事の番号・見られる文書・開ける文書・検索で出る文書は、すべて同じでなければならない
- /api/me の権限は、名前を変えたもの（documents.list → documents.view など）をそろえて比べる。
  違ってよいのは INTENDED に書いたものだけ（表で新しく足した権限で、前は使う API がなかったもの）
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from access_snapshot import GOLDEN, snapshot  # noqa: E402

# 意図した違い：社員番号 → 新しく持つ権限（docs/access-model.md の表のとおり。前は使う API がなかった）
INTENDED = {
    "10005": {"roles.view", "roles.assign"},    # 管理者（roles.view は「監査」の役割からも来る）
    "10006": {"hr.view", "hr.import"},          # 人事（S-51 で使う）
}


def test_same_abilities_before_and_after(tmp_path):
    before = json.loads(GOLDEN.read_text(encoding="utf-8"))
    after = json.loads(json.dumps(snapshot(tmp_path)))
    assert set(after) == set(before)
    for no, b in before.items():
        a = after[no]
        assert a["login"] == b["login"], no
        if "api" not in b:
            continue
        diffs = {k: (v, a["api"].get(k)) for k, v in b["api"].items() if a["api"].get(k) != v}
        assert diffs == {}, f"{no} の API の結果が変わりました: {diffs}"
        assert a["documents"] == b["documents"], f"{no} の見られる文書が変わりました"
        added, removed = set(a["permissions"]) - set(b["permissions"]), set(b["permissions"]) - set(a["permissions"])
        assert removed == set(), f"{no} が前に持っていた権限がなくなりました: {removed}"
        assert added == INTENDED.get(no, set()), f"{no} に意図しない権限が足されました: {added}"
