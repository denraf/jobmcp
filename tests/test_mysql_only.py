"""jobmcp supports MySQL only. These pin that down, and exercise the MySQL
behaviour the schema relies on."""

from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from jobmcp import db
from jobmcp.orm import Board, Job
from jobmcp.services import boards, sync

from .helpers import fj, mk_board

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "sqlite:///x.db",
        "postgresql://u@h/d",
        "mysql://u@h/d",
        "mysql+aiomysql://u@h/d",
    ],
)
def test_only_pymysql_urls_are_accepted(url):
    with pytest.raises(ValueError, match="needs MySQL"):
        db.mysql_url(url)


def test_charset_is_always_utf8mb4():
    assert db.mysql_url("mysql+pymysql://u:p@h/d").endswith("?charset=utf8mb4")
    assert "utf8mb4" in db.mysql_url("mysql+pymysql://u:p@h/d?charset=utf8mb4")
    with pytest.raises(ValueError, match="utf8mb4"):
        db.mysql_url("mysql+pymysql://u:p@h/d?charset=latin1")
    with pytest.raises(ValueError, match="utf8mb4"):
        db.mysql_url(
            "mysql+pymysql://u:p@h/d?charset=utf8"
        )  # MySQL's "utf8" is only 3 bytes: it would truncate emoji


def test_no_trace_of_sqlite_remains_in_the_project():
    checked = [
        *ROOT.glob("jobmcp/**/*.py"),
        *ROOT.glob("jobmcp/web/**/*.js"),
        *ROOT.glob("scripts/*"),
        *ROOT.glob("deploy/**/*"),
        *ROOT.glob("docs/*"),
        *ROOT.glob("docker/**/*"),
        ROOT / "README.md",
        ROOT / "pyproject.toml",
        ROOT / "docker-compose.yml",
        ROOT / ".gitignore",
    ]
    offenders = []
    for f in checked:
        if (
            f.is_file()
            and f.suffix not in (".woff2", ".pyc")
            and "sqlite" in f.read_text(errors="ignore").lower()
        ):
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, (
        f"SQLite is not supported; remove it from: {offenders}"
    )
    assert "StaticPool" not in (ROOT / "jobmcp" / "db.py").read_text()


@pytest.mark.usefixtures("fresh_db")
class TestMySqlBehaviour:
    def test_slugs_are_case_sensitive(self):
        """Ashby/SmartRecruiters slugs differ by case; the default MySQL
        collation would merge them (and fail the unique key)."""
        with db.session() as s:
            s.add_all(
                [
                    Board(vendor="ashby", board="Acme", hits=0),
                    Board(vendor="ashby", board="acme", hits=0),
                ]
            )
        with db.session() as s:
            assert {b.board for b in s.scalars(select(Board))} == {
                "Acme",
                "acme",
            }
        with pytest.raises(IntegrityError), db.session() as s:
            s.add(
                Board(vendor="ashby", board="Acme", hits=0)
            )  # the same key twice is still refused

    def test_four_byte_unicode_round_trips(self):
        title, company = "Ingénieur données 🚀 工程师", "Café ☕ GmbH"
        sync.sync_board(
            mk_board(),
            lambda v, s: [
                fj(
                    title,
                    company=company,
                    loc="Zürich 🇨🇭",
                    desc="Описание 📄 — résumé",
                )
            ],
        )
        with db.session() as s:
            j = s.scalar(select(Job))
            assert (j.title, j.company, j.location, j.description) == (
                title,
                company,
                "Zürich 🇨🇭",
                "Описание 📄 — résumé",
            )

    def test_descriptions_over_64kb_are_not_truncated(self):
        big = (
            "word " * 40000
        )  # ~200 KB; a plain TEXT column would cut this at 64 KB
        with db.session() as s:
            b = boards.add_board(s, "greenhouse", "acme")
            s.add(
                Job(
                    uid="big0000000000001",
                    board_id=b.id,
                    vendor="greenhouse",
                    title="t",
                    description=big,
                )
            )
        with db.session() as s:
            assert len(s.scalar(select(Job.description))) == len(big)

    def test_tables_are_innodb_utf8mb4(self):
        with db.engine().connect() as c:
            rows = c.execute(
                text(
                    "SELECT table_name, engine, table_collation FROM "
                    "information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            ).all()
        assert rows and all(
            r[1] == "InnoDB" and r[2].startswith("utf8mb4") for r in rows
        )
        with db.engine().connect() as c:
            col = c.execute(
                text(
                    "SELECT data_type FROM information_schema.columns WHERE "
                    "table_schema = DATABASE() "
                    "AND table_name = 'jobs' AND column_name = 'description'"
                )
            ).scalar()
        assert col == "mediumtext"

    def test_foreign_keys_are_enforced_by_the_database(self):
        with pytest.raises(IntegrityError), db.session() as s:
            s.add(
                Job(
                    uid="orphan0000000001",
                    board_id=999999,
                    vendor="greenhouse",
                    title="x",
                )
            )


def test_cli_reports_a_non_mysql_url_without_a_traceback(monkeypatch, capsys):
    from jobmcp import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["--db-url", "sqlite:///x.db", "init-db"])
    assert str(e.value).startswith("error: jobmcp needs MySQL")
    monkeypatch.setenv("JOBMCP_DATABASE_URL", "postgresql://u@h/d")
    from jobmcp import config

    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u@h/d")
    with pytest.raises(SystemExit) as e:
        cli.main(["init-db"])
    assert "needs MySQL" in str(e.value)
