from __future__ import annotations

import pytest

from job_hunter.core.filters import JobFilters


@pytest.fixture
def f(cfg) -> JobFilters:
    return JobFilters(cfg.search)


# ----------------------------------------------------------------------------- senior titles

@pytest.mark.parametrize("title", [
    "Senior Backend Engineer", "SENIOR developer", "Sr. Frontend Developer", "Sr Software Engineer",
    "Team Lead, Fullstack", "Tech Lead", "Principal Engineer", "Staff Software Engineer",
    "Software Architect", "R&D Manager", "Director of Engineering", "Head of Data", "VP R&D",
    "Senior-Level Developer",
    "מפתח/ת Backend בכיר/ה", "מפתחת בכירה", "ראש צוות פיתוח", "ראש קבוצת", "ראש קבוצה - תוכנה",
    "ארכיטקט תוכנה", "מנהל פיתוח", "מנהלת מוצר", "מפתח תוכנה ומנהל פרויקטים", "דרוש/ה למנהל",
    "והמנהל הטכני",
])
def test_senior_title_skipped(f, title):
    assert f.senior_match(title), title


@pytest.mark.parametrize("title", [
    "Junior Backend Developer", "Software Engineer", "Full Stack Developer", "QA Automation Engineer",
    "Leadership Program Graduate",  # 'lead' must be a whole word
    "Staffing Systems Developer",    # 'staff' whole word
    "Srv Developer",
    "מפתח/ת Backend", "מפתחת ג'וניור", "מנהלן מערכת",  # 'מנהלן' is a different word
])
def test_non_senior_title_passes(f, title):
    assert f.senior_match(title) is None, title


def test_senior_reason_format(f):
    d = f.evaluate("Senior Backend Engineer", "")
    assert d.skip and d.rule == "senior"
    assert d.reason.startswith("Skipped - Senior Role")


# ----------------------------------------------------------------------------- experience

@pytest.mark.parametrize("text,years", [
    ("3+ years of experience with Node.js", 3),
    ("3 + yrs experience", 3),
    ("2-4 years of experience in React", 2),
    ("2 – 4 years of backend experience", 2),
    ("2 to 4 years in Python development", 2),
    ("between 2 and 4 years", 2),
    ("At least 3 years of experience", 3),
    ("Minimum of 2 years experience", 2),
    ("more than 5 years in C++", 5),
    ("5 years of experience in Java", 5),
    ("Experience: 4 years", 4),
    ("two years of experience", 2),
    ("1 year of experience required", 1),
    ("0-2 years experience", 0),
    ("1-2 years of experience", 1),
    ("1.5 years experience", 1.5),
    ("לפחות 3 שנות ניסיון", 3),
    ("3+ שנות ניסיון בפיתוח", 3),
    ("+3 שנות ניסיון", 3),
    ("2-4 שנות ניסיון", 2),
    ("ניסיון של 3 שנים לפחות", 3),
    ("שנתיים ניסיון בפיתוח Python", 2),
    ("ניסיון של שנתיים לפחות", 2),
    ("לפחות שלוש שנות ניסיון", 3),
    ("שנת ניסיון בפיתוח", 1),
    ("בין 2 ל-4 שנות ניסיון", 2),
])
def test_experience_extraction(f, text, years):
    assert f.extract_experience(text).years_min == years, text


@pytest.mark.parametrize("text", [
    "",
    "Great team, modern stack, Python and React.",
    "Founded 10 years ago, we build cool things.",           # no experience context + 'ago'
    "Our company has been around for 20 years.",              # bare years, no experience word
    "Graduated in 2025, React experience.",                   # a year is not a duration
    "ללא ניסיון",
    "החברה קיימת 15 שנים",                                   # bare years, no experience word
])
def test_no_experience_stated(f, text):
    assert f.extract_experience(text).years_min is None, text


def test_multiple_mentions_take_max(f):
    text = "Requirements:\n3+ years Python\n1+ years Docker"
    assert f.extract_experience(text).years_min == 3


def test_advantage_lines_ignored(f):
    text = "Requirements:\n1+ years with Node.js\nNice to have: 5+ years with Go - advantage"
    r = f.extract_experience(text)
    assert r.years_min == 1
    assert any("5+" in m for m in r.ignored)


def test_advantage_block_header_ignored(f):
    text = "דרישות:\nשנת ניסיון בפיתוח\nיתרונות:\n3+ שנות ניסיון ב-Go\nניסיון של 4 שנים בענן"
    r = f.extract_experience(text)
    assert r.years_min == 1
    assert len(r.ignored) == 2


def test_advantage_block_ends_at_next_header(f):
    text = "Advantages:\n5 years experience in Go\nRequirements:\n3+ years experience"
    assert f.extract_experience(text).years_min == 3


@pytest.mark.parametrize("text,skip", [
    ("2+ years of experience", True),     # boundary: 2 >= cutoff 2
    ("0-2 years of experience", False),
    ("1-2 years of experience", False),
    ("1 year of experience", False),
    ("2-4 years of experience", True),
    ("no years stated here", False),
])
def test_experience_cutoff(f, text, skip):
    d = f.evaluate("Backend Developer", text)
    assert d.skip is skip
    if skip:
        assert d.rule == "experience" and d.reason.startswith("Skipped - Experience")


# ----------------------------------------------------------------------------- student

@pytest.mark.parametrize("title,desc,skip", [
    ("Student Software Developer", "Student position, Python", True),
    ("סטודנט/ית למדעי המחשב", "משרה חלקית", True),
    ("משרת סטודנטים - פיתוח", "", True),
    ("לסטודנטים - QA", "", True),
    ("Student / Graduate Developer", "", False),
    ("Students and Graduates - Software", "", False),
    ("משרת סטודנט/ית - פיתוח תוכנה", "מתאים גם לבוגרים טריים", False),
    ("Junior Developer", "Students and graduates welcome", False),
    ("Junior Developer", "Suitable for students in their last year", False),  # Q2: title only
])
def test_student_matrix(f, title, desc, skip):
    d = f.evaluate(title, desc)
    assert (d.skip and d.rule == "student") is skip, (title, desc)


def test_senior_wins_over_experience(f):
    d = f.evaluate("Senior Developer", "5+ years of experience")
    assert d.rule == "senior" and d.years_min == 5
