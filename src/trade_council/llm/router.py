"""Profile and role-based model routing."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigError
from .models import ModelsConfig, ProfileConfig


@dataclass(frozen=True)
class RouteDecision:
    profile: str
    role: str
    category: str
    provider: str
    model: str
    deep_tier: str | None


class ModelRouter:
    def __init__(self, config: ModelsConfig) -> None:
        self.config = config

    def _validate_policy(self, provider_name: str) -> None:
        policy = self.config.cloud_policy
        if provider_name in policy.denied_providers:
            raise ConfigError(f"Provider {provider_name!r} is explicitly denied")
        provider = self.config.providers.get(provider_name)
        if provider is None:
            raise ConfigError(f"Unknown provider: {provider_name}")
        if (
            provider.kind in {"deepseek", "openai_compatible"}
            and policy.allowed_providers
            and provider_name not in policy.allowed_providers
        ):
            raise ConfigError(f"Cloud provider {provider_name!r} is not in allowed_providers")

    def route(
        self,
        *,
        role: str,
        profile_name: str,
        deep_tier: str | None = None,
    ) -> RouteDecision:
        try:
            profile = self.config.profiles[profile_name]
        except KeyError as exc:
            raise ConfigError(f"Unknown model profile: {profile_name}") from exc
        try:
            category = self.config.role_categories[role]
        except KeyError as exc:
            raise ConfigError(f"Unknown role for model routing: {role}") from exc
        self._validate_policy(profile.provider)

        selected_model = self._select_model(profile, category, deep_tier)
        provider_models = {
            model.id for model in self.config.providers[profile.provider].models
        }
        if selected_model not in provider_models:
            raise ConfigError(
                f"Model {selected_model!r} is not configured for provider {profile.provider}"
            )
        return RouteDecision(
            profile=profile_name,
            role=role,
            category=category,
            provider=profile.provider,
            model=selected_model,
            deep_tier=deep_tier,
        )

    @staticmethod
    def _select_model(
        profile: ProfileConfig,
        category: str,
        requested_tier: str | None,
    ) -> str:
        tier = requested_tier or profile.deep_tier
        if tier not in {"auto", "flash", "pro"}:
            raise ConfigError(f"Unknown deep tier: {tier}")
        if profile.tier_models:
            if tier == "flash":
                selected = profile.tier_models.get("flash")
            elif tier == "pro":
                selected = profile.tier_models.get("pro")
            else:
                selected = (
                    profile.tier_models.get("flash")
                    if category == "extraction"
                    else profile.tier_models.get("pro")
                )
            if selected:
                return selected
        try:
            return profile.models[category]
        except KeyError as exc:
            raise ConfigError(
                f"Profile {profile.display_name!r} has no model for category {category!r}"
            ) from exc
