"""Cross-file validation for method and team configuration."""

from __future__ import annotations

from ..errors import MethodReferenceError
from ..schemas import TeamConfig
from .registry import MethodRegistry


def validate_team_methods(registry: MethodRegistry, team: TeamConfig) -> None:
    """Validate role and team method references against discovered methods."""

    available = set(registry.all())
    seen_roles: set[str] = set()
    for role in team.roles:
        if role.id in seen_roles:
            raise MethodReferenceError(f"Duplicate role id: {role.id}")
        seen_roles.add(role.id)
        unknown = sorted(set(role.methods) - available)
        if unknown:
            raise MethodReferenceError(
                f"Role {role.id} references unknown methods: {', '.join(unknown)}"
            )

    unknown_method_selections = sorted(set(team.methods) - available)
    if unknown_method_selections:
        raise MethodReferenceError(
            "Team config references unknown methods: "
            + ", ".join(unknown_method_selections)
        )

