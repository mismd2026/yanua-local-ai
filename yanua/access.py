"""権限の仕組み（役割 × 対象 × グループ）。考え方は docs/access-model.md。

- 権限：できる操作の最小単位（yanua/permissions.py。コードで決める）
- 役割：権限の束（roles）
- 対象：権限が効く先（targets）。種類は 部署・グループ・機能・システム・サーバー。まとめたもの（target_sets）。
        特別な対象「メンバー自身の部署」（special:self_dept）と「全社」（special:all）
- グループ：役割 × 対象 を受け取る（group_grants）。権限は人に直接は与えない。入れ子は作らない
- アカウント：グループのメンバー（人 person・システム system）。手で入れたメンバー（group_members。期限つき）と、
        グループの決まり（rule：全員・部署）で自動で入るメンバーを、区別して持つ

判定は can(アカウント, 権限, 対象) の1か所。結果は「はい／いいえ」と理由（どのグループのどの役割から来たか）。
アカウントごとの「持っている権限と、その出どころ」は、変更があったときだけ作り直して持っておく（世代の番号で見分ける）。
期限の切れたメンバーは、判定の時点で数えない。
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import hr
from .permissions import DEFAULT_ROLES, PERMISSIONS

PERSON, SYSTEM = "person", "system"
SELF_DEPT, ALL = "special:self_dept", "special:all"
SPECIAL_NAMES = {SELF_DEPT: "メンバー自身の部署", ALL: "全社"}
TARGET_KINDS = ("department", "group", "feature", "system", "server", "special")
EVERYONE = "全員"     # 決まりの中身：在籍している人の全員
MIGRATED_BY = "（移し替え）"
# システムアカウントの今の使い方（「この人は何ができるか」に出す）。受付端末は、この仕組みへの移し替えがまだ
SYSTEM_NOTES = {"sys:kiosk": "今は端末トークンで /api/kiosk/ だけを使えます（この仕組みへの移し替えはまだ）"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id  TEXT PRIMARY KEY,           -- 人は社員番号、システムは sys:<名前>
    kind        TEXT NOT NULL,              -- person / system
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_groups (
    group_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    rule        TEXT                        -- 自動で入る決まり（JSON）：{"all": true} か {"departments": [...]}
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id    TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    expires_at  TEXT,                       -- 期限（YYYY-MM-DD。その日の終わりまで）。空なら期限なし
    added_by    TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (group_id, account_id)
);
CREATE TABLE IF NOT EXISTS access_roles (
    role_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    permissions TEXT NOT NULL               -- JSON の一覧
);
CREATE TABLE IF NOT EXISTS targets (
    target_id   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT ''    -- 部署名・グループの番号・機能の名前・システムやサーバーの参照名
);
CREATE TABLE IF NOT EXISTS target_sets (
    set_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    members     TEXT NOT NULL               -- JSON：target_id の一覧
);
CREATE TABLE IF NOT EXISTS group_grants (
    grant_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    role_id     TEXT NOT NULL,
    target_id   TEXT,
    set_id      TEXT
);
CREATE TABLE IF NOT EXISTS access_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class AccessError(ValueError):
    """権限の設定の誤り（起動しない）・変更の断り。problems は (位置, 理由)。"""

    def __init__(self, message: str, problems: list[tuple[str, str]] | None = None, status: int = 400):
        super().__init__(message)
        self.problems = problems or [("", message)]
        self.status = status      # 400：書き方の誤り。409：決まりで断った（自分の権限が増える・締め出し・決まりで入るグループ）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(targets)")}
    for col in ("description", "host", "secret_ref"):     # システム・サーバーの対象（S-54 の画面で足した）
        if col not in have:
            db.execute(f"ALTER TABLE targets ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def generation(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT value FROM access_meta WHERE key = 'generation'").fetchone()
    return int(row[0]) if row else 0


def _bump(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO access_meta (key, value) VALUES ('generation', '1') "
               "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")


# ------------------------------------------------------------ 移し替え（初めての起動）


def migrate_if_empty(db: sqlite3.Connection, role_config) -> bool:
    """DB が空なら、今の roles.json（RoleConfig）から役割・グループ・対象を作る。作ったら True。

    社員 → 「社員」（全員）× メンバー自身の部署
    総務 → 「総務」（決まり：roles.json の部署）× 全社
    情シス → 「情シス・ヘルプデスク」（決まり：部署）× 全社。役割は「ヘルプデスク担当」
    人事 → 「人事」× 全社
    管理者 → 「管理者」（roles.json の社員番号は手で入れたメンバー）× 全社。今の動きを変えないため、
            「監査」（audit.view）と「文書管理」（documents.manage。前の documents.ingest）の役割も与える
    「監査」のグループ（メンバーなし）× 全社。IaC の役割は作るが、どのグループにも与えない
    """
    ensure_schema(db)
    if db.execute("SELECT COUNT(*) FROM access_groups").fetchone()[0]:
        return False
    now = _now()
    with db:
        for special, name in SPECIAL_NAMES.items():
            db.execute("INSERT OR IGNORE INTO targets (target_id, kind, name, ref) VALUES (?, 'special', ?, '')", (special, name))
        role_ids = {}
        for i, (name, (desc, perms)) in enumerate(DEFAULT_ROLES.items(), start=1):
            role_ids[name] = f"R{i:02d}"
            db.execute("INSERT INTO access_roles VALUES (?, ?, ?, ?)", (role_ids[name], name, desc, json.dumps(perms)))

        def group(gid, name, desc, rule, grants, members=()):
            if rule and members:
                # 決まりで入るグループは、メンバーを手で出し入れしない。roles.json の社員番号は「（個別）」のグループにする
                group(f"{gid}-manual", f"{name}（個別）", f"{name} と同じ役割。roles.json で社員番号を書いた人", None, grants, members)
                members = ()
            db.execute("INSERT INTO access_groups VALUES (?, ?, ?, ?)", (gid, name, desc, json.dumps(rule, ensure_ascii=False) if rule else None))
            for role in grants:
                db.execute("INSERT INTO group_grants (group_id, role_id, target_id) VALUES (?, ?, ?)",
                           (gid, role_ids[role], SELF_DEPT if role == "社員" else ALL))
            for no in members:
                _ensure_person(db, no)
                db.execute("INSERT OR IGNORE INTO group_members VALUES (?, ?, NULL, ?, ?)", (gid, no, MIGRATED_BY, now))

        def from_config(role):
            depts = sorted(role_config.by_department.get(role, set()))
            return ({"departments": depts} if depts else None), sorted(role_config.by_employee.get(role, set()))

        group("G-employees", "社員", "ログインした人の全員", {"all": True}, ["社員"])
        rule, members = from_config("総務")
        group("G-general", "総務", "総務部（前の役割「総務」）", rule, ["総務"], members)
        rule, members = from_config("情シス")
        group("G-helpdesk", "情シス・ヘルプデスク", "情報システム部（前の役割「情シス」）", rule, ["ヘルプデスク担当"], members)
        rule, members = from_config("人事")
        group("G-hr", "人事", "人事部（前の役割「人事」）", rule, ["人事"], members)
        rule, members = from_config("管理者")
        group("G-admin", "管理者", "前の役割「管理者」。今の動きを変えないため、監査と文書管理の役割も持つ", rule,
              ["管理者", "監査", "文書管理"], members)
        group("G-audit", "監査", "監査ログを見る人（今はメンバーなし）", None, ["監査"])
        # システムアカウント（画面には入らない）。受付端末の端末トークンの移し替えは次の PR 以降
        for aid, name in (("sys:kiosk", "受付端末"), ("sys:update-check", "更新の確認（S-53）")):
            db.execute("INSERT OR IGNORE INTO accounts VALUES (?, 'system', ?, ?)", (aid, name, now))
        _bump(db)
    return True


def _ensure_person(db: sqlite3.Connection, employee_no: str) -> None:
    try:
        person = hr.lookup(db, employee_no)
    except sqlite3.Error:
        person = None             # 人事データをまだ取り込んでいないとき
    db.execute("INSERT OR IGNORE INTO accounts VALUES (?, 'person', ?, ?)",
               (employee_no, (person or {}).get("name") or employee_no, _now()))


# ------------------------------------------------------------ 読み出し・検証


def load(db: sqlite3.Connection) -> dict:
    ensure_schema(db)
    roles = {r[0]: {"role_id": r[0], "name": r[1], "description": r[2], "permissions": json.loads(r[3])}
             for r in db.execute("SELECT role_id, name, description, permissions FROM access_roles ORDER BY rowid")}
    groups = {g[0]: {"group_id": g[0], "name": g[1], "description": g[2], "rule": json.loads(g[3]) if g[3] else None}
              for g in db.execute("SELECT group_id, name, description, rule FROM access_groups ORDER BY rowid")}
    members = [dict(zip(("group_id", "account_id", "expires_at", "added_by", "added_at"), m))
               for m in db.execute("SELECT group_id, account_id, expires_at, added_by, added_at FROM group_members ORDER BY group_id, account_id")]
    targets = {t[0]: {"target_id": t[0], "kind": t[1], "name": t[2], "ref": t[3], "description": t[4], "host": t[5],
                      "secret_ref": t[6]}
               for t in db.execute("SELECT target_id, kind, name, ref, description, host, secret_ref FROM targets ORDER BY rowid")}
    sets = {s[0]: {"set_id": s[0], "name": s[1], "members": json.loads(s[2])}
            for s in db.execute("SELECT set_id, name, members FROM target_sets ORDER BY set_id")}
    grants = [dict(zip(("grant_id", "group_id", "role_id", "target_id", "set_id"), g))
              for g in db.execute("SELECT grant_id, group_id, role_id, target_id, set_id FROM group_grants ORDER BY grant_id")]
    accounts = {a[0]: {"account_id": a[0], "kind": a[1], "name": a[2]}
                for a in db.execute("SELECT account_id, kind, name FROM accounts ORDER BY account_id")}
    return {"roles": roles, "groups": groups, "members": members, "targets": targets, "sets": sets, "grants": grants,
            "accounts": accounts}


def problems(data: dict) -> list[tuple[str, str]]:
    """権限の設定の誤り（あれば起動しない）。"""
    out = []
    for rid, r in data["roles"].items():
        if not isinstance(r["permissions"], list):
            out.append((f"roles.{r['name']}", "権限は一覧にしてください"))
            continue
        for p in r["permissions"]:
            if p not in PERMISSIONS:
                out.append((f"roles.{r['name']}", f"知らない権限です: {p}"))
    for gid, g in data["groups"].items():
        rule = g["rule"]
        if rule is not None and not (isinstance(rule, dict) and (rule == {"all": True} or (
                set(rule) == {"departments"} and isinstance(rule["departments"], list)
                and all(isinstance(d, str) and d for d in rule["departments"])))):
            out.append((f"groups.{g['name']}", "自動で入る決まりは {\"all\": true} か {\"departments\": [...]} にしてください"))
    for gr in data["grants"]:
        at = f"grants[{gr['grant_id']}]"
        if gr["group_id"] not in data["groups"]:
            out.append((at, f"ないグループです: {gr['group_id']}"))
        if gr["role_id"] not in data["roles"]:
            out.append((at, f"ない役割です: {gr['role_id']}"))
        if bool(gr["target_id"]) == bool(gr["set_id"]):
            out.append((at, "対象か、対象のまとまりのどちらか1つを決めてください"))
        elif gr["target_id"] and gr["target_id"] not in data["targets"]:
            out.append((at, f"ない対象です: {gr['target_id']}"))
        elif gr["set_id"] and gr["set_id"] not in data["sets"]:
            out.append((at, f"ない対象のまとまりです: {gr['set_id']}"))
    for s in data["sets"].values():
        for t in s["members"]:
            if t not in data["targets"]:
                out.append((f"target_sets.{s['name']}", f"ない対象です: {t}"))
    for t in data["targets"].values():
        if t["kind"] not in TARGET_KINDS:
            out.append((f"targets.{t['name']}", f"知らない種類です: {t['kind']}"))
    for m in data["members"]:
        at = f"members.{m['group_id']}.{m['account_id']}"
        if m["group_id"] not in data["groups"]:
            out.append((at, "ないグループのメンバーです"))
        if m["expires_at"]:
            try:
                datetime.fromisoformat(m["expires_at"])
            except ValueError:
                out.append((at, "期限は YYYY-MM-DD で書いてください"))
    return out


# ------------------------------------------------------------ 判定


@dataclass
class Grant:
    """ある人が持つ1つの権限の出どころ。"""
    permission: str
    group: str
    role: str
    target: str            # target_id（特別な対象・部署など）
    target_kind: str
    target_ref: str
    target_name: str
    via: str               # 「手で入れた」「決まり：…」
    expires_at: str | None = None

    def reason(self) -> str:
        how = self.via + (f"、期限 {self.expires_at}" if self.expires_at else "")
        return f"グループ「{self.group}」（{how}）の役割「{self.role}」× {self.target_label()}"

    def target_label(self) -> str:
        return SPECIAL_NAMES.get(self.target, self.target_name or self.target)


@dataclass
class Decision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def _expired(expires_at: str | None, today: str) -> bool:
    return bool(expires_at) and expires_at[:10] < today


def _rule_match(rule: dict | None, dept: str) -> str | None:
    if not rule:
        return None
    if rule.get("all"):
        return f"決まり：{EVERYONE}"
    if dept and dept in rule.get("departments", []):
        return f"決まり：部署「{dept}」"
    return None


def compute_grants(data: dict, account_id: str, dept: str) -> list[Grant]:
    """その人が持つ権限と出どころ（データだけから決める。変更を試すときにも使う）。期限の切れたものも入っている。"""
    result = []
    manual = {m["group_id"]: m for m in data["members"] if m["account_id"] == account_id}
    for gid, g in data["groups"].items():
        via, expires = None, None
        rule = _rule_match(g["rule"], dept) if not account_id.startswith("sys:") else None
        if rule:
            via = rule
        if gid in manual:
            if via is None or manual[gid]["expires_at"] is None:
                via, expires = ("手で入れた", manual[gid]["expires_at"]) if via is None else (via, None)
        if via is None:
            continue
        for gr in data["grants"]:
            if gr["group_id"] != gid or gr["role_id"] not in data["roles"]:
                continue
            role = data["roles"][gr["role_id"]]
            tids = [gr["target_id"]] if gr["target_id"] else data["sets"].get(gr["set_id"], {}).get("members", [])
            for tid in tids:
                t = data["targets"].get(tid)
                if not t:
                    continue
                for p in role["permissions"]:
                    result.append(Grant(p, g["name"], role["name"], tid, t["kind"], t["ref"], t["name"], via, expires))
    return result


class Engine:
    """判定の1か所。アカウントごとの結果を、変更の世代が同じあいだ持っておく。"""

    def __init__(self, connect, today=None):
        self._connect = connect
        self._today = today or (lambda: datetime.now().astimezone().date().isoformat())
        self._cache: dict[tuple[str, str], tuple[int, list[Grant]]] = {}
        self._data: tuple[int, dict] | None = None
        self._lock = threading.Lock()

    def _load(self, db) -> tuple[int, dict]:
        gen = generation(db)
        if self._data is None or self._data[0] != gen:
            self._data = (gen, load(db))
            self._cache.clear()
        return self._data

    def grants_for(self, account_id: str, dept: str) -> list[Grant]:
        """その人が持つ権限と出どころ（期限は判定のときに見るので、期限の切れたものも入っている）。"""
        with self._lock, self._connect() as db:
            gen, data = self._load(db)
            hit = self._cache.get((account_id, dept))
            if hit and hit[0] == gen:
                return hit[1]
            result = compute_grants(data, account_id, dept)
            self._cache[(account_id, dept)] = (gen, result)
            return result

    def _live(self, account_id: str, dept: str) -> list[Grant]:
        today = self._today()
        return [g for g in self.grants_for(account_id, dept) if not _expired(g.expires_at, today)]

    def can(self, account_id: str, dept: str, permission: str, target: tuple[str, str] | None = None) -> Decision:
        """target は (種類, 参照)（例 ("department", "営業部")）。None なら「どこかに対して持っているか」（機能を使えるか）。"""
        reasons = []
        for g in self._live(account_id, dept):
            if g.permission != permission:
                continue
            if target is None or g.target == ALL or (g.target == SELF_DEPT and target == ("department", dept)) \
                    or (g.target_kind == target[0] and g.target_ref == target[1]):
                reasons.append(g.reason())
        return Decision(bool(reasons), reasons)

    def permissions(self, account_id: str, dept: str) -> list[str]:
        have = {g.permission for g in self._live(account_id, dept)}
        return [p for p in PERMISSIONS if p in have]

    def departments(self, account_id: str, dept: str, permission: str):
        """その権限が効く部署の集まり。全社なら ALL。"""
        out = set()
        for g in self._live(account_id, dept):
            if g.permission != permission:
                continue
            if g.target == ALL:
                return ALL
            if g.target == SELF_DEPT and dept:
                out.add(dept)
            elif g.target_kind == "department":
                out.add(g.target_ref)
        return out

    def groups_of(self, account_id: str, dept: str) -> list[str]:
        seen = []
        for g in self._live(account_id, dept):
            if g.group not in seen:
                seen.append(g.group)
        return seen

    def roles_for(self, employee_no: str, dept: str) -> list[str]:
        """入っているグループの名前（/api/me の roles に出す）。"""
        return self.groups_of(employee_no, dept)


# ------------------------------------------------------------ 対象をそろえる（部署・グループ・機能は自動）


AUTO_KINDS = ("department", "group", "feature")
MANUAL_KINDS = {"system": "システム", "server": "サーバー"}


def _hr_departments(db) -> list[str]:
    try:
        return sorted({d for (d,) in db.execute("SELECT dept FROM hr_departments")}
                      | {d for (d,) in db.execute("SELECT DISTINCT dept FROM employees WHERE active = 1")})
    except sqlite3.Error:
        return []


def _used_targets(data: dict) -> set[str]:
    used = {g["target_id"] for g in data["grants"] if g["target_id"]}
    for s in data["sets"].values():
        used.update(s["members"])
    return used


def sync_targets(db: sqlite3.Connection) -> bool:
    """部署（人事データ）・グループ・機能（permissions.py）の対象を、手で作らずにそろえる。変えたら True。
    なくなった部署・グループの対象は、どこにも使っていなければ消す。使っていれば残す（画面で「なくなった」と出す）。"""
    data = load(db)
    want: dict[str, tuple[str, str, str]] = {}
    for d in _hr_departments(db):
        want[f"dept:{d}"] = ("department", d, d)
    for gid, g in data["groups"].items():
        want[f"group:{gid}"] = ("group", g["name"], gid)
    for feature in dict.fromkeys(p.feature for p in PERMISSIONS.values()):
        want[f"feature:{feature}"] = ("feature", feature, feature)
    used = _used_targets(data)
    changed = False
    with db:
        for tid, (kind, name, ref) in want.items():
            t = data["targets"].get(tid)
            if t is None:
                db.execute("INSERT INTO targets (target_id, kind, name, ref) VALUES (?, ?, ?, ?)", (tid, kind, name, ref))
                changed = True
            elif (t["kind"], t["name"], t["ref"]) != (kind, name, ref):
                db.execute("UPDATE targets SET kind = ?, name = ?, ref = ? WHERE target_id = ?", (kind, name, ref, tid))
                changed = True
        for tid, t in data["targets"].items():
            if t["kind"] in AUTO_KINDS and tid not in want and tid not in used:
                db.execute("DELETE FROM targets WHERE target_id = ?", (tid,))
                changed = True
        if changed:
            _bump(db)
    return changed


def stale_targets(db, data: dict) -> set[str]:
    """自動の対象のうち、もとの部署・グループがなくなったもの（使っているので残している）。"""
    depts = set(_hr_departments(db))
    out = set()
    for tid, t in data["targets"].items():
        if (t["kind"] == "department" and t["ref"] not in depts) or (t["kind"] == "group" and t["ref"] not in data["groups"]):
            out.add(tid)
    return out


# ------------------------------------------------------------ 人を探す


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_people(db, data: dict, q: str, limit: int = 20) -> list[dict]:
    """社員番号（前の部分）か名前の一部で、在籍している人を探す。システムアカウントも名前や番号で出す。"""
    q = (q or "").strip()[:50]
    if not q:
        return []
    out = [{"account_id": a["account_id"], "name": a["name"], "dept": "", "kind": SYSTEM}
           for a in data["accounts"].values()
           if a["kind"] == SYSTEM and (q.lower() in a["account_id"].lower() or q in a["name"])]
    key = hr.name_key(q)
    try:
        rows = db.execute(
            "SELECT employee_no, name, dept FROM employees WHERE active = 1 AND (employee_no LIKE ? ESCAPE '\\' "
            "OR name LIKE ? ESCAPE '\\' OR (? <> '' AND name_key LIKE ? ESCAPE '\\')) ORDER BY employee_no LIMIT ?",
            (_like(q) + "%", "%" + _like(q) + "%", key, "%" + _like(key) + "%", limit)).fetchall()
    except sqlite3.Error:
        rows = []
    out += [{"account_id": r[0], "name": r[1], "dept": r[2], "kind": PERSON} for r in rows]
    return out[:limit]


def account_of(db, data: dict, account_id: str) -> dict | None:
    """アカウント（在籍している人か、システムアカウント）。"""
    if account_id.startswith("sys:"):
        a = data["accounts"].get(account_id)
        return {"account_id": account_id, "name": a["name"], "dept": "", "kind": SYSTEM} if a else None
    person = hr.lookup(db, account_id)
    return {"account_id": person["employee_no"], "name": person["name"], "dept": person["dept"], "kind": PERSON} if person else None


# ------------------------------------------------------------ 変更（roles.assign。API と復旧用のコマンドから）


FOREVER = "9999-12-31"
IMPACT_LIMIT = 200          # これより多くの人に効く変更は、一人ずつではなく、グループごとに出す
SECRET_REF = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
NAME_MAX = 40
CHANGE_LOCK = threading.Lock()   # 変更は1つずつ（読む → 試す → 書くのあいだに、ほかの変更を入れない）


@dataclass
class Change:
    """1つの変更。after（変えたあとのデータ）で試しに当て、write で保存する。"""
    action: str                 # 監査ログの操作の名前（roles.member.add など）
    target: str                 # 監査ログの対象
    detail: str                 # 変えた中身（監査ログと画面に出す）
    after: dict
    write: Callable[[sqlite3.Connection], None]
    groups: set[str] = field(default_factory=set)   # roles.assign が効いている要があるグループ（API で確かめる）


def _effect(data: dict, account_id: str, dept: str, today: str) -> dict[tuple[str, str], str]:
    """(権限, 対象) → いつまで効くか（期限なしは FOREVER）。期限の切れたものは入れない。"""
    out: dict[tuple[str, str], str] = {}
    for g in compute_grants(data, account_id, dept):
        if _expired(g.expires_at, today):
            continue
        until = (g.expires_at or FOREVER)[:10]
        key = (g.permission, g.target)
        out[key] = max(out.get(key, ""), until)
    return out


def _active_people(db) -> list[tuple[str, str]]:
    try:
        return [(r[0], r[1]) for r in db.execute("SELECT employee_no, dept FROM employees WHERE active = 1")]
    except sqlite3.Error:
        return []


def _assign_holders(db, data: dict, today: str) -> list[str]:
    """roles.assign を持つ人（在籍している人だけ。システムアカウントは数えない）。"""
    return [no for no, dept in _active_people(db)
            if any(p == "roles.assign" for (p, _t) in _effect(data, no, dept, today))]


def guard(db, engine: "Engine", actor: str, actor_dept: str, before: dict, after: dict) -> None:
    """変更の決まり（docs/access-model.md）：
    - 自分の権限が増える変更はできない（自分のグループに役割・対象を足す・自分を入れる・自分の期限を延ばす・自分の役割に権限を足す）
    - 権限が減る変更と、自分に効かない変更はできる
    - roles.assign を持つ人が1人もいなくなる変更はできない（締め出しを防ぐ）"""
    today = engine._today()
    now, then = _effect(before, actor, actor_dept, today), _effect(after, actor, actor_dept, today)
    gained = sorted(p for key, until in then.items() if until > now.get(key, "") for p in [key[0]])
    if gained:
        raise AccessError(f"自分の権限が増える変更はできません（増える権限：{'・'.join(dict.fromkeys(gained))}）",
                          [("", "自分の権限が増える変更です。ほかの roles.assign を持つ人に頼んでください")], status=409)
    if not _assign_holders(db, after, today):
        raise AccessError("roles.assign を持つ人が1人もいなくなるので、この変更はできません（締め出しを防ぐため）",
                          [("", "roles.assign を持つ人が1人もいなくなります")], status=409)


def apply(db, engine: "Engine", actor: str | None, actor_dept: str, before: dict, change: Change) -> str:
    """変更を保存する。actor が None のときは復旧用のコマンド（サーバーの操作）：「自分」がいないので、自分の権限と締め出しの決まりは当てない。"""
    if actor is not None:
        guard(db, engine, actor, actor_dept, before, change.after)
    with db:
        change.write(db)
        _bump(db)
    if change.action.startswith("roles.group."):
        sync_targets(db)            # グループの対象をそろえる
    return change.detail


def _fail(message: str, where: str = "") -> AccessError:
    return AccessError(message, [(where, message)])


def _group(data: dict, group_id: str) -> dict:
    g = data["groups"].get(group_id)
    if not g:
        raise KeyError(group_id)
    return g


def _rule_text(rule: dict | None) -> str:
    if not rule:
        return "手で入れる"
    return EVERYONE if rule.get("all") else f"部署「{'・'.join(rule.get('departments', []))}」"


def _no_manual_for_rule_group(g: dict) -> None:
    if g["rule"]:
        raise AccessError(f"グループ「{g['name']}」は決まり（{_rule_text(g['rule'])}）で入るグループなので、メンバーを手で出し入れできません",
                          [("account", "決まりで入るグループです")], status=409)


def _check_name(value: str, taken: set[str], what: str) -> str:
    name = (value or "").strip()
    if not name:
        raise _fail(f"{what}の名前を入れてください", "name")
    if len(name) > NAME_MAX:
        raise _fail(f"{what}の名前は {NAME_MAX} 文字までにしてください", "name")
    if name in taken:
        raise _fail(f"同じ名前の{what}があります：{name}", "name")
    return name


def _target_name(data: dict, target_id: str | None, set_id: str | None) -> str:
    if target_id:
        return data["targets"][target_id]["name"]
    return f"まとまり「{data['sets'][set_id]['name']}」"


def _groups_using(data: dict, *, role_id: str | None = None, set_id: str | None = None) -> set[str]:
    return {g["group_id"] for g in data["grants"]
            if (role_id and g["role_id"] == role_id) or (set_id and g["set_id"] == set_id)}


def _rule_count(db, rule: dict | None) -> int:
    """決まりで入る人の数（在籍している人）。"""
    if not rule:
        return 0
    try:
        if rule.get("all"):
            return db.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0]
        depts = rule.get("departments", [])
        if not depts:
            return 0
        return db.execute(f"SELECT COUNT(*) FROM employees WHERE active = 1 AND dept IN ({','.join('?' * len(depts))})",
                          depts).fetchone()[0]
    except sqlite3.Error:
        return 0


def _check_expiry(expires_at: str | None) -> str | None:
    if not expires_at:
        return None
    try:
        return date.fromisoformat(expires_at).isoformat()
    except ValueError as e:
        raise AccessError("期限は YYYY-MM-DD で書いてください", [("expires_at", "期限は YYYY-MM-DD で書いてください")]) from e


# --- メンバー


def member_add(db, data: dict, by: str, group_id: str, account_id: str, expires_at: str | None) -> Change:
    g = _group(data, group_id)
    _no_manual_for_rule_group(g)
    expires_at = _check_expiry(expires_at)
    account_id = (account_id or "").strip()
    who = account_of(db, data, account_id)
    if not who:
        if account_id.startswith("sys:"):
            raise AccessError("そのシステムアカウントはありません", [("account", "ないアカウントです")])
        raise AccessError("その社員番号の人は、人事データにいません（在籍している人だけ入れられます）", [("account", "人事データにいません")])
    after = copy.deepcopy(data)
    after["members"] = [m for m in after["members"] if not (m["group_id"] == group_id and m["account_id"] == account_id)]
    after["members"].append({"group_id": group_id, "account_id": account_id, "expires_at": expires_at,
                             "added_by": by, "added_at": _now()})

    def write(db):
        if who["kind"] == PERSON:
            _ensure_person(db, account_id)
        db.execute("INSERT INTO group_members VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id, account_id) "
                   "DO UPDATE SET expires_at = excluded.expires_at, added_by = excluded.added_by, added_at = excluded.added_at",
                   (group_id, account_id, expires_at, by, _now()))

    detail = f"{g['name']} に {who['name']} を追加" + (f"（期限 {expires_at}）" if expires_at else "（期限なし）")
    return Change("roles.member.add", group_id, detail, after, write, {group_id})


def member_remove(db, data: dict, by: str, group_id: str, account_id: str) -> Change:
    g = _group(data, group_id)
    _no_manual_for_rule_group(g)
    if not any(m["group_id"] == group_id and m["account_id"] == account_id for m in data["members"]):
        raise KeyError(account_id)
    name = (data["accounts"].get(account_id) or {}).get("name") or account_id
    after = copy.deepcopy(data)
    after["members"] = [m for m in after["members"] if not (m["group_id"] == group_id and m["account_id"] == account_id)]

    def write(db):
        db.execute("DELETE FROM group_members WHERE group_id = ? AND account_id = ?", (group_id, account_id))

    return Change("roles.member.remove", group_id, f"{g['name']} から {name} を外した（手で入れたメンバー）", after, write, {group_id})


# --- 役割 × 対象 を与える


def grant_add(db, data: dict, by: str, group_id: str, role_id: str, target_id: str | None, set_id: str | None) -> Change:
    g = _group(data, group_id)
    target_id, set_id = (target_id or None), (set_id or None)
    new = {"grant_id": "new", "group_id": group_id, "role_id": role_id, "target_id": target_id, "set_id": set_id}
    trial = {**data, "grants": [*data["grants"], new]}
    found = [p for p in problems(trial) if p[0] == "grants[new]"]
    if found:
        raise AccessError("役割の与え方に直すところがあります：" + "／".join(m for _, m in found), found)
    if any(x["group_id"] == group_id and x["role_id"] == role_id and x["target_id"] == target_id and x["set_id"] == set_id
           for x in data["grants"]):
        raise _fail("同じ役割と対象を、もう与えています", "role_id")

    def write(db):
        db.execute("INSERT INTO group_grants (group_id, role_id, target_id, set_id) VALUES (?, ?, ?, ?)",
                   (group_id, role_id, target_id, set_id))

    detail = f"{g['name']} に 役割「{data['roles'][role_id]['name']}」× {_target_name(data, target_id, set_id)} を与えた"
    return Change("roles.grant.add", group_id, detail, trial, write, {group_id})


def grant_remove(db, data: dict, by: str, grant_id: int) -> Change:
    gr = next((x for x in data["grants"] if x["grant_id"] == grant_id), None)
    if not gr:
        raise KeyError(grant_id)
    after = {**data, "grants": [x for x in data["grants"] if x["grant_id"] != grant_id]}

    def write(db):
        db.execute("DELETE FROM group_grants WHERE grant_id = ?", (grant_id,))

    detail = (f"{data['groups'][gr['group_id']]['name']} から 役割「{data['roles'][gr['role_id']]['name']}」"
              f"× {_target_name(data, gr['target_id'], gr['set_id'])} を外した")
    return Change("roles.grant.remove", gr["group_id"], detail, after, write, {gr["group_id"]})


# --- 役割


def _check_permissions(permissions) -> list[str]:
    if not isinstance(permissions, list):
        raise _fail("権限は一覧にしてください", "permissions")
    bad = [p for p in permissions if p not in PERMISSIONS]
    if bad:
        raise AccessError("知らない権限があります", [("permissions", f"知らない権限です: {p}") for p in bad])
    return [p for p in PERMISSIONS if p in set(permissions)]


def role_update(db, data: dict, by: str, role_id: str, permissions: list[str], description: str | None = None) -> Change:
    r = data["roles"].get(role_id)
    if not r:
        raise KeyError(role_id)
    new = _check_permissions(permissions)
    desc = r["description"] if description is None else description.strip()[:200]
    after = copy.deepcopy(data)
    after["roles"][role_id].update(permissions=new, description=desc)

    def write(db):
        db.execute("UPDATE access_roles SET permissions = ?, description = ? WHERE role_id = ?", (json.dumps(new), desc, role_id))

    added = [p for p in new if p not in r["permissions"]]
    removed = [p for p in r["permissions"] if p not in new]
    parts = ([f"{'・'.join(added)} を足した"] if added else []) + ([f"{'・'.join(removed)} を外した"] if removed else [])
    if desc != r["description"]:
        parts.append(f"説明を「{desc}」にした")
    return Change("roles.role.update", role_id, f"役割「{r['name']}」の権限：" + ("、".join(parts) or "変更なし"),
                  after, write, _groups_using(data, role_id=role_id))


def role_create(db, data: dict, by: str, name: str, description: str = "", permissions: list[str] | None = None) -> Change:
    name = _check_name(name, {r["name"] for r in data["roles"].values()}, "役割")
    perms = _check_permissions(permissions or [])
    n = max([int(rid[1:]) for rid in data["roles"] if rid[1:].isdigit()] + [0]) + 1
    role_id = f"R{n:02d}"
    desc = (description or "").strip()[:200]
    after = copy.deepcopy(data)
    after["roles"][role_id] = {"role_id": role_id, "name": name, "description": desc, "permissions": perms}

    def write(db):
        db.execute("INSERT INTO access_roles VALUES (?, ?, ?, ?)", (role_id, name, desc, json.dumps(perms)))

    detail = f"役割「{name}」を作った（権限：{'・'.join(perms) or 'なし'}）"
    return Change("roles.role.create", role_id, detail, after, write)


def role_delete(db, data: dict, by: str, role_id: str) -> Change:
    r = data["roles"].get(role_id)
    if not r:
        raise KeyError(role_id)
    if _groups_using(data, role_id=role_id):
        names = "・".join(data["groups"][g]["name"] for g in _groups_using(data, role_id=role_id))
        raise AccessError(f"役割「{r['name']}」はグループ（{names}）に与えているので消せません。先にグループから外してください",
                          [("role", "グループに与えています")], status=409)
    after = copy.deepcopy(data)
    del after["roles"][role_id]

    def write(db):
        db.execute("DELETE FROM access_roles WHERE role_id = ?", (role_id,))

    return Change("roles.role.delete", role_id, f"役割「{r['name']}」を消した", after, write)


# --- グループ


def _check_rule(db, rule) -> dict | None:
    if rule in (None, {}):
        return None
    if rule == {"all": True}:
        return rule
    if isinstance(rule, dict) and set(rule) == {"departments"} and isinstance(rule["departments"], list):
        depts = [d.strip() for d in rule["departments"] if isinstance(d, str) and d.strip()]
        if not depts:
            raise _fail("決まりで入る部署を1つ以上選んでください", "rule")
        known = set(_hr_departments(db))
        unknown = [d for d in depts if d not in known]
        if unknown:
            raise _fail(f"人事データにない部署です：{'・'.join(unknown)}", "rule")
        return {"departments": list(dict.fromkeys(depts))}
    raise _fail("入り方は「手で入れる」「全員」「部署」のどれかにしてください", "rule")


def group_create(db, data: dict, by: str, name: str, description: str = "", rule=None) -> Change:
    name = _check_name(name, {g["name"] for g in data["groups"].values()}, "グループ")
    rule = _check_rule(db, rule)
    desc = (description or "").strip()[:200]
    gid = "G-" + uuid.uuid4().hex[:8]
    after = copy.deepcopy(data)
    after["groups"][gid] = {"group_id": gid, "name": name, "description": desc, "rule": rule}

    def write(db):
        db.execute("INSERT INTO access_groups VALUES (?, ?, ?, ?)",
                   (gid, name, desc, json.dumps(rule, ensure_ascii=False) if rule else None))

    return Change("roles.group.create", gid, f"グループ「{name}」を作った（入り方：{_rule_text(rule)}）", after, write)


def group_update(db, data: dict, by: str, group_id: str, description: str) -> Change:
    g = _group(data, group_id)
    desc = (description or "").strip()[:200]
    after = copy.deepcopy(data)
    after["groups"][group_id]["description"] = desc

    def write(db):
        db.execute("UPDATE access_groups SET description = ? WHERE group_id = ?", (desc, group_id))

    return Change("roles.group.update", group_id, f"グループ「{g['name']}」の説明を「{desc}」にした", after, write, {group_id})


def group_delete(db, data: dict, by: str, group_id: str) -> Change:
    g = _group(data, group_id)
    why = []
    if any(m["group_id"] == group_id for m in data["members"]):
        why.append("手で入れたメンバーがいます")
    if g["rule"] and _rule_count(db, g["rule"]):
        why.append(f"決まり（{_rule_text(g['rule'])}）で入る人がいます")
    if any(x["group_id"] == group_id for x in data["grants"]):
        why.append("役割を与えています")
    if f"group:{group_id}" in _used_targets(data):
        why.append("ほかのグループの役割の対象になっています")
    if why:
        raise AccessError(f"グループ「{g['name']}」は消せません（{'・'.join(why)}）。メンバーも役割もないグループだけ消せます",
                          [("group", w) for w in why], status=409)
    after = copy.deepcopy(data)
    del after["groups"][group_id]

    def write(db):
        db.execute("DELETE FROM access_groups WHERE group_id = ?", (group_id,))
        db.execute("DELETE FROM targets WHERE target_id = ?", (f"group:{group_id}",))

    return Change("roles.group.delete", group_id, f"グループ「{g['name']}」を消した", after, write, {group_id})


# --- 対象（システム・サーバーは手で作る）と、対象のまとまり


def _check_target_fields(description: str, host: str, secret_ref: str, hosts: list[str]) -> tuple[str, str, str]:
    host, secret_ref = (host or "").strip(), (secret_ref or "").strip()
    if host and host not in hosts:
        raise _fail("つながる宛先は、通信の設定（S-53）の宛先から選んでください", "host")
    if secret_ref and not SECRET_REF.match(secret_ref):
        raise _fail("接続情報の参照名は、環境変数の名前（英大文字・数字・_、英大文字で始める）だけを書いてください。値は書きません",
                    "secret_ref")
    return (description or "").strip()[:200], host, secret_ref


def target_create(db, data: dict, by: str, kind: str, name: str, description: str, host: str, secret_ref: str,
                  hosts: list[str]) -> Change:
    if kind not in MANUAL_KINDS:
        raise _fail("手で作れる対象は、システムとサーバーだけです（部署・グループ・機能は自動でそろえます）", "kind")
    name = _check_name(name, {t["name"] for t in data["targets"].values() if t["kind"] == kind}, MANUAL_KINDS[kind])
    desc, host, secret_ref = _check_target_fields(description, host, secret_ref, hosts)
    tid = f"{kind}:{uuid.uuid4().hex[:8]}"
    after = copy.deepcopy(data)
    after["targets"][tid] = {"target_id": tid, "kind": kind, "name": name, "ref": tid, "description": desc,
                             "host": host, "secret_ref": secret_ref}

    def write(db):
        db.execute("INSERT INTO targets (target_id, kind, name, ref, description, host, secret_ref) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (tid, kind, name, tid, desc, host, secret_ref))

    detail = (f"{MANUAL_KINDS[kind]}「{name}」を作った（宛先：{host or 'なし'}、接続情報の参照名：{secret_ref or 'なし'}）")
    return Change("roles.target.create", tid, detail, after, write)


def target_update(db, data: dict, by: str, target_id: str, description: str, host: str, secret_ref: str,
                  hosts: list[str]) -> Change:
    t = data["targets"].get(target_id)
    if not t:
        raise KeyError(target_id)
    if t["kind"] not in MANUAL_KINDS:
        raise _fail("部署・グループ・機能の対象は、自動でそろえるので直せません", "kind")
    desc, host, secret_ref = _check_target_fields(description, host, secret_ref, hosts)
    after = copy.deepcopy(data)
    after["targets"][target_id].update(description=desc, host=host, secret_ref=secret_ref)

    def write(db):
        db.execute("UPDATE targets SET description = ?, host = ?, secret_ref = ? WHERE target_id = ?",
                   (desc, host, secret_ref, target_id))

    detail = f"{MANUAL_KINDS[t['kind']]}「{t['name']}」を直した（宛先：{host or 'なし'}、接続情報の参照名：{secret_ref or 'なし'}）"
    return Change("roles.target.update", target_id, detail, after, write)


def target_delete(db, data: dict, by: str, target_id: str) -> Change:
    t = data["targets"].get(target_id)
    if not t:
        raise KeyError(target_id)
    if t["kind"] not in MANUAL_KINDS:
        raise _fail("部署・グループ・機能の対象は、自動でそろえるので消せません", "kind")
    if target_id in _used_targets(data):
        raise AccessError(f"「{t['name']}」は役割の対象か、対象のまとまりに使っているので消せません", [("target", "使っています")], status=409)
    after = copy.deepcopy(data)
    del after["targets"][target_id]

    def write(db):
        db.execute("DELETE FROM targets WHERE target_id = ?", (target_id,))

    return Change("roles.target.delete", target_id, f"{MANUAL_KINDS[t['kind']]}「{t['name']}」を消した", after, write)


def _check_set_members(data: dict, members) -> list[str]:
    if not isinstance(members, list) or not members:
        raise _fail("まとまりに入れる対象を1つ以上選んでください", "members")
    bad = [m for m in members if m not in data["targets"]]
    if bad:
        raise _fail(f"ない対象です：{'・'.join(map(str, bad))}", "members")
    return list(dict.fromkeys(members))


def set_create(db, data: dict, by: str, name: str, members: list[str]) -> Change:
    name = _check_name(name, {s["name"] for s in data["sets"].values()}, "対象のまとまり")
    members = _check_set_members(data, members)
    sid = "S-" + uuid.uuid4().hex[:8]
    after = copy.deepcopy(data)
    after["sets"][sid] = {"set_id": sid, "name": name, "members": members}

    def write(db):
        db.execute("INSERT INTO target_sets VALUES (?, ?, ?)", (sid, name, json.dumps(members, ensure_ascii=False)))

    names = "・".join(data["targets"][m]["name"] for m in members)
    return Change("roles.set.create", sid, f"対象のまとまり「{name}」を作った（{names}）", after, write)


def set_update(db, data: dict, by: str, set_id: str, members: list[str]) -> Change:
    s = data["sets"].get(set_id)
    if not s:
        raise KeyError(set_id)
    members = _check_set_members(data, members)
    after = copy.deepcopy(data)
    after["sets"][set_id]["members"] = members

    def write(db):
        db.execute("UPDATE target_sets SET members = ? WHERE set_id = ?", (json.dumps(members, ensure_ascii=False), set_id))

    label = lambda tid: data["targets"][tid]["name"]   # noqa: E731
    added = [label(m) for m in members if m not in s["members"]]
    removed = [label(m) for m in s["members"] if m not in members]
    parts = ([f"{'・'.join(added)} を足した"] if added else []) + ([f"{'・'.join(removed)} を外した"] if removed else [])
    return Change("roles.set.update", set_id, f"対象のまとまり「{s['name']}」：" + ("、".join(parts) or "変更なし"),
                  after, write, _groups_using(data, set_id=set_id))


def set_delete(db, data: dict, by: str, set_id: str) -> Change:
    s = data["sets"].get(set_id)
    if not s:
        raise KeyError(set_id)
    if _groups_using(data, set_id=set_id):
        raise AccessError(f"対象のまとまり「{s['name']}」はグループに与えているので消せません", [("set", "使っています")], status=409)
    after = copy.deepcopy(data)
    del after["sets"][set_id]

    def write(db):
        db.execute("DELETE FROM target_sets WHERE set_id = ?", (set_id,))

    return Change("roles.set.delete", set_id, f"対象のまとまり「{s['name']}」を消した", after, write)


# ------------------------------------------------------------ 試しに当てる（保存しない）


def _group_signature(data: dict, gid: str):
    g = data["groups"].get(gid)
    if not g:
        return None
    members = sorted((m["account_id"], m["expires_at"] or "") for m in data["members"] if m["group_id"] == gid)
    grants = []
    for gr in data["grants"]:
        if gr["group_id"] != gid:
            continue
        perms = tuple(sorted(data["roles"].get(gr["role_id"], {}).get("permissions", [])))
        tids = [gr["target_id"]] if gr["target_id"] else data["sets"].get(gr["set_id"], {}).get("members", [])
        grants.append((perms, tuple(sorted(tids))))
    return json.dumps(g["rule"], sort_keys=True), members, sorted(grants)


def _touched_groups(before: dict, after: dict) -> list[str]:
    gids = list(dict.fromkeys([*before["groups"], *after["groups"]]))
    return [gid for gid in gids if _group_signature(before, gid) != _group_signature(after, gid)]


def _group_pairs(data: dict, gid: str) -> set[tuple[str, str]]:
    out = set()
    for gr in data["grants"]:
        if gr["group_id"] != gid or gr["role_id"] not in data["roles"]:
            continue
        tids = [gr["target_id"]] if gr["target_id"] else data["sets"].get(gr["set_id"], {}).get("members", [])
        out.update((p, t) for p in data["roles"][gr["role_id"]]["permissions"] for t in tids)
    return out


def _label(before: dict, after: dict, key: tuple[str, str]) -> str:
    t = after["targets"].get(key[1]) or before["targets"].get(key[1]) or {"name": key[1]}
    return f"{key[0]} × {t['name']}"


def _until(v: str | None) -> str:
    return "期限なし" if not v or v == FOREVER else v


def _diff(before: dict, after: dict, b: dict, a: dict) -> tuple[list[str], list[str]]:
    gained, lost = [], []
    for key in sorted(set(a) | set(b)):
        old, new = b.get(key, ""), a.get(key, "")
        if new > old:
            gained.append(_label(before, after, key) + (f"（期限 {_until(old)} → {_until(new)}）" if old
                                                         else (f"（期限 {new}）" if new != FOREVER else "")))
        elif new < old:
            lost.append(_label(before, after, key) + (f"（期限 {_until(old)} → {_until(new)}）" if new else ""))
    return gained, lost


def _dept_of(db, account_id: str) -> str:
    try:
        row = db.execute("SELECT dept FROM employees WHERE employee_no = ?", (account_id,)).fetchone()
    except sqlite3.Error:
        row = None
    return row[0] if row else ""


def _people_of(db, data_list: list[dict], gids: list[str]) -> tuple[dict[str, str], int]:
    """その変更で権限が変わりうる人（触れたグループのメンバー）→ 部署。数が多ければ空で返し、数だけ返す。"""
    manual = {m["account_id"] for d in data_list for m in d["members"] if m["group_id"] in gids}
    rules = [d["groups"][g]["rule"] for d in data_list for g in gids if g in d["groups"] and d["groups"][g]["rule"]]
    everyone = any(r.get("all") for r in rules)
    depts = sorted({x for r in rules for x in r.get("departments", [])})
    count = _rule_count(db, {"all": True} if everyone else ({"departments": depts} if depts else None)) + len(manual)
    if count > IMPACT_LIMIT:
        return {}, count
    people = {no: _dept_of(db, no) for no in manual}
    if everyone or depts:
        sql = "SELECT employee_no, dept FROM employees WHERE active = 1"
        args: list = []
        if not everyone:
            sql += f" AND dept IN ({','.join('?' * len(depts))})"
            args = depts
        try:
            people.update({r[0]: r[1] for r in db.execute(sql, args)})
        except sqlite3.Error:
            pass
    return people, len(people)


def preview(db, engine: "Engine", actor: str, actor_dept: str, before: dict, change: Change) -> dict:
    """変更を試しに当てて、だれのどの権限が増える・減るかを返す（保存しない）。断られる変更なら refused に理由。"""
    today = engine._today()
    after = change.after
    refused = None
    try:
        guard(db, engine, actor, actor_dept, before, after)
    except AccessError as e:
        refused = str(e)
    mine_gained, mine_lost = _diff(before, after, _effect(before, actor, actor_dept, today), _effect(after, actor, actor_dept, today))
    gids = _touched_groups(before, after)
    people, count = _people_of(db, [before, after], gids)
    rows = []
    for no, dept in sorted(people.items()):
        gained, lost = _diff(before, after, _effect(before, no, dept, today), _effect(after, no, dept, today))
        if gained or lost:
            who = account_of(db, after, no) or {"name": (after["accounts"].get(no) or {}).get("name") or no}
            rows.append({"account_id": no, "name": who["name"], "dept": dept, "gained": gained, "lost": lost})
    groups = []
    if count > IMPACT_LIMIT:
        for gid in gids:
            g = after["groups"].get(gid) or before["groups"][gid]
            b, a = _group_pairs(before, gid), _group_pairs(after, gid)
            members = _rule_count(db, g["rule"]) + len({m["account_id"] for d in (before, after) for m in d["members"]
                                                        if m["group_id"] == gid})
            groups.append({"group_id": gid, "name": g["name"], "members": members,
                           "gained": [_label(before, after, k) for k in sorted(a - b)],
                           "lost": [_label(before, after, k) for k in sorted(b - a)]})
    return {"detail": change.detail, "refused": refused, "self": {"gained": mine_gained, "lost": mine_lost},
            "self_loss": bool(mine_lost) and refused is None, "people": rows, "checked": count,
            "too_many": count > IMPACT_LIMIT, "groups": groups}


def explain(engine: Engine, account_id: str, dept: str) -> list[dict]:
    """その人が何をできるか：権限ごとに、どのグループのどの役割から来たか、対象、期限。期限の切れたものは出さない。"""
    out: dict[str, list[dict]] = {}
    for g in engine._live(account_id, dept):
        out.setdefault(g.permission, []).append({"group": g.group, "role": g.role, "target": g.target_label(),
                                                 "via": g.via, "expires_at": g.expires_at, "reason": g.reason()})
    return [{"permission": p, "feature": PERMISSIONS[p].feature, "does": PERMISSIONS[p].does, "from": out[p]}
            for p in PERMISSIONS if p in out]


# ------------------------------------------------------------ 復旧用のコマンド（python -m yanua access）


def show_text(db) -> str:
    """グループ・役割・メンバーを表で出す。"""
    data = load(db)
    lines = ["■ グループ"]
    for gid, g in data["groups"].items():
        lines.append(f"  {gid}  {g['name']}  （入り方：{_rule_text(g['rule'])}）")
        for gr in data["grants"]:
            if gr["group_id"] == gid:
                role = data["roles"].get(gr["role_id"], {}).get("name", gr["role_id"])
                lines.append(f"      役割 {role} × {_target_name(data, gr['target_id'], gr['set_id'])}  （番号 {gr['grant_id']}）")
        for m in data["members"]:
            if m["group_id"] == gid:
                name = (data["accounts"].get(m["account_id"]) or {}).get("name", "")
                lines.append(f"      メンバー {m['account_id']} {name}  期限 {m['expires_at'] or 'なし'}")
    lines.append("■ 役割")
    for rid, r in data["roles"].items():
        lines.append(f"  {rid}  {r['name']}：{'・'.join(r['permissions']) or 'なし'}")
    return "\n".join(lines)


def resolve(items: dict, key: str, field_name: str = "name") -> str:
    """番号か名前から、番号を返す（コマンドで名前でも書けるように）。"""
    if key in items:
        return key
    hits = [k for k, v in items.items() if v.get(field_name) == key]
    if len(hits) != 1:
        raise KeyError(key)
    return hits[0]
