from __future__ import annotations

import pytest

from job_hunter.core.categories import Categorizer


@pytest.mark.parametrize("text,expected", [
    ("Python developer, React.js frontend, Node backend", ["Python", "React", "Node.js"]),
    ("C++ and C# and C", ["C++", "C#", "C"]),
    ("C/C++ embedded", ["C", "C++"]),
    ("JavaScript only", ["JavaScript"]),
    ("Java only", ["Java"]),
    ("React Native mobile app", ["React Native"]),            # longest wins: no plain React
    ("React-Native and React", ["React Native", "React"]),
    ("Node.js, NodeJS, node", ["Node.js"]),
    ("Postgres + MySQL", ["PostgreSQL", "MySQL"]),
    ("k8s on AWS", ["Kubernetes", "AWS"]),
    ("Generative AI, LLMs, RAG", ["LLM/GenAI"]),
    ("ASP.NET and .NET Core", [".NET"]),
    ("CI/CD with Jenkins", ["CI/CD", "Jenkins"]),
    ("Golang or Go", ["Go"]),
    ("Let's go to the office", []),                            # Go is case-sensitive
    ("the rest of the team", []),                              # REST is case-sensitive
    ("REST APIs and RESTful services", ["REST"]),
    ("ידע ב-Python ו-Java", ["Python", "Java"]),
    ("ניסיון בפייתון ולינוקס", ["Python", "Linux"]),
    ("Spring Boot microservices", ["Spring", "Microservices"]),
    ("", []),
])
def test_extract_skills(matcher, text, expected):
    assert matcher.extract(text) == expected


def test_canonical_aliases(matcher):
    assert matcher.canonical("react.js") == "React"
    assert matcher.canonical("Node") == "Node.js"
    assert matcher.canonical("LLM/GenAI") == "LLM/GenAI"
    assert matcher.canonical("Cobol") is None
    assert matcher.canonicalize_all(["Node", "node.js", "Cobol"]) == ["Node.js", "Cobol"]


@pytest.fixture
def cat(cfg) -> Categorizer:
    return Categorizer(cfg.search)


@pytest.mark.parametrize("title,desc,name,where", [
    ("QA Automation Engineer", "", "QA Automation", "title"),
    ("Junior AI Developer", "", "AI", "title"),
    ("Machine Learning Engineer", "", "AI", "title"),
    ("Full Stack Developer", "", "Fullstack", "title"),
    ("Full-Stack Developer", "", "Fullstack", "title"),
    ("Backend Developer", "", "Backend", "title"),
    ("Back-End Engineer", "", "Backend", "title"),
    ("Frontend Developer", "", "Frontend", "title"),
    ("DevOps Engineer", "", "DevOps", "title"),
    ("Embedded Software Engineer", "", "Embedded/RT", "title"),
    ("RT Software Engineer", "", "Embedded/RT", "title"),
    ("Cyber Security Researcher", "", "Defense/Security", "title"),
    ("מפתח/ת פולסטאק", "", "Fullstack", "title"),
    ("מפתח/ת צד שרת", "", "Backend", "title"),
    ("בודק/ת תוכנה - אוטומציה", "", "QA Automation", "title"),
    ("מהנדס/ת תוכנה זמן אמת", "", "Embedded/RT", "title"),
    # first match wins in the configured order (QA before Fullstack)
    ("Fullstack QA Engineer", "", "QA Automation", "title"),
    # title checked before description
    ("Frontend Developer", "Backend APIs in Python", "Frontend", "title"),
    ("Software Engineer", "Join our backend team", "Backend", "description"),
    ("Software Engineer", "Great place to work", "General", None),
    ("Rtl Support Specialist", "", "General", None),  # 'rt' whole word only
])
def test_categorize(cat, title, desc, name, where):
    m = cat.categorize(title, desc)
    assert (m.category.name, m.where) == (name, where)
