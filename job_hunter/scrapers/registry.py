"""Scraper registry: site name -> scraper class. Populated by @register in each <site>.py (Phase 2+)."""

from __future__ import annotations

from job_hunter.scrapers.base import BaseScraper

_REGISTRY: dict[str, type[BaseScraper]] = {}


def register(cls: type[BaseScraper]) -> type[BaseScraper]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} must define a non-empty `name`")
    _REGISTRY[cls.name] = cls
    return cls


def get_scraper(name: str) -> type[BaseScraper]:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"No scraper registered for site '{name}'") from exc


def registered_sites() -> list[str]:
    return sorted(_REGISTRY)


def enabled_sites(sites_config: dict[str, dict]) -> list[str]:
    """Sites that are both registered and enabled in search.yaml."""
    return [n for n in registered_sites() if sites_config.get(n, {}).get("enabled") is True]
