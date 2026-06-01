from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from core.contracts import SupplierScrapeResult
from models import SupplierConfig


class SupplierAdapter(ABC):
    def __init__(self, config: SupplierConfig, project_root: Path) -> None:
        self.config = config
        self.project_root = project_root
        self.output_dir = config.output_path(project_root)

    @abstractmethod
    async def scrape(
        self,
        *,
        force_refresh: bool = False,
    ) -> SupplierScrapeResult:
        raise NotImplementedError
