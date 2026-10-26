"""30 hand-written FAKE jobs exercising every filter/scoring path. Fake companies, fake URLs.

Each entry has an expected outcome checked by tests/test_pipeline_seed.py.
3 entries are deliberate duplicates (fingerprint / URL / source id), so 27 rows are created.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from job_hunter.core.models import utcnow
from job_hunter.core.pipeline import JobPosting

SEED_SOURCE = "seed"
SEED_SOURCE_B = "seed_b"


@dataclass(frozen=True)
class SeedJob:
    key: str
    title: str
    company: str
    location: str
    description: str
    days_ago: int | None
    expected: str  # 'Skipped' | 'Shortlisted' | 'New' | 'duplicate'
    expected_rule: str | None = None  # senior | student | experience
    expected_category: str | None = None
    source: str = SEED_SOURCE
    source_job_id: str | None = None
    url: str | None = None

    def posting(self, now: datetime | None = None) -> JobPosting:
        now = now or utcnow()
        sid = self.source_job_id or self.key
        return JobPosting(
            source=self.source,
            source_job_id=sid,
            title=self.title,
            company=self.company,
            location=self.location,
            description_text=self.description,
            url=self.url or f"https://jobs.example.test/{self.source}/{sid}",
            posted_at=(now - timedelta(days=self.days_ago)) if self.days_ago is not None else None,
        )


SEED_JOBS: list[SeedJob] = [
    # --- Shortlisted / pass paths -------------------------------------------------------------
    SeedJob("s01", "Junior Backend Developer (Python)", "TechNova Ltd", "Tel Aviv",
            "We are looking for a junior backend developer.\nRequirements: Python, Django, PostgreSQL, "
            "Docker.\n0-2 years of experience.", 1, "Shortlisted", expected_category="Backend"),
    SeedJob("s11", "Frontend Developer", "PixelForge", "Ramat Gan",
            "Build modern web apps.\n0-2 years experience with React, JavaScript, HTML and CSS.",
            3, "Shortlisted", expected_category="Frontend"),
    SeedJob("s12", "QA Automation Engineer", "TestRight", "Petah Tikva",
            "Requirements:\n1-2 years of experience with Python and Selenium.", 5, "Shortlisted",
            expected_category="QA Automation"),
    SeedJob("s16", "משרת סטודנט/ית - פיתוח תוכנה", "קודלאב", "חיפה",
            "משרה חלקית בפיתוח. מתאים גם לבוגרים טריים.\nדרישות: Java, C++.", 2, "Shortlisted",
            expected_category="General"),
    SeedJob("s17", "Junior AI Developer", "NeuralNest", "Herzliya",
            "Work with LLM-based products using Python, FastAPI and Docker. Graduates welcome.", 1,
            "Shortlisted", expected_category="AI"),
    SeedJob("s19", "Embedded Software Engineer", "Iron Shield Systems", "Haifa",
            "Develop real-time software for defense systems.\nC, C++, RTOS, Linux.", 4, "Shortlisted",
            expected_category="Embedded/RT"),
    SeedJob("s20", "Cyber Security Researcher", "RedDune", "Beer Sheva",
            "Vulnerability research and tooling in Python.", 20, "Shortlisted",
            expected_category="Defense/Security"),
    SeedJob("s23", "מפתח/ת Java ג׳וניור", "בייטווקס", "רחובות",
            "ללא ניסיון קודם.\nידע ב-Java ו-Spring Boot.", 1, "Shortlisted", expected_category="General"),
    SeedJob("s24", "Backend Developer Node.js", "StreamPath", "Tel Aviv",
            "Requirements:\n1+ years with Node.js\nNice to have: 5+ years with Go - advantage", 6,
            "Shortlisted", expected_category="Backend"),
    SeedJob("s28", "מפתח/ת Frontend", "גלים דיגיטל", "ירושלים",
            "פיתוח ממשקים ב-React, TypeScript, HTML, CSS.\nמתאים לבוגרי תואר.", 12, "Shortlisted",
            expected_category="Frontend"),
    SeedJob("s30", "Fullstack Developer", "BrightLoop", "Netanya",
            "Experience with React, Node.js and MongoDB.\n1 year of experience required.", 15,
            "Shortlisted", expected_category="Fullstack"),
    # --- New (below threshold) ----------------------------------------------------------------
    SeedJob("s18", "DevOps Engineer", "CloudHarbor", "Tel Aviv",
            "Kubernetes, Terraform, AWS, Jenkins and CI/CD pipelines.", 10, "New",
            expected_category="DevOps"),
    SeedJob("s21", "Software Engineer", "GopherWorks", "Remote",
            "Go, Kubernetes, AWS, gRPC services.", 30, "New", expected_category="General"),
    SeedJob("s22", "Sales Development Representative", "SellFast", "Tel Aviv",
            "Generate leads, manage the CRM and book meetings with B2B customers.", 2, "New",
            expected_category="General"),
    SeedJob("s29", "Data Analyst", "NumbersCo", "Holon",
            "SQL, Excel and Tableau. Analyze business data and build reports.", 8, "New",
            expected_category="General"),
    # --- Skipped: senior title ----------------------------------------------------------------
    SeedJob("s02", "Senior Backend Engineer", "CloudCorp", "Tel Aviv",
            "Python, Django, PostgreSQL.", 1, "Skipped", "senior"),
    SeedJob("s03", "Sr. Frontend Developer", "WebWave", "Ramat Gan",
            "React, TypeScript.", 2, "Skipped", "senior"),
    SeedJob("s04", "Team Lead, Fullstack", "StackHouse", "Tel Aviv",
            "Lead a team of developers. React and Node.js.", 3, "Skipped", "senior"),
    SeedJob("s05", "מפתח/ת Backend בכיר/ה", "אופק תוכנה", "תל אביב",
            "פיתוח שרתים ב-Python.", 2, "Skipped", "senior"),
    SeedJob("s06", "ראש צוות פיתוח", "מגדלור טכנולוגיות", "הרצליה",
            "ניהול צוות מפתחים.", 4, "Skipped", "senior"),
    SeedJob("s07", "מפתח תוכנה ומנהל פרויקטים", "שחר מערכות", "פתח תקווה",
            "פיתוח וניהול פרויקטים.", 5, "Skipped", "senior"),
    SeedJob("s08", "Software Architect", "BlueprintIO", "Tel Aviv",
            "Design large-scale systems.", 1, "Skipped", "senior"),
    # --- Skipped: experience ------------------------------------------------------------------
    SeedJob("s09", "Backend Developer", "DataMill", "Tel Aviv",
            "Requirements:\n3+ years of experience with Node.js and PostgreSQL.", 2, "Skipped",
            "experience"),
    SeedJob("s10", "Fullstack Developer", "AppNest", "Haifa",
            "2-4 years of experience in React and Node.js.", 3, "Skipped", "experience"),
    SeedJob("s13", "מפתח/ת Fullstack", "רקיע", "רמת גן",
            "דרישות:\nלפחות 3 שנות ניסיון בפיתוח React ו-Node.js.", 2, "Skipped", "experience"),
    SeedJob("s14", "מפתח/ת Backend", "נתיב קוד", "ראשון לציון",
            "דרישות: שנתיים ניסיון בפיתוח Python.", 6, "Skipped", "experience"),
    # --- Skipped: student-only ----------------------------------------------------------------
    SeedJob("s15", "Student Software Developer", "CampusSoft", "Rehovot",
            "Student position, 20 hours per week. Python.", 1, "Skipped", "student"),
    # --- Duplicates (must not create rows) ----------------------------------------------------
    # same job as s01 on another site: company/title/location differ only in case/punctuation/suffix
    SeedJob("d25", "junior backend developer - python", "TechNova, Ltd.", "Tel Aviv, Israel",
            "Same job, other board.", 1, "duplicate", source=SEED_SOURCE_B, source_job_id="b-1001"),
    # same URL as s12 (tracking params + trailing slash + www) with different text
    SeedJob("d26", "QA Engineer (Automation)", "Test Right Group", "Petah Tikva",
            "Reposted.", 5, "duplicate", source=SEED_SOURCE_B, source_job_id="b-1002",
            url="https://WWW.jobs.example.test/seed/s12/?utm_source=newsletter&utm_medium=email"),
    # same (source, source_job_id) as s11 with a changed title
    SeedJob("d27", "Frontend Developer (React)", "PixelForge Inc", "Ramat Gan",
            "Edited listing.", 3, "duplicate", source=SEED_SOURCE, source_job_id="s11",
            url="https://jobs.example.test/seed/s11-edited"),
]


def seed_postings(now: datetime | None = None) -> list[JobPosting]:
    now = now or utcnow()
    return [j.posting(now) for j in SEED_JOBS]
