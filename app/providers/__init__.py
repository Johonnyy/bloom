"""Provider manifests — one TOML document per connected service.

A provider used to be *only* a file here, and that made adding one a code change
and a redeploy for something a user should be able to ask for. It is now a file
**or** a row in ``provider_manifests``, written by the builder from a service's own
documentation and shared through the sync store; see `app/manifests.py` for the
stored half and `app/providers/registry.py` for the format, the validation both are
held to, and the extra rules a stored one must also pass.

`spotify.toml` and `github.toml` remain as worked examples, and a file always beats
a row of the same name — those two definitions cannot be redefined by anything a
model writes.
"""

from app.providers.registry import (
    MAX_STORED_OPERATIONS,
    MAX_STORED_TOML_BYTES,
    ApiKeySpec,
    ClientCredentials,
    ManifestError,
    Operation,
    Probe,
    Provider,
    client_for,
    file_providers,
    get_provider,
    load_manifest_text,
    providers,
    register_operations,
    reload_providers,
    set_stored_loader,
)

__all__ = [
    "MAX_STORED_OPERATIONS",
    "MAX_STORED_TOML_BYTES",
    "ApiKeySpec",
    "ClientCredentials",
    "ManifestError",
    "Operation",
    "Probe",
    "Provider",
    "client_for",
    "file_providers",
    "get_provider",
    "load_manifest_text",
    "providers",
    "register_operations",
    "reload_providers",
    "set_stored_loader",
]
