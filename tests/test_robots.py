"""Offline unit tests for wildcard robots.txt enforcement and disallowed-pattern guards."""

from __future__ import annotations

import httpx
import pytest

from job_hunter.scrapers.alljobs import ALLJOBS_ORIGIN, build_alljobs_http_settings, build_search_url
from job_hunter.scrapers.http import DEFAULT_USER_AGENT, PoliteHttpClient
from job_hunter.scrapers.robots import (
    ALLJOBS_HARD_DENY_PATH_PREFIXES,
    ALLJOBS_HARD_DENY_SUBSTRINGS,
    GLOBAL_HARD_DENY_PATH_PREFIXES,
    RobotsDisallowedError,
    RobotsPolicy,
    RobotsUnavailableError,
    fetch_robots_policy,
)

ALLJOBS_ROBOTS_TXT = """\
User-agent: *
Disallow: /home3.aspx*
Disallow: /Uploads/User/*
Disallow: /uploads/user/*
Disallow: /LandingPages/
Allow: /LandingPages/CampaignPublic/
Disallow: /SectionsArticles/ContentIndex
Disallow: /SectionsArticles/ContentSearch.aspx
Disallow: /Partners/Ynet
Disallow: /Partners/Calcalist
Disallow: *&fdate
Disallow: /*searchFeedReturn=search
Disallow: *Eloqua*
Disallow: *&urltoredirect
Disallow: /auth/login
"""


@pytest.fixture
def alljobs_policy() -> RobotsPolicy:
    return RobotsPolicy.from_text(
        ALLJOBS_ORIGIN,
        ALLJOBS_ROBOTS_TXT,
        DEFAULT_USER_AGENT,
        deny_path_prefixes=GLOBAL_HARD_DENY_PATH_PREFIXES + ALLJOBS_HARD_DENY_PATH_PREFIXES,
        deny_substrings=ALLJOBS_HARD_DENY_SUBSTRINGS,
    )


@pytest.mark.parametrize(
    "disallowed_url",
    [
        "https://www.alljobs.co.il/home3.aspx",
        "https://www.alljobs.co.il/home3.aspx?foo=1",
        "https://www.alljobs.co.il/HOME3.ASPX?bar=2",
        "https://www.alljobs.co.il/Uploads/User/cv_123.pdf",
        "https://www.alljobs.co.il/uploads/user/cv_123.pdf",
        "https://www.alljobs.co.il/LandingPages/Promo123",
        "https://www.alljobs.co.il/SectionsArticles/ContentIndex",
        "https://www.alljobs.co.il/SectionsArticles/ContentSearch.aspx?q=dev",
        "https://www.alljobs.co.il/Partners/Ynet/jobs",
        "https://www.alljobs.co.il/Partners/Calcalist/jobs",
        "https://www.alljobs.co.il/SearchResultsGuest.aspx?page=1&fdate=7",
        "https://www.alljobs.co.il/SearchResultsGuest.aspx?fdate=7",
        "https://www.alljobs.co.il/SearchResultsGuest.aspx?page=1&searchFeedReturn=search",
        "https://www.alljobs.co.il/track?campaign=EloquaMail",
        "https://www.alljobs.co.il/track?campaign=eloquamail",
        "https://www.alljobs.co.il/SearchResultsGuest.aspx?page=1&urltoredirect=/foo",
        "https://www.alljobs.co.il/auth/login",
        "https://www.alljobs.co.il/auth/sso/callback",
        "https://www.alljobs.co.il/Auth/Login?returnUrl=/",
        "https://evil.example.com/SearchResultsGuest.aspx?page=1",
    ],
)
def test_alljobs_disallowed_urls_are_blocked(alljobs_policy: RobotsPolicy, disallowed_url: str):
    assert alljobs_policy.can_fetch(disallowed_url) is False
    with pytest.raises(RobotsDisallowedError):
        alljobs_policy.assert_allowed(disallowed_url)


@pytest.mark.parametrize(
    "allowed_url",
    [
        build_search_url("Python Developer", page=1),
        build_search_url("מפתח תוכנה", page=2),
        "https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID=8817898",
        "https://www.alljobs.co.il/LandingPages/CampaignPublic/index.aspx",
    ],
)
def test_alljobs_allowed_urls_pass(alljobs_policy: RobotsPolicy, allowed_url: str):
    assert alljobs_policy.can_fetch(allowed_url) is True
    alljobs_policy.assert_allowed(allowed_url)


def test_robots_fetched_and_cached_once_per_run():
    calls: list[str] = []
    valid_page = "<html><body><div id='divAlljobsHeader'></div><span id='TotalJobsCount'>0</span></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT, headers={"content-type": "text/plain"})
        return httpx.Response(200, text=valid_page, headers={"content-type": "text/html"})

    raw = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        with PoliteHttpClient(build_alljobs_http_settings(), client=raw, sleep_fn=lambda _: None) as polite:
            polite.get_html(build_search_url("Python", page=1), expected_markers=("TotalJobsCount",))
            polite.get_html(build_search_url("Python", page=2), expected_markers=("TotalJobsCount",))
    finally:
        raw.close()

    assert calls.count("https://www.alljobs.co.il/robots.txt") == 1
    assert len(calls) == 3


@pytest.mark.parametrize(
    "robots_response",
    [
        httpx.Response(404, text="Not Found"),
        httpx.Response(500, text="Server Error"),
        httpx.Response(200, text="<!DOCTYPE html><html><head><title>Captcha</title></head><body></body></html>",
                       headers={"content-type": "text/html"}),
    ],
)
def test_robots_unavailable_or_html_aborts_before_any_page_fetch(robots_response: httpx.Response):
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return robots_response
        raise AssertionError("Page URL must never be fetched if robots.txt fails")

    raw = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        with PoliteHttpClient(build_alljobs_http_settings(), client=raw, sleep_fn=lambda _: None) as polite:
            with pytest.raises(RobotsUnavailableError):
                polite.get_html(build_search_url("Python", page=1))
    finally:
        raw.close()

    assert calls == ["https://www.alljobs.co.il/robots.txt"]


def test_robots_network_error_raises_unavailable():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failure", request=req)

    raw = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        with pytest.raises(RobotsUnavailableError):
            fetch_robots_policy(raw, ALLJOBS_ORIGIN, DEFAULT_USER_AGENT)
    finally:
        raw.close()


def test_redirect_to_disallowed_robots_pattern_is_not_followed():
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT, headers={"content-type": "text/plain"})
        if req.url.path == "/SearchResultsGuest.aspx":
            return httpx.Response(
                302,
                headers={"location": "/LandingPages/BlockedPromo?a=1&urltoredirect=/foo"},
            )
        raise AssertionError(f"Disallowed redirect target must never be requested: {req.url}")

    raw = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        with PoliteHttpClient(build_alljobs_http_settings(), client=raw, sleep_fn=lambda _: None) as polite:
            with pytest.raises(RobotsDisallowedError):
                polite.get_html(build_search_url("Python", page=1))
    finally:
        raw.close()

    assert len(calls) == 2  # robots.txt + initial SearchResultsGuest.aspx only
