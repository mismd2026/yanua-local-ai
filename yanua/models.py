"""モデルの入手と照合（models pull / models verify）。

- 入手は、許可リストの宛先からだけ（yanua/netpolicy.py）
- 入手したファイルの SHA-256 を models.lock.json に記録し、使う前に毎回照合する（すり替え・破損を見つける）
- 版（revision）は config/network.json で固定できる。空なら初回に入手した版を記録し、2回目からはその版だけを使う
  （版を上げるときは --update を付けて、明示的に上げる）
- Ollama のモデル（"kind": "ollama"。チャット・要約に使う）は、入手は ollama pull で行う。models pull は、
  手元の Ollama が持っている版の digest（SHA-256）を登録するだけで、Yanua からは社外に通信しない。
  使う前（設定の画面・models verify）に、Ollama が返す digest と登録した digest が同じかを確かめる。
  入手元（Ollama のレジストリ）の digest との照合は、S-53（通信とモデル）で足す
- 目録（config/network.json の models）の "use" は用途（chat・summary・embedding・transcribe）。
  設定の画面のチャット・要約の欄には、その用途があるモデルだけを出す（埋め込み用は出さない）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .netpolicy import Policy, guarded_hf_session

DEFAULT_DIR = Path("data/models")
LOCK = "models.lock.json"
OLLAMA = "ollama"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# 設定の画面に出す、目録のモデルの状態
OK, NOT_INSTALLED, NOT_REGISTERED, CHANGED = "ok", "not_installed", "not_registered", "changed"


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


def kind_of(spec: dict) -> str:
    return spec.get("kind", "huggingface")


def ollama_tags(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = 10.0) -> dict[str, str]:
    """手元の Ollama に入っているモデル → digest（手元の Ollama に聞くだけ。社外には通信しない）。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ModelError(f"Ollama（{base_url}）に接続できません: {e}") from e
    return {m["name"]: m.get("digest", "") for m in data.get("models", []) if m.get("name")}


def _tag(tags: dict[str, str], name: str) -> str | None:
    return tags.get(name) if name in tags else tags.get(f"{name}:latest") if ":" not in name else None


def register_ollama(name: str, models_dir: Path, tags: dict[str, str], update: bool = False) -> dict:
    """Ollama のモデルの、今の digest を登録する。登録したものと違う digest は、--update のときだけ登録し直す。"""
    digest = _tag(tags, name)
    if not digest:
        raise ModelError(f"Ollama にモデル「{name}」が入っていません。先に ollama pull {name} で入手してから、"
                         f"python -m yanua models pull {name} で登録してください。")
    lock = read_lock(models_dir)
    old = lock.get(name, {}).get("digest")
    if old and old != digest and not update:
        raise ModelError(f"モデル「{name}」の digest が、登録したものと違います（登録 {old[:12]}、今 {digest[:12]}）。"
                         f"入れ替えたのが自分たちだと確かめてから、--update を付けて登録し直してください。")
    lock[name] = {"kind": OLLAMA, "digest": digest,
                  "registered_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    write_lock(models_dir, lock)
    return lock[name]


def ollama_state(name: str, models_dir: Path, tags: dict[str, str]) -> tuple[str, str]:
    """(状態, 画面に出す文)。使ってよいのは OK だけ。"""
    digest = _tag(tags, name)
    entry = read_lock(models_dir).get(name)
    if not digest:
        return NOT_INSTALLED, f"入っていません（ollama pull {name} で入手し、models pull {name} で登録してください）"
    if not entry or not entry.get("digest"):
        return NOT_REGISTERED, f"登録されていません（python -m yanua models pull {name} で登録してください）"
    if entry["digest"] != digest:
        return CHANGED, (f"登録した digest と違います（すり替え・入れ替えの可能性）。確かめてから "
                         f"python -m yanua models pull {name} --update で登録し直してください")
    return OK, ""


def catalog(policy: Policy, models_dir: Path, tags: dict[str, str]) -> list[dict]:
    """目録の Ollama のモデルと、その状態（設定の画面で、チャット・要約に選べるものを決める）。"""
    out = []
    for name, spec in policy.models.items():
        if kind_of(spec) != OLLAMA:
            continue
        state, message = ollama_state(name, models_dir, tags)
        out.append({"name": name, "use": list(spec.get("use", [])), "state": state, "message": message})
    return out


def pull(name: str, policy: Policy, models_dir: Path = DEFAULT_DIR, update: bool = False, downloader=None,
         ollama_url: str = DEFAULT_OLLAMA_URL, tags: dict[str, str] | None = None) -> dict:
    """モデルを入手して記録する。downloader はテスト用（既定は huggingface_hub.snapshot_download）。
    Ollama のモデルは入手せず、手元の Ollama の digest を登録する（tags はテスト用）。"""
    if name not in policy.models:
        raise ModelError(f"モデル「{name}」は config/network.json の models にありません（ある: {'、'.join(policy.models)}）")
    spec = policy.models[name]
    if kind_of(spec) == OLLAMA:
        return register_ollama(name, models_dir, tags if tags is not None else ollama_tags(ollama_url), update)
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


def verify_ollama(name: str, models_dir: Path = DEFAULT_DIR, tags: dict[str, str] | None = None,
                  ollama_url: str = DEFAULT_OLLAMA_URL) -> str:
    """Ollama のモデルの今の digest が、登録したものと同じかを確かめる。同じなら digest を返す。"""
    tags = tags if tags is not None else ollama_tags(ollama_url)
    state, message = ollama_state(name, models_dir, tags)
    if state != OK:
        raise ModelError(f"モデル「{name}」：{message}")
    return _tag(tags, name) or ""


def verify(name: str, models_dir: Path = DEFAULT_DIR) -> Path:
    """記録したハッシュと照合し、合っていればモデルのフォルダを返す。"""
    entry = read_lock(models_dir).get(name)
    if not entry:
        raise ModelError(f"モデル「{name}」はまだ入手していません。先に python -m yanua models pull {name} を実行してください。")
    if entry.get("kind") == OLLAMA:
        raise ModelError(f"モデル「{name}」は Ollama のモデルです（ファイルではなく digest で確かめます）")
    local = models_dir / entry["path"]
    for rel, digest in entry["files"].items():
        p = local / rel
        if not p.exists():
            raise ModelError(f"モデルのファイルがありません: {p}")
        if sha256(p) != digest:
            raise ModelError(f"モデルのファイルが記録と違います（すり替え・破損の可能性）: {p}")
    return local
