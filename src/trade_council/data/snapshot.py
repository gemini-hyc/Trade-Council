"""Content hashing and verification for frozen data packs.

A snapshot ID identifies the *selection* of source data; it is not proof of the
bytes on disk. This module records a sha256 per emitted file and a combined
content hash, so a later run can prove that the files it reads are the files it
froze.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

#: ``manifest.json`` and ``snapshot.json`` carry the hash list itself and
#: ``snapshot_files.json`` is the hash list, so none of them can be part of the
#: content hash they record: writing the recorded hash back into them would
#: change the very bytes being hashed.
EXCLUDED_FILES = {"manifest.json", "snapshot.json", "snapshot_files.json"}
HASHES_FILE = "snapshot_files.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_hashes(root: Path) -> dict[str, dict[str, Any]]:
    """Hash every data file under ``root`` except the manifest and hash list."""

    hashes: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in EXCLUDED_FILES:
            continue
        hashes[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return hashes


def content_hash(hashes: dict[str, dict[str, Any]]) -> str:
    payload = "\n".join(
        f"{name}:{entry['sha256']}:{entry['size']}" for name, entry in sorted(hashes.items())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def freeze(root: Path) -> dict[str, Any]:
    """Write ``snapshot_files.json`` and return its payload."""

    hashes = collect_hashes(root)
    payload = {
        "algorithm": "sha256",
        "excluded": sorted(EXCLUDED_FILES),
        "content_hash": content_hash(hashes),
        "files": hashes,
    }
    (root / HASHES_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def load_frozen(root: Path) -> dict[str, Any] | None:
    path = root / HASHES_FILE
    if not path.is_file():
        return None
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def verify(root: Path, expected_content_hash: str | None) -> list[str]:
    """Return a list of integrity problems; empty means the pack is intact."""

    if not root.is_dir():
        return [f"数据包目录不存在：{root}"]
    if not expected_content_hash:
        return ["运行记录没有绑定数据包内容哈希，无法证明输入未变"]
    actual = content_hash(collect_hashes(root))
    if actual != expected_content_hash:
        return [
            (
                "数据包内容与冻结时不一致："
                f"记录 {expected_content_hash[:16]}…，实际 {actual[:16]}…"
            )
        ]
    frozen = load_frozen(root)
    if frozen is None:
        return ["数据包缺少 snapshot_files.json，无法逐文件核对"]
    recorded = frozen.get("files", {})
    problems: list[str] = []
    for name, entry in recorded.items():
        path = root / name
        if not path.is_file():
            problems.append(f"冻结文件缺失：{name}")
            continue
        if sha256_file(path) != entry.get("sha256"):
            problems.append(f"冻结文件被修改：{name}")
    return problems


def verify_manifest(root: Path, expected_sha256: str | None) -> list[str]:
    path = root / "manifest.json"
    if not path.is_file():
        return [f"缺少 manifest.json：{root}"]
    if not expected_sha256:
        return ["运行记录没有绑定 manifest 哈希"]
    actual = sha256_file(path)
    if actual != expected_sha256:
        return ["manifest.json 与冻结时不一致"]
    return []
