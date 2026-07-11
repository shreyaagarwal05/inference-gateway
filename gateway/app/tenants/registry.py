"""Load and retain per-tenant configuration in memory."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger("uvicorn.error")
DEFAULT_CONFIG_PATH = "/app/config/tenants.yaml"


@dataclass(frozen=True, slots=True)
class TenantConfig:
    similarity_threshold: float
    ttl: int
    model_version: str
    failure_threshold: int
    window_seconds: int
    cooldown_seconds: int
    timeout_seconds: float


_tenant_registry: dict[str, TenantConfig] = {}


def _parse_tenant(tenant_id: str, values: Any) -> TenantConfig:
    if not isinstance(values, dict):
        raise ValueError(f"Configuration for tenant {tenant_id!r} must be a mapping")

    try:
        return TenantConfig(
            similarity_threshold=float(values["similarity_threshold"]),
            ttl=int(values["ttl"]),
            model_version=str(values["model_version"]),
            failure_threshold=int(values["failure_threshold"]),
            window_seconds=int(values["window_seconds"]),
            cooldown_seconds=int(values["cooldown_seconds"]),
            timeout_seconds=float(values["timeout_seconds"]),
        )
    except KeyError as exc:
        raise ValueError(
            f"Configuration for tenant {tenant_id!r} is missing {exc.args[0]!r}"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Configuration for tenant {tenant_id!r} contains an invalid value"
        ) from exc


def load_tenant_registry(config_path: str | Path | None = None) -> None:
    """Read the tenant YAML once during application startup."""
    path = Path(
        config_path
        if config_path is not None
        else os.getenv("TENANTS_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    )

    with path.open("r", encoding="utf-8") as config_file:
        document = yaml.safe_load(config_file)

    if not isinstance(document, dict) or not isinstance(document.get("tenants"), dict):
        raise ValueError("Tenant configuration must contain a 'tenants' mapping")

    parsed = {
        tenant_id: _parse_tenant(tenant_id, values)
        for tenant_id, values in document["tenants"].items()
    }
    if not parsed:
        raise ValueError("Tenant configuration must define at least one tenant")

    _tenant_registry.clear()
    _tenant_registry.update(parsed)
    LOGGER.info("Loaded tenant configuration: %s", _tenant_registry)


def get_tenant_config(tenant_id: str) -> TenantConfig | None:
    """Return a tenant's startup-loaded configuration, if it exists."""
    return _tenant_registry.get(tenant_id)


def get_tenant_ids() -> tuple[str, ...]:
    """Return the tenant identifiers loaded during application startup."""
    return tuple(_tenant_registry)
