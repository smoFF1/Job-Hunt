from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from job_hunter.core.dedup import fingerprint, normalize_company, normalize_location, normalize_text, normalize_url
from job_hunter.core.models import Job
from job_hunter.core.pipeline import JobPosting, ingest
from tests.conftest import NOW


@pytest.mark.parametrize("raw,expected", [
    ("Junior Backend Developer (Python)", "junior backend developer python"),
    ("  junior   backend-developer / PYTHON ", "junior backend developer python"),
    ("מפתח/ת Backend", "מפתח ת backend"),
    ("מְפַתֵּחַ", "מפתח"),  # niqqud stripped
    ("Full\u200fStack", "fullstack"),  # bidi mark stripped
])
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize("a,b", [
    ("TechNova Ltd", "TechNova, Ltd."),
    ("TechNova Inc.", "technova"),
    ('אופק תוכנה בע"מ', "אופק תוכנה"),
    ("אופק תוכנה בע״מ", "אופק תוכנה"),
])
def test_company_suffixes(a, b):
    assert normalize_company(a) == normalize_company(b)


def test_location_ignores_country():
    assert normalize_location("Tel Aviv, Israel") == normalize_location("tel aviv") == "tel aviv"


def test_fingerprint_same_job_across_sites():
    assert fingerprint("TechNova Ltd", "Junior Backend Developer (Python)", "Tel Aviv") == \
        fingerprint("TechNova, Ltd.", "junior backend developer - python", "Tel Aviv, Israel")


def test_fingerprint_differs_by_location():
    assert fingerprint("A", "Dev", "Haifa") != fingerprint("A", "Dev", "Tel Aviv")


@pytest.mark.parametrize("a,b", [
    ("https://www.Example.com/jobs/1/", "https://example.com/jobs/1"),
    ("http://example.com/jobs/1#apply", "https://example.com/jobs/1"),
    ("https://example.com/jobs/1?utm_source=x&utm_medium=y&fbclid=z", "https://example.com/jobs/1"),
    ("https://example.com/job?id=5&lang=he", "https://example.com/job?lang=he&id=5"),
])
def test_normalize_url_equivalents(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_keeps_meaningful_params():
    assert normalize_url("https://example.com/job?id=5") != normalize_url("https://example.com/job?id=6")
    assert normalize_url("") is None and normalize_url(None) is None


def _posting(**kw) -> JobPosting:
    base = dict(source="s1", source_job_id="1", title="Junior Dev", company="Acme", location="Haifa",
                description_text="Python", url="https://a.test/1")
    base.update(kw)
    return JobPosting(**base)


def test_ingest_dedup_by_fingerprint(session):
    j1, c1 = ingest(session, _posting(), NOW)
    j2, c2 = ingest(session, _posting(source="s2", source_job_id="99", url="https://b.test/x",
                                      company="ACME Ltd."), NOW)
    assert c1 and not c2 and j1.id == j2.id


def test_ingest_dedup_by_source_job_id(session):
    j1, _ = ingest(session, _posting(), NOW)
    j2, created = ingest(session, _posting(title="Totally different", url="https://a.test/other"), NOW)
    assert not created and j2.id == j1.id


def test_ingest_dedup_by_url(session):
    j1, _ = ingest(session, _posting(), NOW)
    j2, created = ingest(session, _posting(source="s2", source_job_id="2", title="Other",
                                           url="https://www.a.test/1/?utm_source=mail"), NOW)
    assert not created and j2.id == j1.id


def test_duplicate_bumps_last_seen_and_logs_event(session):
    from datetime import timedelta

    from sqlalchemy import select

    from job_hunter.core.models import Event

    j1, _ = ingest(session, _posting(), NOW)
    later = NOW + timedelta(days=1)
    ingest(session, _posting(), later)
    assert j1.last_seen_at == later and j1.first_seen_at == NOW
    actions = [e.action for e in session.scalars(select(Event)).all()]
    assert actions == ["job_inserted", "job_duplicate"]


def test_distinct_jobs_are_inserted(session):
    _, a = ingest(session, _posting(), NOW)
    _, b = ingest(session, _posting(source_job_id="2", title="QA Engineer", url="https://a.test/2"), NOW)
    assert a and b
    assert session.query(Job).count() == 2


@pytest.mark.parametrize("field,value", [
    ("fingerprint", "same"), ("url_normalized", "https://x.test/1")])
def test_db_unique_constraints(session, field, value):
    common = dict(source="s", title="t", company="c", location="l", status="New")
    a = Job(fingerprint="f1", source_job_id="1", url_normalized="u1", **common)
    b = Job(fingerprint="f2", source_job_id="2", url_normalized="u2", **common)
    setattr(a, field, value)
    setattr(b, field, value)
    session.add_all([a, b])
    with pytest.raises(IntegrityError):
        session.flush()


def test_db_unique_source_job_id(session):
    common = dict(title="t", company="c", location="l", status="New")
    session.add_all([Job(fingerprint="f1", source="s", source_job_id="1", **common),
                     Job(fingerprint="f2", source="s", source_job_id="1", **common)])
    with pytest.raises(IntegrityError):
        session.flush()


def test_db_rejects_unknown_status(session):
    session.add(Job(fingerprint="f", source="s", title="t", status="Maybe"))
    with pytest.raises(IntegrityError):
        session.flush()
