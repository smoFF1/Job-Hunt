"""Load and validate YAML configuration (profile, cvs, search, skills).

Config errors raise ConfigError with a clear message; soft problems are collected in
AppConfig.warnings (e.g. a profile core skill that is not in the skills vocabulary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from job_hunter import DEFAULT_CONFIG_DIR, PROJECT_ROOT


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Profile:
    first_name: str
    last_name: str
    email: str
    core_skills: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CVEntry:
    id: str
    file: str
    label: str
    tags: list[str]
    default: bool


@dataclass(frozen=True)
class Category:
    name: str
    tag: str
    keywords: list[str]


@dataclass(frozen=True)
class SkillDef:
    name: str
    aliases: list[str]
    case_sensitive: bool = False


@dataclass(frozen=True)
class ScoringConfig:
    role_title_category: int = 30
    role_generic_title: int = 20
    role_description_category: int = 15
    skills_max: int = 30
    skills_none_listed: int = 10
    seniority_boost: int = 20
    seniority_neutral: int = 10
    seniority_boost_max_years: float = 1
    domain_bonus: int = 10
    recency_buckets: list[tuple[int, int]] = field(default_factory=lambda: [(3, 10), (7, 7), (14, 4)])
    recency_older: int = 0
    recency_unknown: int = 5
    student_description_penalty: int = -20


@dataclass(frozen=True)
class SearchConfig:
    experience_cutoff: float
    shortlist_threshold: int
    llm_tiebreaker: bool
    hebrew_prefixes: str
    senior_title_keywords: list[str]
    student_keywords: list[str]
    graduate_keywords: list[str]
    advantage_markers: list[str]
    boost_keywords: list[str]
    generic_title_keywords: list[str]
    categories: list[Category]
    domain_keywords: list[str]
    scoring: ScoringConfig
    roles: list[str]
    sites: dict[str, dict[str, Any]]
    senior_description_keywords: list[str] = field(default_factory=list)
    blocked_companies: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    root: Path
    config_dir: Path
    profile: Profile
    cvs: list[CVEntry]
    search: SearchConfig
    skills: list[SkillDef]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        hint = ""
        if path.name in ("profile.yaml", "cvs.yaml"):
            example = path.name.replace(".yaml", ".example.yaml")
            hint = f" (copy config/{example} to config/{path.name} and fill it in)"
        raise ConfigError(f"Missing config file: {path}{hint}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")
    return data


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return [str(v) for v in value]


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field '{key}'")
    return d[key]


# --------------------------------------------------------------------------- loaders

def load_profile(path: Path) -> Profile:
    d = _read_yaml(path)
    where = path.name
    return Profile(
        first_name=str(_require(d, "first_name", where)),
        last_name=str(_require(d, "last_name", where)),
        email=str(_require(d, "email", where)),
        core_skills=_str_list(d.get("core_skills"), f"{where}: core_skills"),
        raw=d,
    )


def load_cvs(path: Path, warnings: list[str]) -> list[CVEntry]:
    d = _read_yaml(path)
    items = d.get("cvs") or []
    if not isinstance(items, list):
        raise ConfigError("cvs.yaml: 'cvs' must be a list")
    cvs: list[CVEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"cvs.yaml entry #{i + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping")
        cv_id = str(_require(item, "id", where))
        if cv_id in seen:
            raise ConfigError(f"cvs.yaml: duplicate id '{cv_id}'")
        seen.add(cv_id)
        cvs.append(
            CVEntry(
                id=cv_id,
                file=str(_require(item, "file", where)),
                label=str(item.get("label") or cv_id),
                tags=[t.lower() for t in _str_list(item.get("tags"), f"{where}: tags")],
                default=bool(item.get("default", False)),
            )
        )
    defaults = [c for c in cvs if c.default]
    if len(defaults) > 1:
        raise ConfigError(f"cvs.yaml: more than one default CV ({', '.join(c.id for c in defaults)})")
    if cvs and not defaults:
        first = cvs[0]
        warnings.append(f"cvs.yaml: no default CV set; using '{first.id}' as default")
        cvs[0] = CVEntry(first.id, first.file, first.label, first.tags, True)
    if not cvs:
        warnings.append("cvs.yaml: no CVs registered; scoring will use profile core_skills only")
    return cvs


def load_skills(path: Path) -> list[SkillDef]:
    d = _read_yaml(path)
    items = d.get("skills") or []
    if not isinstance(items, list):
        raise ConfigError("skills.yaml: 'skills' must be a list")
    skills: list[SkillDef] = []
    alias_owner: dict[str, str] = {}
    for i, item in enumerate(items):
        where = f"skills.yaml entry #{i + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping")
        name = str(_require(item, "name", where))
        aliases = _str_list(item.get("aliases"), f"{where}: aliases")
        for term in [name, *aliases]:
            key = _alias_key(term)
            if key in alias_owner and alias_owner[key] != name:
                raise ConfigError(f"skills.yaml: alias '{term}' used by both '{alias_owner[key]}' and '{name}'")
            alias_owner[key] = name
        skills.append(SkillDef(name=name, aliases=aliases, case_sensitive=bool(item.get("case_sensitive"))))
    return skills


def _alias_key(term: str) -> str:
    return " ".join(term.lower().replace("-", " ").split())


def load_search(path: Path) -> SearchConfig:
    d = _read_yaml(path)
    thresholds = d.get("thresholds") or {}
    filters = d.get("filters") or {}
    sc = d.get("scoring") or {}
    role, skills, sen = sc.get("role") or {}, sc.get("skills") or {}, sc.get("seniority") or {}
    dom, rec, pen = sc.get("domain") or {}, sc.get("recency") or {}, sc.get("penalties") or {}

    categories: list[Category] = []
    for i, c in enumerate(d.get("categories") or []):
        where = f"search.yaml categories #{i + 1}"
        if not isinstance(c, dict):
            raise ConfigError(f"{where} must be a mapping")
        categories.append(
            Category(
                name=str(_require(c, "name", where)),
                tag=str(c.get("tag") or c["name"]).lower(),
                keywords=_str_list(c.get("keywords"), f"{where}: keywords"),
            )
        )

    defaults = ScoringConfig()
    buckets_raw = rec.get("buckets", defaults.recency_buckets)
    try:
        buckets = sorted((int(b[0]), int(b[1])) for b in buckets_raw)
    except (TypeError, ValueError, IndexError) as exc:
        raise ConfigError("search.yaml: scoring.recency.buckets must be [[days, points], ...]") from exc

    student_penalty = int(pen.get("student_in_description", defaults.student_description_penalty))
    if student_penalty > 0:
        raise ConfigError("search.yaml: scoring.penalties.student_in_description must be <= 0 (e.g. -20)")

    scoring = ScoringConfig(
        role_title_category=int(role.get("title_category", defaults.role_title_category)),
        role_generic_title=int(role.get("generic_title", defaults.role_generic_title)),
        role_description_category=int(role.get("description_category", defaults.role_description_category)),
        skills_max=int(skills.get("max", defaults.skills_max)),
        skills_none_listed=int(skills.get("no_skills_listed", defaults.skills_none_listed)),
        seniority_boost=int(sen.get("boost", defaults.seniority_boost)),
        seniority_neutral=int(sen.get("neutral", defaults.seniority_neutral)),
        seniority_boost_max_years=float(sen.get("boost_max_years", defaults.seniority_boost_max_years)),
        domain_bonus=int(dom.get("bonus", defaults.domain_bonus)),
        recency_buckets=buckets,
        recency_older=int(rec.get("older", defaults.recency_older)),
        recency_unknown=int(rec.get("unknown", defaults.recency_unknown)),
        student_description_penalty=student_penalty,
    )

    sites = d.get("sites") or {}
    if not isinstance(sites, dict):
        raise ConfigError("search.yaml: 'sites' must be a mapping")
    for name, site in sites.items():
        if not isinstance(site, dict) or "enabled" not in site:
            raise ConfigError(f"search.yaml: site '{name}' must be a mapping with an 'enabled' flag")

    return SearchConfig(
        experience_cutoff=float(thresholds.get("experience_cutoff", 2)),
        shortlist_threshold=int(thresholds.get("shortlist_threshold", 50)),
        llm_tiebreaker=bool(d.get("llm_tiebreaker", False)),
        hebrew_prefixes=str(d.get("hebrew_prefixes", "ובהלש")),
        senior_title_keywords=_str_list(filters.get("senior_title_keywords"), "filters.senior_title_keywords"),
        student_keywords=_str_list(filters.get("student_keywords"), "filters.student_keywords"),
        graduate_keywords=_str_list(filters.get("graduate_keywords"), "filters.graduate_keywords"),
        advantage_markers=_str_list(filters.get("advantage_markers"), "filters.advantage_markers"),
        boost_keywords=_str_list(d.get("boost_keywords"), "boost_keywords"),
        generic_title_keywords=_str_list(d.get("generic_title_keywords"), "generic_title_keywords"),
        categories=categories,
        domain_keywords=_str_list(d.get("domain_keywords"), "domain_keywords"),
        scoring=scoring,
        roles=_str_list(d.get("roles"), "roles"),
        sites=sites,
        senior_description_keywords=_str_list(
            filters.get("senior_description_keywords"), "filters.senior_description_keywords"
        ),
        blocked_companies=_str_list(filters.get("blocked_companies"), "filters.blocked_companies"),
    )


def load_config(config_dir: Path | None = None, root: Path | None = None) -> AppConfig:
    """Load all config files from `config_dir`. CV file paths are resolved relative to `root`."""
    config_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
    root = Path(root or PROJECT_ROOT)
    warnings: list[str] = []
    profile = load_profile(config_dir / "profile.yaml")
    cvs = load_cvs(config_dir / "cvs.yaml", warnings)
    search = load_search(config_dir / "search.yaml")
    skills = load_skills(config_dir / "skills.yaml")

    known = {_alias_key(t) for s in skills for t in [s.name, *s.aliases]}
    for cs in profile.core_skills:
        if _alias_key(cs) not in known:
            warnings.append(f"profile.yaml: core skill '{cs}' is not in skills.yaml (it can't be matched)")

    for site_name, site_cfg in search.sites.items():
        if not site_cfg.get("enabled"):
            continue
        per_q = site_cfg.get("max_pages_per_query")
        per_run = site_cfg.get("max_pages_per_run")
        if per_q is not None and per_run is not None and search.roles:
            planned = len(search.roles) * int(per_q)
            if planned > int(per_run):
                warnings.append(
                    f"search.yaml: site '{site_name}' max_pages_per_run ({per_run}) is below planned budget "
                    f"({len(search.roles)} queries * {per_q} pages = {planned}); trailing queries may be skipped"
                )

    return AppConfig(root=root, config_dir=config_dir, profile=profile, cvs=cvs, search=search,
                     skills=skills, warnings=warnings)
