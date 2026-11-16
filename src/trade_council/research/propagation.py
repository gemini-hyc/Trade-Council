"""Apply a claim revision and propagate its invalidation.

A retracted or weakened claim does not silently disappear. Every claim that
depends on it becomes ``unresolved``, the outputs it fed are marked as needing
review, and the run stays in that state until someone records the re-check.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

REVISIONS_FILE = "revisions.jsonl"
CLAIMS_FILE = "claims.json"
STATE_FILE = "run.json"
INVALIDATING = {"weakened", "retracted", "unresolved"}


class RevisionError(ValueError):
    """Raised when a revision cannot be applied to the run."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_claims(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = run_dir / CLAIMS_FILE
    if not path.is_file():
        raise RevisionError(f"缺少 {CLAIMS_FILE}，先写出论点再记录修订")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        payload = {"schema_version": "1.0", "claims": payload}
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise RevisionError(f"{CLAIMS_FILE} 的 claims 必须是数组")
    return payload, claims


def record_revision(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Record a revision, update claim statuses and propagate to dependents.

    ``payload`` keys: ``revision_id``, ``trigger_evidence_ids``, ``before``,
    ``after``, ``changed_claims`` (list of ``{claim_id, status, status_reason}``),
    ``affected_outputs``, ``reviewed``, ``review_note``.
    """

    store, claims = _load_claims(run_dir)
    by_id = {str(claim.get("claim_id")): claim for claim in claims}

    raw_changes = payload.get("changed_claims") or []
    if not isinstance(raw_changes, list) or not raw_changes:
        raise RevisionError("changed_claims 必须是非空数组：修订要指出哪个论点变了")
    changes: list[dict[str, str]] = []
    for change in raw_changes:
        claim_id = str(change.get("claim_id") or "").strip()
        status = str(change.get("status") or "").strip()
        if claim_id not in by_id:
            raise RevisionError(f"修订引用了不存在的论点：{claim_id}")
        if status not in {"intact", "weakened", "retracted", "unresolved"}:
            raise RevisionError(f"修订状态非法：{claim_id} -> {status!r}")
        changes.append(
            {
                "claim_id": claim_id,
                "status": status,
                "status_reason": str(change.get("status_reason") or "").strip(),
            }
        )

    propagated: list[str] = []
    for change in changes:
        claim = by_id[change["claim_id"]]
        claim["status"] = change["status"]
        claim["status_reason"] = change["status_reason"]
        claim["revision"] = int(claim.get("revision") or 1) + 1

    changed_ids = {change["claim_id"] for change in changes}
    invalidated = {
        claim_id for claim_id in changed_ids if by_id[claim_id].get("status") in INVALIDATING
    }
    queue = list(invalidated)
    while queue:
        current = queue.pop()
        for claim in claims:
            claim_id = str(claim.get("claim_id"))
            depends = {str(item) for item in claim.get("depends_on_claim_ids") or []}
            if current not in depends or claim_id in invalidated:
                continue
            if (claim.get("status") or "intact") == "intact":
                claim["status"] = "unresolved"
                claim["status_reason"] = f"依赖的论点 {current} 已失效，需重新确认"
                claim["revision"] = int(claim.get("revision") or 1) + 1
            invalidated.add(claim_id)
            propagated.append(claim_id)
            queue.append(claim_id)

    # Persist claims first: a crash after this point leaves claims and the
    # revision log consistent enough to re-run the command.
    (run_dir / CLAIMS_FILE).write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    revision_id = str(payload.get("revision_id") or "").strip()
    if not revision_id:
        revision_id = f"r{_next_revision_number(run_dir)}"
    record = {
        "revision_id": revision_id,
        "created_at": created_at or date.today().isoformat(),
        "trigger_evidence_ids": [str(item) for item in payload.get("trigger_evidence_ids") or []],
        "before": str(payload.get("before") or ""),
        "after": str(payload.get("after") or ""),
        "affected_claim_ids": sorted(changed_ids | set(propagated)),
        "affected_outputs": [str(item) for item in payload.get("affected_outputs") or []],
        "reviewed": bool(payload.get("reviewed", False)),
        "review_note": str(payload.get("review_note") or ""),
    }
    if not record["before"] or not record["after"]:
        raise RevisionError("before / after 都必须写明修订前后的判断")
    with (run_dir / REVISIONS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    state = _update_run_state(run_dir, record, pending=not record["reviewed"])
    return {"record": record, "propagated_claim_ids": propagated, "run_state": state}


def _next_revision_number(run_dir: Path) -> int:
    path = run_dir / REVISIONS_FILE
    if not path.is_file():
        return 2
    count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return count + 2


def load_revisions(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / REVISIONS_FILE
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"revision_id": "invalid", "raw": line[:200]})
    return records


def _update_run_state(run_dir: Path, record: dict[str, Any], *, pending: bool) -> dict[str, Any]:
    path = run_dir / STATE_FILE
    if not path.is_file():
        return {}
    state = _read_json(path)
    revision_state = dict(state.get("revision_state") or {})
    revision_state["current_revision_id"] = record["revision_id"]
    pending_outputs = set(revision_state.get("pending_review_outputs") or [])
    if pending:
        pending_outputs.update(record["affected_outputs"])
    else:
        pending_outputs.clear()
    revision_state["pending_review_outputs"] = sorted(pending_outputs)
    revision_state["last_review_note"] = record["review_note"]
    state["revision_state"] = revision_state
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return revision_state
