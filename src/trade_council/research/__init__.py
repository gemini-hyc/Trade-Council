"""Research loop helpers: supplementary evidence import, gap classes, revisions.

The modules here are deliberately small. They validate and record what the
current agent found; they do not search, scrape, or call a model.
"""

from .evidence_import import EvidenceImportError, ImportResult, import_evidence
from .gaps import FRESHNESS_RULES, GAP_LABELS, GapRecord, freshness_check, gap_kind_for_status
from .propagation import (
    RevisionError,
    load_revisions,
    pending_reviews,
    reconcile_revisions,
    record_revision,
    review_output,
)
from .recompute import RecomputeError, finance_dir, recompute_financials

__all__ = [
    "FRESHNESS_RULES",
    "GAP_LABELS",
    "EvidenceImportError",
    "GapRecord",
    "ImportResult",
    "RecomputeError",
    "RevisionError",
    "finance_dir",
    "freshness_check",
    "gap_kind_for_status",
    "import_evidence",
    "load_revisions",
    "pending_reviews",
    "recompute_financials",
    "reconcile_revisions",
    "record_revision",
    "review_output",
]
