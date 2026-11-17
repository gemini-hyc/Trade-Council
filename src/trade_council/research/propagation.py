"""Apply a claim revision and propagate its invalidation.

A retracted or weakened claim does not silently disappear. Every claim that
depends on it becomes ``unresolved``, the outputs it fed are marked as needing
review, and the run stays in that state until someone records the re-check.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

REVISIONS_FILE = "revisions.jsonl"
CLAIMS_FILE = "claims.json"
STATE_FILE = "run.json"
ROLE_DIR = "agents"
INVALIDATING = {"weakened", "retracted", "unresolved"}


class RevisionError(ValueError):
    """Raised when a revision cannot be applied to the run."""


def pending_reviews(run: dict[str, Any]) -> list[dict[str, str]]:
    """Unreviewed ``(output, revision_id)`` pairs.

    Each entry names one output and the revision that invalidated it. Reading
    the legacy ``pending_review_outputs`` list is kept so old runs stay
    readable, but it is expanded to per-revision entries: one blanket review
    flag can no longer clear everything.
    """

    state = run.get("revision_state") or {}
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in state.get("pending_reviews") or []:
        if not isinstance(raw, dict):
            continue
        output = str(raw.get("output") or "").strip()
        revision_id = str(raw.get("revision_id") or "unknown").strip()
        if not output or (output, revision_id) in seen:
            continue
        seen.add((output, revision_id))
        entries.append(
            {
                "output": output,
                "revision_id": revision_id,
                "reason": str(raw.get("reason") or ""),
            }
        )
    legacy_revision = str(state.get("current_revision_id") or "legacy")
    for output in state.get("pending_review_outputs") or []:
        key = (str(output), legacy_revision)
        if not str(output) or key in seen:
            continue
        seen.add(key)
        entries.append(
            {"output": str(output), "revision_id": legacy_revision, "reason": "旧版待复核记录"}
        )
    return entries


def _rebuild_pending(state: dict[str, Any], revision_state: dict[str, Any]) -> None:
    """Derive both the per-revision list and the legacy flat list."""

    revision_state["pending_reviews"] = list(revision_state.get("pending_reviews") or [])
    revision_state["pending_review_outputs"] = sorted(
        {item["output"] for item in revision_state["pending_reviews"]}
    )
    state["revision_state"] = revision_state


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def role_payloads(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Every role artifact written by the workflow, keyed by role id."""

    found: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / ROLE_DIR).glob("phase*/*.json")):
        try:
            payload = _read_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            found[path.stem] = payload
    return found


def role_claim_refs(payload: dict[str, Any]) -> set[str]:
    """Claim ids a role states in its own words.

    A role summary and the central claim list are two texts about the same
    thing. The only way they stay consistent is if the role names the claims it
    is speaking about, so the pair can be checked instead of assumed.
    """

    refs: set[str] = set()
    for item in payload.get("key_findings") or []:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id") or "").strip()
        if claim_id:
            refs.add(claim_id)
    for item in payload.get("claim_refs") or []:
        if isinstance(item, dict):
            claim_id = str(item.get("claim_id") or "").strip()
        else:
            claim_id = str(item).strip()
        if claim_id:
            refs.add(claim_id)
    return refs


def roles_touching(run_dir: Path, claim_ids: Iterable[str]) -> dict[str, list[str]]:
    """Which roles must re-read their text after these claims changed."""

    wanted = {str(item) for item in claim_ids if str(item).strip()}
    if not wanted:
        return {}
    touched: dict[str, list[str]] = {}
    store = run_dir / CLAIMS_FILE
    if store.is_file():
        payload = _read_json(store)
        claims = payload.get("claims") if isinstance(payload, dict) else payload
        for claim in claims or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id") or "")
            role_id = str(claim.get("role_id") or "").strip()
            if claim_id in wanted and role_id:
                touched.setdefault(role_id, []).append(claim_id)
    for role_id, payload in role_payloads(run_dir).items():
        shared = role_claim_refs(payload) & wanted
        if shared:
            touched.setdefault(role_id, []).extend(sorted(shared))
    return {role: sorted(set(ids)) for role, ids in touched.items()}


