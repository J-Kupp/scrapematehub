from __future__ import annotations

import json
import os
from pathlib import Path

from models import SupplierConfig
from shared_secrets import load_shared_secrets


PROJECT_ROOT = Path(__file__).resolve().parent
SUPPLIER_CONFIG_PATH = PROJECT_ROOT / "suppliers.json"
SUPPLIER_CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "suppliers.example.json"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env.local"


DEFAULT_SUPPLIERS = [
    SupplierConfig(
        supplier_slug="swissbox",
        enabled=True,
        scraper_adapter="swissbox",
        base_url="https://www.swissbox-ag.ch",
        ybm_token_env_var="YBM_TOKEN_SWISSBOX",
        output_dir="output/swissbox",
        catalog_update_policy="delete_missing",
        schedule={"frequency": "weekly", "weekday": "monday", "time": "03:30"},
        scrape_settings={"concurrency": 2, "min_delay_seconds": 0.15, "max_delay_seconds": 0.45},
    ),
    SupplierConfig(
        supplier_slug="gourmador",
        enabled=False,
        scraper_adapter="gourmador",
        base_url="https://shop.gourmadorzollikofen.ch",
        ybm_token_env_var="YBM_TOKEN_GOURMADOR",
        output_dir="output/gourmador",
        catalog_update_policy="delete_missing",
        schedule={"frequency": "weekly", "weekday": "monday", "time": "04:30"},
        scrape_settings={
            "category_concurrency": 2,
            "concurrency": 4,
            "min_delay_seconds": 0.1,
            "max_delay_seconds": 0.3,
        },
    ),
]


def _runtime_root_from_env(env_var: str, default_relative_path: str) -> Path:
    raw = os.environ.get(env_var, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (PROJECT_ROOT / default_relative_path).resolve()


def get_log_root() -> Path:
    return _runtime_root_from_env("SCRAPER_LOG_ROOT", "logs")


def get_state_root() -> Path:
    return _runtime_root_from_env("SCRAPER_STATE_ROOT", "state")


def get_cache_root() -> Path:
    return _runtime_root_from_env("SCRAPER_CACHE_ROOT", ".cache")


def load_env_file(env_path: Path | None = None) -> None:
    path = env_path or DEFAULT_ENV_PATH
    if not path.exists():
        load_shared_secrets()
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value
    load_shared_secrets()


def _load_supplier_configs_from_json(config_path: Path) -> list[SupplierConfig]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    suppliers = payload.get("suppliers", [])
    return [SupplierConfig(**supplier) for supplier in suppliers]


def _load_supplier_configs_from_payload(payload: object) -> list[SupplierConfig]:
    if isinstance(payload, dict):
        if "suppliers" in payload and isinstance(payload["suppliers"], list):
            suppliers = payload["suppliers"]
        elif "supplier_slug" in payload:
            suppliers = [payload]
        else:
            suppliers = []
    elif isinstance(payload, list):
        suppliers = payload
    else:
        suppliers = []
    return [SupplierConfig(**supplier) for supplier in suppliers if isinstance(supplier, dict)]


def load_supplier_configs(config_path: Path | None = None) -> list[SupplierConfig]:
    inline_json = os.environ.get("SUPPLIER_CONFIG_JSON", "").strip()
    if inline_json and config_path is None:
        try:
            return _load_supplier_configs_from_payload(json.loads(inline_json))
        except Exception:
            pass
    path = config_path or SUPPLIER_CONFIG_PATH
    if path.exists():
        return _load_supplier_configs_from_json(path)
    return DEFAULT_SUPPLIERS


def get_supplier_config(supplier_slug: str, config_path: Path | None = None) -> SupplierConfig:
    for supplier in load_supplier_configs(config_path):
        if supplier.supplier_slug == supplier_slug:
            return supplier
    raise KeyError(f"Unknown supplier slug: {supplier_slug}")


def save_supplier_configs(
    suppliers: list[SupplierConfig], config_path: Path | None = None
) -> Path:
    path = config_path or SUPPLIER_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "suppliers": [
            {
                "supplier_slug": supplier.supplier_slug,
                "enabled": supplier.enabled,
                "scraper_adapter": supplier.scraper_adapter,
                "base_url": supplier.base_url,
                "ybm_token_env_var": supplier.ybm_token_env_var,
                "output_dir": supplier.output_dir,
                "catalog_update_policy": supplier.catalog_update_policy,
                "schedule": supplier.schedule,
                "ybm_api_base": supplier.ybm_api_base,
                "scrape_settings": supplier.scrape_settings,
                "archived": supplier.archived,
                "alert_settings": supplier.alert_settings,
            }
            for supplier in suppliers
        ]
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path
