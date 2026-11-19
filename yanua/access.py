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

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import hr
from .permissions import DEFAULT_ROLES, PERMISSIONS

PERSON, SYSTEM = "person", "system"
SELF_DEPT, ALL = "special:self_dept", "special:all"
SPECIAL_NAMES = {SELF_DEPT: "メンバー自身の部署", ALL: "全社"}
TARGET_KINDS = ("department", "group", "feature", "system", "server", "special")
EVERYONE = "全員"     # 決まりの中身：在籍している人の全員
MIGRATED_BY = "（移し替え）"

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

    def __init__(self, message: str, problems: list[tuple[str, str]] | None = None):
        super().__init__(message)
        self.problems = problems or [("", message)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


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
            db.execute("INSERT OR IGNORE INTO targets VALUES (?, 'special', ?, '')", (special, name))
        role_ids = {}
        for i, (name, (desc, perms)) in enumerate(DEFAULT_ROLES.items(), start=1):
            role_ids[name] = f"R{i:02d}"
            db.execute("INSERT INTO access_roles VALUES (?, ?, ?, ?)", (role_ids[name], name, desc, json.dumps(perms)))

        def group(gid, name, desc, rule, grants, members=()):
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
    targets = {t[0]: {"target_id": t[0], "kind": t[1], "name": t[2], "ref": t[3]}
               for t in db.execute("SELECT target_id, kind, name, ref FROM targets ORDER BY target_id")}
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
    via: str               # 「手で入れた」「決まり：…」
    expires_at: str | None = None

    def reason(self) -> str:
        how = self.via + (f"、期限 {self.expires_at}" if self.expires_at else "")
        return f"グループ「{self.group}」（{how}）の役割「{self.role}」× {self.target_label()}"

    def target_label(self) -> str:
        return SPECIAL_NAMES.get(self.target, f"{self.target_kind}:{self.target_ref or self.target}")


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
                    if gr["group_id"] != gid:
                        continue
                    role = data["roles"][gr["role_id"]]
                    tids = [gr["target_id"]] if gr["target_id"] else data["sets"][gr["set_id"]]["members"]
                    for tid in tids:
                        t = data["targets"][tid]
                        for p in role["permissions"]:
                            result.append(Grant(p, g["name"], role["name"], tid, t["kind"], t["ref"], via, expires))
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


# ------------------------------------------------------------ 変更（roles.assign。API から）


def member_of(engine: Engine, account_id: str, dept: str) -> set[str]:
    with engine._connect() as db:
        data = load(db)
    today = engine._today()
    out = set()
    for gid, g in data["groups"].items():
        if _rule_match(g["rule"], dept):
            out.add(gid)
    for m in data["members"]:
        if m["account_id"] == account_id and not _expired(m["expires_at"], today):
            out.add(m["group_id"])
    return out


def _check_not_own(engine: Engine, actor: str, actor_dept: str, group_ids: set[str], what: str) -> None:
    own = member_of(engine, actor, actor_dept) & group_ids
    if own:
        raise AccessError(f"自分が入っているグループの{what}は変えられません", [("group", "自分が入っているグループです")])


def add_member(db, engine: Engine, actor: str, actor_dept: str, group_id: str, account_id: str, expires_at: str | None) -> str:
    data = load(db)
    g = data["groups"].get(group_id)
    if not g:
        raise KeyError(group_id)
    _check_not_own(engine, actor, actor_dept, {group_id}, "メンバー")
    if expires_at:
        try:
            datetime.fromisoformat(expires_at)
        except ValueError as e:
            raise AccessError("期限は YYYY-MM-DD で書いてください", [("expires_at", "期限は YYYY-MM-DD で書いてください")]) from e
    if account_id.startswith("sys:"):
        if account_id not in data["accounts"]:
            raise AccessError("そのシステムアカウントはありません", [("account", "ないアカウントです")])
        name = data["accounts"][account_id]["name"]
    else:
        person = hr.lookup(db, account_id)
        if not person:
            raise AccessError("その社員番号の人は、人事データにいません（在籍している人だけ入れられます）", [("account", "人事データにいません")])
        name = person["name"]
    with db:
        if not account_id.startswith("sys:"):
            _ensure_person(db, account_id)
        db.execute("INSERT INTO group_members VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id, account_id) "
                   "DO UPDATE SET expires_at = excluded.expires_at, added_by = excluded.added_by, added_at = excluded.added_at",
                   (group_id, account_id, expires_at or None, actor, _now()))
        _bump(db)
    return f"{g['name']} に {name} を追加" + (f"（期限 {expires_at}）" if expires_at else "（期限なし）")


def remove_member(db, engine: Engine, actor: str, actor_dept: str, group_id: str, account_id: str) -> str:
    data = load(db)
    g = data["groups"].get(group_id)
    if not g:
        raise KeyError(group_id)
    _check_not_own(engine, actor, actor_dept, {group_id}, "メンバー")
    name = (data["accounts"].get(account_id) or {}).get("name") or account_id
    with db:
        cur = db.execute("DELETE FROM group_members WHERE group_id = ? AND account_id = ?", (group_id, account_id))
        if not cur.rowcount:
            raise KeyError(account_id)
        _bump(db)
    return f"{g['name']} から {name} を外した（手で入れたメンバー）"


def add_grant(db, engine: Engine, actor: str, actor_dept: str, group_id: str, role_id: str,
              target_id: str | None, set_id: str | None) -> str:
    data = load(db)
    g = data["groups"].get(group_id)
    if not g:
        raise KeyError(group_id)
    _check_not_own(engine, actor, actor_dept, {group_id}, "役割")
    trial = {**data, "grants": [*data["grants"], {"grant_id": "new", "group_id": group_id, "role_id": role_id,
                                                  "target_id": target_id, "set_id": set_id}]}
    found = [p for p in problems(trial) if p[0] == "grants[new]"]
    if found:
        raise AccessError("役割の与え方に直すところがあります", found)
    with db:
        db.execute("INSERT INTO group_grants (group_id, role_id, target_id, set_id) VALUES (?, ?, ?, ?)",
                   (group_id, role_id, target_id, set_id))
        _bump(db)
    what = data["targets"][target_id]["name"] if target_id else f"まとまり「{data['sets'][set_id]['name']}」"
    return f"{g['name']} に 役割「{data['roles'][role_id]['name']}」× {what} を与えた"


def remove_grant(db, engine: Engine, actor: str, actor_dept: str, grant_id: int) -> str:
    data = load(db)
    gr = next((x for x in data["grants"] if x["grant_id"] == grant_id), None)
    if not gr:
        raise KeyError(grant_id)
    _check_not_own(engine, actor, actor_dept, {gr["group_id"]}, "役割")
    with db:
        db.execute("DELETE FROM group_grants WHERE grant_id = ?", (grant_id,))
        _bump(db)
    what = data["targets"][gr["target_id"]]["name"] if gr["target_id"] else f"まとまり「{data['sets'][gr['set_id']]['name']}」"
    return f"{data['groups'][gr['group_id']]['name']} から 役割「{data['roles'][gr['role_id']]['name']}」× {what} を外した"


def set_role_permissions(db, engine: Engine, actor: str, actor_dept: str, role_id: str, permissions: list[str]) -> str:
    data = load(db)
    r = data["roles"].get(role_id)
    if not r:
        raise KeyError(role_id)
    bad = [p for p in permissions if p not in PERMISSIONS]
    if bad:
        raise AccessError("知らない権限があります", [("permissions", f"知らない権限です: {p}") for p in bad])
    uses = {gr["group_id"] for gr in data["grants"] if gr["role_id"] == role_id}
    _check_not_own(engine, actor, actor_dept, uses, "役割")
    new = [p for p in PERMISSIONS if p in set(permissions)]
    with db:
        db.execute("UPDATE access_roles SET permissions = ? WHERE role_id = ?", (json.dumps(new), role_id))
        _bump(db)
    added = [p for p in new if p not in r["permissions"]]
    removed = [p for p in r["permissions"] if p not in new]
    parts = ([f"{'・'.join(added)} を足した"] if added else []) + ([f"{'・'.join(removed)} を外した"] if removed else [])
    return f"役割「{r['name']}」の権限：" + ("、".join(parts) or "変更なし")


def explain(engine: Engine, account_id: str, dept: str) -> list[dict]:
    """その人が何をできるか（権限と、それがどこから来たか）。期限の切れたものは出さない。"""
    out: dict[str, list[str]] = {}
    for g in engine._live(account_id, dept):
        out.setdefault(g.permission, []).append(g.reason())
    return [{"permission": p, "feature": PERMISSIONS[p].feature, "does": PERMISSIONS[p].does, "from": out[p]}
            for p in PERMISSIONS if p in out]
