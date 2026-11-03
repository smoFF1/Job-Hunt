"""Site scrapers (Phase 2+). Adding a site = one file here + one entry in search.yaml."""

from __future__ import annotations

import importlib
import os
import pkgutil

_INFRA_MODULES = frozenset({"base", "http", "registry", "robots", "runner", "scan"})


def load_registered_scrapers() -> None:
    """Auto-import all site scraper modules in `job_hunter.scrapers` plus any test modules."""
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.ispkg or mod_info.name.startswith("_") or mod_info.name in _INFRA_MODULES:
            continue
        importlib.import_module(f"{__name__}.{mod_info.name}")

    extra = os.environ.get("JOB_HUNTER_EXTRA_SCRAPERS", "").strip()
    if extra:
        for mod_name in extra.split(","):
            clean = mod_name.strip()
            if clean:
                importlib.import_module(clean)


load_registered_scrapers()
