"""モデルの入手と照合（models pull / models verify）。

- 入手は、許可リストの宛先からだけ（yanua/netpolicy.py）
- 入手したファイルの SHA-256 を models.lock.json に記録し、使う前に毎回照合する（すり替え・破損を見つける）
- 版（revision）は config/network.json で固定できる。空なら初回に入手した版を記録し、2回目からはその版だけを使う
  （版を上げるときは --update を付けて、明示的に上げる）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .netpolicy import Policy, guarded_hf_session

DEFAULT_DIR = Path("data/models")
LOCK = "models.lock.json"


class ModelError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _lock_path(models_dir: Path) -> Path:
    return models_dir / LOCK


def read_lock(models_dir: Path) -> dict:
    p = _lock_path(models_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_lock(models_dir: Path, lock: dict) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    _lock_path(models_dir).write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record(models_dir: Path, name: str, repo: str, revision: str, local: Path) -> dict:
    files = {str(p.relative_to(local)): sha256(p) for p in sorted(local.rglob("*"))
             if p.is_file() and ".cache" not in p.parts}
    lock = read_lock(models_dir)
    lock[name] = {"repo": repo, "revision": revision, "path": local.name,
                  "pulled_at": datetime.now().astimezone().isoformat(timespec="seconds"), "files": files}
    write_lock(models_dir, lock)
    return lock[name]


def pull(name: str, policy: Policy, models_dir: Path = DEFAULT_DIR, update: bool = False, downloader=None) -> dict:
    """モデルを入手して記録する。downloader はテスト用（既定は huggingface_hub.snapshot_download）。"""
    if name not in policy.models:
        raise ModelError(f"モデル「{name}」は config/network.json の models にありません（ある: {'、'.join(policy.models)}）")
    spec = policy.models[name]
    repo = spec["repo"]
    locked = read_lock(models_dir).get(name, {})
    revision = spec.get("revision") or ("" if update else locked.get("revision", ""))
    policy.check(f"https://huggingface.co/{repo}")
    local = models_dir / name
    if downloader is None:
        from huggingface_hub import HfApi, snapshot_download

        guarded_hf_session(policy)
        if not revision:
            revision = HfApi().model_info(repo).sha   # いまの版を固定して入手する
        downloader = snapshot_download
    downloader(repo_id=repo, revision=revision or None, local_dir=str(local))
    return record(models_dir, name, repo, revision, local)


def verify(name: str, models_dir: Path = DEFAULT_DIR) -> Path:
    """記録したハッシュと照合し、合っていればモデルのフォルダを返す。"""
    entry = read_lock(models_dir).get(name)
    if not entry:
        raise ModelError(f"モデル「{name}」はまだ入手していません。先に python -m yanua models pull {name} を実行してください。")
    local = models_dir / entry["path"]
    for rel, digest in entry["files"].items():
        p = local / rel
        if not p.exists():
            raise ModelError(f"モデルのファイルがありません: {p}")
        if sha256(p) != digest:
            raise ModelError(f"モデルのファイルが記録と違います（すり替え・破損の可能性）: {p}")
    return local
