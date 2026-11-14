"""役割と、役割ごとにできる操作（K-049）。

- 部署はどの「文書」を見られるかを決める（K-003）。役割はどの「画面と操作」を使えるかを決める
- ログインした人は全員「社員」。ほかの役割は config/roles.json で、部署か社員番号から決める
- 操作は PERMISSIONS に書いたものだけを許す。書いていない操作はだれにも許さない
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

EMPLOYEE = "社員"
ROLES = (EMPLOYEE, "総務", "情シス", "人事", "管理者")

# 操作 → その操作をしてよい役割
PERMISSIONS: dict[str, frozenset[str]] = {
    "ask": frozenset({EMPLOYEE}),
    "search": frozenset({EMPLOYEE}),
    "documents.list": frozenset({EMPLOYEE}),
    "documents.ingest": frozenset({"管理者"}),
    "helpdesk": frozenset({EMPLOYEE}),
    "jobs.own": frozenset({EMPLOYEE}),
    "jobs.all": frozenset({"管理者"}),
}


class RoleConfigError(ValueError):
    pass


@dataclass
class RoleConfig:
    # 役割 → 部署の一覧・社員番号の一覧
    by_department: dict[str, set[str]] = field(default_factory=dict)
    by_employee: dict[str, set[str]] = field(default_factory=dict)

    def roles_for(self, employee_no: str, dept: str) -> list[str]:
        roles = {EMPLOYEE}
        for role, depts in self.by_department.items():
            if dept in depts:
                roles.add(role)
        for role, nos in self.by_employee.items():
            if employee_no in nos:
                roles.add(role)
        return [r for r in ROLES if r in roles]


def load_roles(path: Path) -> RoleConfig:
    """役割の設定を読む。書き間違い（知らない役割、リストでない値）はエラーにする。"""
    if not path.exists():
        return RoleConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RoleConfigError(f"{path} をJSONとして読めません: {e}") from e
    cfg = RoleConfig()
    for role, spec in (data.get("roles") or {}).items():
        if role not in ROLES or role == EMPLOYEE:
            raise RoleConfigError(f"{path}: 知らない役割です: {role}（使える役割: {'・'.join(ROLES[1:])}）")
        if not isinstance(spec, dict):
            raise RoleConfigError(f"{path}: {role} の値は {{\"departments\": [...], \"employees\": [...]}} の形にしてください")
        for key, target in (("departments", cfg.by_department), ("employees", cfg.by_employee)):
            values = spec.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise RoleConfigError(f"{path}: {role} の {key} は文字列のリストにしてください")
            target[role] = {v.strip() for v in values if v.strip()}
    return cfg


def allowed(roles: list[str], action: str) -> bool:
    return bool(PERMISSIONS.get(action, frozenset()) & set(roles))


def permissions_of(roles: list[str]) -> list[str]:
    return sorted(a for a in PERMISSIONS if allowed(roles, a))
