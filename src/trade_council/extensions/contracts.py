"""Stable method contracts used by the orchestrator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..schemas import AnalysisMethodManifest, MethodResult, RequirementResult


class AnalysisMethod(Protocol):
    """Contract for a prompt, code, or hybrid analysis method."""

    manifest: AnalysisMethodManifest

    def validate_requirements(self, context: Mapping[str, Any]) -> RequirementResult:
        """Check whether all declared data requirements are available."""

    def run(self, context: Mapping[str, Any]) -> MethodResult:
        """Run the method and return a normalized result."""

