from __future__ import annotations

import pytest

from job_hunter.cli import main


@pytest.fixture
def run_cli(project, tmp_path, monkeypatch, capsys):
    import job_hunter.core.cv_loader as cvl
    from tests.conftest import fake_extractor

    monkeypatch.setattr(cvl, "extract_pdf_text", fake_extractor)
    # sync_cvs captured the default at definition time -> patch its default too
    monkeypatch.setattr(cvl.sync_cvs, "__defaults__", (fake_extractor, None))
    db = tmp_path / "cli.db"

    def _run(*args: str) -> tuple[int, str]:
        code = main(["--db", str(db), "--config-dir", str(project / "config"), "--root", str(project), *args])
        return code, capsys.readouterr().out

    return _run


def test_cli_end_to_end(run_cli):
    code, out = run_cli("init-db")
    assert code == 0 and "Database ready" in out

    code, out = run_cli("load-cvs")
    assert code == 0 and "[main]" in out and "OK" in out

    code, out = run_cli("seed")
    assert code == 0 and "new=27" in out

    code, out = run_cli("seed")
    assert code == 0 and "new=0" in out

    code, out = run_cli("list", "--status", "Shortlisted")
    assert code == 0 and "Junior AI Developer" in out and "Senior" not in out

    code, out = run_cli("list", "--min-score", "85")
    assert code == 0 and "(2 jobs)" in out  # s01 + s17 (90 each)

    code, out = run_cli("explain", "1")
    assert code == 0 and "TOTAL" in out and "category: Backend" in out

    code, out = run_cli("explain", "999")
    assert code == 1

    code, out = run_cli("process")
    assert code == 0 and "Processed 0" in out

    code, out = run_cli("rescore")
    assert code == 0 and "changed=0" in out

    code, out = run_cli("stats")
    assert code == 0 and "Jobs: 27" in out and "Recent runs" in out


def test_cli_config_error_exit_code(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "x.db"), "--config-dir", str(tmp_path / "nope"), "init-db"])
    assert code == 2
    assert "Config error" in capsys.readouterr().err