def pending_role_reviews(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Roles whose summary has not been re-read since a claim they cite changed."""

    state = run.get("revision_state") or {}
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in state.get("pending_role_reviews") or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip()
        revision_id = str(raw.get("revision_id") or "unknown").strip()
        if not role or (role, revision_id) in seen:
            continue
        seen.add((role, revision_id))
        entries.append(
            {
                "role": role,
                "revision_id": revision_id,
                "claim_ids": [str(item) for item in raw.get("claim_ids") or []],
            }
        )
    return entries


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

    revision_id = str(payload.get("revision_id") or "").strip()
    if not revision_id:
        revision_id = f"r{_next_revision_number(run_dir)}"
    # The store carries the revision its claim statuses belong to. Leaving it
    # at the previous id makes the next validate report RESEARCH_REVISION_MISMATCH
    # and forces a hand edit of claims.json, which is exactly what the revision
    # command exists to avoid.
    store["revision_id"] = revision_id

    # Persist claims first: a crash after this point leaves claims and the
    # revision log consistent enough to re-run the command.
    (run_dir / CLAIMS_FILE).write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    affected_outputs = [str(item).strip() for item in payload.get("affected_outputs") or [] if str(item).strip()]
    if not affected_outputs:
        raise RevisionError(
            "affected_outputs 不能为空：修订必须指出它让哪些结论（valuation/fundamental/"
            "timing/execution 等）不再可直接引用"
        )
    record = {
        "revision_id": revision_id,
        "created_at": created_at or date.today().isoformat(),
        "trigger_evidence_ids": [str(item) for item in payload.get("trigger_evidence_ids") or []],
        "before": str(payload.get("before") or ""),
        "after": str(payload.get("after") or ""),
        "affected_claim_ids": sorted(changed_ids | set(propagated)),
        "affected_outputs": affected_outputs,
        "reviewed": False,
        "review_note": "",
    }
    if not record["before"] or not record["after"]:
        raise RevisionError("before / after 都必须写明修订前后的判断")
    with (run_dir / REVISIONS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    state = _update_run_state(run_dir, record)
    return {"record": record, "propagated_claim_ids": propagated, "run_state": state}


def _sync_revision_reviewed(run_dir: Path, revision_state: dict[str, Any], revision_id: str) -> None:
    """Mark a revision as reviewed once nothing about it is pending any more.

    The revision log is what the report prints, so leaving ``reviewed=False``
    after every output and role has been re-checked would make the delivered
    report claim work that was already done.
    """

    still_pending = any(
        str(item.get("revision_id")) == revision_id
        for item in (revision_state.get("pending_reviews") or [])
        if isinstance(item, dict)
    ) or any(
        str(item.get("revision_id")) == revision_id
        for item in (revision_state.get("pending_role_reviews") or [])
        if isinstance(item, dict)
    )
    if still_pending:
        return
    path = run_dir / REVISIONS_FILE
    if not path.is_file():
        return
    notes: list[str] = []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("revision_id")) == revision_id:
            record["reviewed"] = True
            notes = [
                str(item.get("note") or "")
                for item in (revision_state.get("reviews") or [])
                if str(item.get("revision_id")) == revision_id
            ]
            record["review_note"] = "；".join(note for note in notes if note)
        records.append(record)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def review_output(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    """Close exactly one pending review: an ``(output, revision)`` or a role.

    The review must name what it re-checked and the revision it reacted to,
    plus the evidence or calculation used. It cannot clear other pending
    items, and it cannot be replaced by one blanket flag.
    """

    output = str(payload.get("output") or "").strip()
    role = str(payload.get("role") or "").strip()
    revision_id = str(payload.get("revision_id") or "").strip()
    if output and role:
        raise RevisionError("一次复核只能针对一个对象：输出或角色，不要同时给")
    if not (output or role):
        raise RevisionError(
            "review 必须写明 output 或 role，且给出 revision_id，不能用一个布尔值清空待审项"
        )
    if not revision_id:
        raise RevisionError("review 必须写明 revision_id：复核要对应具体修订")
    note = str(payload.get("note") or "").strip()
    if not note:
        raise RevisionError("review 必须写明复核内容（改了哪个数、依据哪条证据）")

    state_path = run_dir / STATE_FILE
    if not state_path.is_file():
        raise RevisionError("缺少 run.json，无法记录复核")
    state = _read_json(state_path)
    revision_state = dict(state.get("revision_state") or {})
    if role:
        role_entries = [
            item
            for item in revision_state.get("pending_role_reviews") or []
            if isinstance(item, dict)
        ]
        matched = [
            item
            for item in role_entries
            if item.get("role") == role and str(item.get("revision_id")) == revision_id
        ]
        if not matched:
            raise RevisionError(
                f"没有待重审的角色 {role}@{revision_id}；角色复核必须对应具体修订"
            )
        revisions = load_revisions(run_dir)
        if not any(str(item.get("revision_id")) == revision_id for item in revisions):
            raise RevisionError(f"修订 {revision_id} 不存在于 revisions.jsonl")
        revision_state["pending_role_reviews"] = [
            item
            for item in role_entries
            if not (
                item.get("role") == role and str(item.get("revision_id")) == revision_id
            )
        ]
        reviews = list(revision_state.get("reviews") or [])
        reviews.append(
            {
                "role": role,
                "revision_id": revision_id,
                "claim_ids": [str(item) for item in matched[0].get("claim_ids") or []],
                "reviewed_at": reviewed_at or date.today().isoformat(),
                "note": note,
            }
        )
        revision_state["reviews"] = reviews
        _rebuild_pending(state, revision_state)
        _sync_revision_reviewed(run_dir, revision_state, revision_id)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "role": role,
            "revision_id": revision_id,
            "pending_role_reviews": revision_state["pending_role_reviews"],
            "reviews": reviews,
        }

    entries = revision_state.get("pending_reviews") or []
    if not any(
        item.get("output") == output and str(item.get("revision_id")) == revision_id
        for item in entries
        if isinstance(item, dict)
    ):
        raise RevisionError(
            f"没有待复核的 {output}@{revision_id}；复核必须对应具体修订与输出"
        )
    revisions = load_revisions(run_dir)
    if not any(str(item.get("revision_id")) == revision_id for item in revisions):
        raise RevisionError(f"修订 {revision_id} 不存在于 revisions.jsonl")
    revision_state["pending_reviews"] = [
        item
        for item in entries
        if not (isinstance(item, dict) and item.get("output") == output and str(item.get("revision_id")) == revision_id)
    ]
    reviews = list(revision_state.get("reviews") or [])
    reviews.append(
        {
            "output": output,
            "revision_id": revision_id,
            "reviewed_at": reviewed_at or date.today().isoformat(),
            "note": note,
            "recomputed_evidence_ids": [
                str(item) for item in payload.get("recomputed_evidence_ids") or []
            ],
        }
    )
    revision_state["reviews"] = reviews
    _rebuild_pending(state, revision_state)
    _sync_revision_reviewed(run_dir, revision_state, revision_id)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "output": output,
        "revision_id": revision_id,
        "pending_reviews": revision_state["pending_reviews"],
        "reviews": reviews,
    }


def reconcile_revisions(run_dir: Path) -> None:
    """Re-derive ``reviewed`` for every revision from the pending queues.

    Called before the report is built so a delivered report never claims a
    review is outstanding when nothing is, and never claims one is done when a
    queue still holds it.
    """

    path = run_dir / STATE_FILE
    if not path.is_file():
        return
    state = _read_json(path)
    revision_state = state.get("revision_state") or {}
    pending_outputs = {
        str(item.get("revision_id"))
        for item in revision_state.get("pending_reviews") or []
        if isinstance(item, dict)
    }
    pending_roles = {
        str(item.get("revision_id"))
        for item in revision_state.get("pending_role_reviews") or []
        if isinstance(item, dict)
    }
    documents = run_dir / REVISIONS_FILE
    if not documents.is_file():
        return
    records: list[dict[str, Any]] = []
    changed = False
    for line in documents.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        revision_id = str(record.get("revision_id"))
        if revision_id in pending_outputs or revision_id in pending_roles:
            if record.get("reviewed"):
                record["reviewed"] = False
                changed = True
        elif not record.get("reviewed") and (revision_state.get("reviews") or []):
            notes = [
                str(item.get("note") or "")
                for item in revision_state.get("reviews") or []
                if str(item.get("revision_id")) == revision_id
            ]
            if any(note for note in notes):
                record["reviewed"] = True
                record["review_note"] = "；".join(note for note in notes if note)
                changed = True
        records.append(record)
    if changed:
        documents.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )


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


def _update_run_state(run_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / STATE_FILE
    if not path.is_file():
        return {}
    state = _read_json(path)
    revision_state = dict(state.get("revision_state") or {})
    revision_state["current_revision_id"] = record["revision_id"]
    entries = [
        item
        for item in revision_state.get("pending_reviews") or []
        if isinstance(item, dict)
    ]
    seen = {(str(item.get("output")), str(item.get("revision_id"))) for item in entries}
    for output in record["affected_outputs"]:
        if (output, record["revision_id"]) in seen:
            continue
        entries.append(
            {
                "output": output,
                "revision_id": record["revision_id"],
                "reason": record["after"][:200],
            }
        )
    revision_state["pending_reviews"] = entries
    role_entries = [
        item
        for item in revision_state.get("pending_role_reviews") or []
        if isinstance(item, dict)
    ]
    seen_roles = {(str(item.get("role")), str(item.get("revision_id"))) for item in role_entries}
    for role, claim_ids in roles_touching(run_dir, record["affected_claim_ids"]).items():
        if (role, record["revision_id"]) in seen_roles:
            continue
        role_entries.append(
            {
                "role": role,
                "revision_id": record["revision_id"],
                "claim_ids": claim_ids,
                "reason": record["after"][:200],
            }
        )
    revision_state["pending_role_reviews"] = role_entries
    revision_state["last_revision_note"] = record["after"][:200]
    _rebuild_pending(state, revision_state)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return revision_state
