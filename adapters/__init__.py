from importlib import import_module
from pathlib import Path

from adapters.base import SupplierAdapter


ADAPTER_REGISTRY = {
    "gourmador": "adapters.gourmador:GourmadorAdapter",
    "swissbox": "adapters.swissbox:SwissboxAdapter",
    "walker": "adapters.walker:WalkerAdapter",
}


def get_adapter(name: str) -> type[SupplierAdapter]:
    try:
        target = ADAPTER_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown scraper adapter: {name}") from exc
    module_name, class_name = target.split(":", 1)
    module = import_module(module_name)
    return getattr(module, class_name)


def adapter_path_info(name: str) -> dict[str, str]:
    root = Path(__file__).resolve().parent
    package_dir = root / name
    return {
        "adapter_package_dir": str(package_dir),
        "scraper_path": str(package_dir / "scraper.py"),
        "transformer_path": str(package_dir / "transform.py"),
        "fixtures_path": str(package_dir / "fixtures"),
        "tests_hint": f"tests/test_{name}_transform.py",
    }
