"""Application error types and stable exit codes."""


class TradeCouncilError(Exception):
    """Base class for expected application errors."""

    exit_code = 1


class ConfigError(TradeCouncilError):
    """Configuration is missing or invalid."""

    exit_code = 2


class MethodManifestError(TradeCouncilError):
    """A method manifest or package is invalid."""

    exit_code = 2


class MethodReferenceError(TradeCouncilError):
    """A configured method reference does not exist."""

    exit_code = 2


class LLMError(TradeCouncilError):
    """Base error for model calls."""

    exit_code = 3


class ProviderUnavailableError(LLMError):
    """The configured model provider cannot be reached."""


class InvalidResponseError(LLMError):
    """The provider returned an invalid or unusable response."""


class BudgetExceededError(LLMError):
    """The configured run budget has been exceeded."""


class OperationError(TradeCouncilError):
    """A local run, export, or maintenance operation cannot be completed."""

    exit_code = 2
