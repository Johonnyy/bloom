"""Provider manifests — one TOML file per connected service.

Adding a provider is adding a file here. See `app/providers/registry.py` for the
format and the validation it is held to, and `spotify.toml` for a worked example.
"""

from app.providers.registry import (
    ManifestError,
    Operation,
    Provider,
    get_provider,
    providers,
    register_operations,
)

__all__ = [
    "ManifestError",
    "Operation",
    "Provider",
    "get_provider",
    "providers",
    "register_operations",
]
