"""Generic runtime for prompt, code, and hybrid analysis methods."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..engineering.evidence import EvidenceIndex
from ..errors import MethodManifestError
from ..schemas import MethodResult
from .loader import ResolvedMethod
from .sandbox import run_compute_method


def _validate_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema_subset(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the JSON Schema subset used by method output contracts."""

    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _validate_type(value, expected_type):
        return [f"{path}: expected {expected_type}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, str) and len(value) < int(schema.get("minLength", 0)):
        errors.append(f"{path}: string is shorter than minLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: number is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: number is above maximum")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required property {key}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                if schema.get("additionalProperties") is False:
                    errors.append(f"{path}: unexpected property {key}")
                continue
            errors.extend(validate_schema_subset(item, child_schema, f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(validate_schema_subset(item, schema["items"], f"{path}[{index}]"))
    return errors


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MethodManifestError(f"Invalid method output schema {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MethodManifestError(f"Method output schema must be an object: {path}")
    return value


def _collect_evidence_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_id" and isinstance(item, str):
                found.append(item)
            elif key == "evidence_ids" and isinstance(item, list):
                found.extend(str(entry) for entry in item)
            else:
                found.extend(_collect_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_evidence_ids(item))
    return list(dict.fromkeys(found))


def _requirement_gaps(
    method: ResolvedMethod,
    evidence: EvidenceIndex,
) -> list[str]:
    items = evidence.all()
    available_datasets = {item.dataset_id for item in items}
    available_fields = {item.field for item in items if item.field}
    gaps = [
        f"method {method.manifest.id}: missing dataset {dataset_id}"
        for dataset_id in method.manifest.requires.datasets
        if dataset_id not in available_datasets
    ]
    gaps.extend(
        f"method {method.manifest.id}: missing field {field}"
        for field in method.manifest.requires.fields
        if field not in available_fields
    )
    return gaps


def execute_method(
    method: ResolvedMethod,
    *,
    data_pack_dir: Path,
    evidence: EvidenceIndex,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Execute one method without coupling the orchestrator to its domain logic."""

    instructions_path = method.path.parent / "method.md"
    instructions = (
        instructions_path.read_text(encoding="utf-8") if instructions_path.is_file() else ""
    )
    manifest_sha256 = hashlib.sha256(method.path.read_bytes()).hexdigest()
    method_md_sha256 = (
        hashlib.sha256(instructions_path.read_bytes()).hexdigest()
        if instructions_path.is_file()
        else None
    )
    execution: dict[str, Any] = {
        "id": method.manifest.id,
        "name": method.manifest.name,
        "version": method.manifest.version,
        "domain": method.manifest.domain,
        "type": method.manifest.type,
        "enabled": method.enabled,
        "instructions": instructions,
        "manifest_sha256": manifest_sha256,
        "method_md_sha256": method_md_sha256,
        "snapshot_id": manifest.get("snapshot_id"),
        "status": "ready",
        "result": None,
        "data_gaps": [],
        "errors": [],
    }
    if not method.enabled:
        execution["status"] = "disabled"
        return execution

    gaps = _requirement_gaps(method, evidence)
    if gaps:
        execution["status"] = "skipped"
        execution["data_gaps"] = gaps
        return execution

    if method.manifest.type in {"code", "hybrid"}:
        compute_path = method.path.parent / "compute.py"
        try:
            output = run_compute_method(
                compute_path,
                {
                    "method_id": method.manifest.id,
                    "parameters": method.manifest.parameters,
                    "symbol": manifest.get("ts_code"),
                    "asof": manifest.get("asof"),
                    "snapshot_id": manifest.get("snapshot_id"),
                    "data_pack_dir": str(data_pack_dir),
                    "evidence": [item.model_dump() for item in evidence.all()],
                },
            )
            schema_path = method.path.parent / method.manifest.outputs.schema_path
            schema_errors = validate_schema_subset(output, _load_schema(schema_path))
            if schema_errors:
                raise MethodManifestError("; ".join(schema_errors))
            evidence_ids = _collect_evidence_ids(output)
            evidence.validate_references(evidence_ids)
            if method.manifest.outputs.evidence_policy == "required" and not evidence_ids:
                raise MethodManifestError("method output must reference evidence")
            normalized = MethodResult(
                method_id=method.manifest.id,
                version=method.manifest.version,
                domain=method.manifest.domain,
                signal=str(output.get("signal", "insufficient")),
                evidence_ids=evidence_ids,
                structured_output=output,
            )
            execution["status"] = "computed"
            execution["result"] = normalized.model_dump()
        except Exception as exc:  # noqa: BLE001
            execution["status"] = "error"
            execution["errors"] = [str(exc)]
            execution["data_gaps"] = [f"method {method.manifest.id}: execution failed"]
    return execution


def build_role_method_context(
    executions: dict[str, dict[str, Any]],
    method_ids: list[str],
) -> dict[str, Any]:
    selected = [executions[method_id] for method_id in method_ids if method_id in executions]
    return {
        "instructions": "\n\n".join(
            f"## {item['id']}@{item['version']}\n{item['instructions']}"
            for item in selected
            if item["instructions"]
        ),
        "methods": selected,
        "data_gaps": [
            gap
            for item in selected
            for gap in item.get("data_gaps", [])
        ],
        "conflict_groups": [
            domain
            for domain in sorted({item["domain"] for item in selected})
            if sum(item["domain"] == domain for item in selected) > 1
        ],
    }
