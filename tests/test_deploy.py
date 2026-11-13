"""The deployment files and docs must stay true to the code: names, flags,
paths and metrics."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from jobmcp import cli

ROOT = Path(__file__).resolve().parent.parent
DEPLOY, DOCS = ROOT / "deploy", ROOT / "docs"
CONFIG_SRC = (ROOT / "jobmcp" / "config.py").read_text()
METRICS_SRC = (ROOT / "jobmcp" / "metrics.py").read_text()


def env_names(text):
    # names ending in "_" are prefix shorthand in prose (JOBMCP_SMTP_*), not
    # variables
    return {
        n
        for n in re.findall(r"\bJOBMCP_[A-Z0-9_]+\b", text)
        if not n.endswith("_")
    }


def test_every_documented_env_var_exists_in_config():
    known = env_names(CONFIG_SRC) | {
        "JOBMCP_TOKEN",
        "JOBMCP_ADMIN_PASSWORD",
        "JOBMCP_ADMIN_EMAIL",
        "JOBMCP_TEST_DATABASE_URL",
    }
    shell_only = {  # read by the deploy scripts themselves, not by the app
        "JOBMCP_ENV_FILE",
        "JOBMCP_APP_DIR",
        "JOBMCP_LOCK_FILE",
        "JOBMCP_INVENTORY_LOCK_FILE",
        "JOBMCP_BACKUP_DIR",
        "JOBMCP_DB_NAME",
        "JOBMCP_BACKUP_KEEP",
        "JOBMCP_MYSQL_DEFAULTS",
        "JOBMCP_RUN_MIGRATIONS",
        "JOBMCP_DB_WAIT_SECONDS",
    }
    for f in [
        DEPLOY / "jobmcp.env.example",
        DOCS / "INSTALL.md",
        ROOT / "README.md",
        ROOT / "scripts" / "dev.sh",
        ROOT / "docker" / "app.env",
        ROOT / "docker-compose.yml",
        ROOT / "docker" / "entrypoint.sh",
    ]:
        unknown = env_names(f.read_text()) - known - shell_only
        assert not unknown, (
            f"{f.name} mentions settings that config.py doesn't read: "
            f"{sorted(unknown)}"
        )


def test_env_example_is_valid_for_both_systemd_and_bash():
    out = subprocess.run(
        ["bash", "-c", f"set -a; . {DEPLOY / 'jobmcp.env.example'}; env"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    got = dict(
        line.split("=", 1)
        for line in out.splitlines()
        if line.startswith("JOBMCP_")
    )
    assert (
        got["JOBMCP_MAIL_BACKEND"] == "smtp"
        and got["JOBMCP_SMTP_PORT"] == "587"
    )
    assert got["JOBMCP_DATABASE_URL"].startswith("mysql+pymysql://")
    for line in (DEPLOY / "jobmcp.env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z_]+=('[^'$]*'|[0-9]+)(\s+#.*)?", line), (
                f"not plain KEY='value': {line}"
            )  # systemd + bash safe


@pytest.mark.parametrize(
    "unit",
    sorted((DEPLOY / "systemd").glob("*.service")),
    ids=lambda p: p.name,
)
def test_systemd_service_commands_are_real_cli_invocations(unit):
    text = unit.read_text()
    assert (
        "[Service]" in text
        and "EnvironmentFile=/etc/jobmcp/jobmcp.env" in text
        and "User=jobmcp" in text
    )
    for line in re.findall(r"^ExecStart=(.+)$", text, re.M):
        argv = shlex.split(line)
        assert Path(argv[0]).name in ("jobmcp", "flock"), argv
        i = next(i for i, a in enumerate(argv) if a.endswith("/jobmcp"))
        assert Path(argv[i]).as_posix().startswith("/opt/jobmcp/.venv/bin/")
        cli.build_parser().parse_args(
            argv[i + 1 :]
        )  # raises SystemExit on a flag/command that doesn't exist


def test_hourly_units_actually_run_hourly_with_slack():
    sync = (DEPLOY / "systemd" / "jobmcp-sync.service").read_text()
    assert (
        "sync-once --slack 10" in sync
        and "RuntimeDirectory=jobmcp" in sync
        and "-E 0" in sync
    )
    assert re.search(
        r"^OnCalendar=hourly$",
        (DEPLOY / "systemd" / "jobmcp-sync.timer").read_text(),
        re.M,
    )
    cron = (DEPLOY / "cron" / "jobmcp").read_text()
    assert re.search(
        r"^0\s+\*\s+\*\s+\*\s+\*\s+jobmcp\s+/opt/jobmcp/deploy/bin/jobmcp-cron-sync\.sh",
        cron,
        re.M,
    )
    assert (
        "sync-once --slack 10"
        in (DEPLOY / "bin" / "jobmcp-cron-sync.sh").read_text()
    )


def test_cron_file_is_well_formed_and_points_at_real_scripts():
    for line in (DEPLOY / "cron" / "jobmcp").read_text().splitlines():
        if not line.strip() or line.startswith(("#", "SHELL=", "PATH=")):
            continue
        fields = line.split(None, 6)
        assert len(fields) == 7 and fields[5] == "jobmcp", (
            line
        )  # /etc/cron.d lines have a user column
        script = ROOT / Path(fields[6].split()[0]).relative_to("/opt/jobmcp")
        assert script.exists() and os.access(script, os.X_OK), (
            f"{script} missing or not executable"
        )


@pytest.mark.parametrize(
    "script", sorted((DEPLOY / "bin").iterdir()), ids=lambda p: p.name
)
def test_scripts_are_executable_and_valid_bash(script):
    assert os.access(script, os.X_OK)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    assert (ROOT / "scripts" / "dev.sh").exists() and subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / "dev.sh")]
    ).returncode == 0


def test_every_script_and_unit_the_install_guide_references_exists():
    guide = (DOCS / "INSTALL.md").read_text()
    for ref in set(re.findall(r"/opt/jobmcp/(deploy/[\w./,{}-]+)", guide)):
        ref = ref.rstrip(".")
        if "{" in ref:  # brace expansion: jobmcp-sync.{service,timer}
            base, rest = ref.split("{")
            for ext in rest.rstrip("}").split(","):
                assert ext, ref
                assert (ROOT / (base + ext)).exists(), base + ext
        else:
            assert (ROOT / ref).exists(), (
                f"INSTALL.md references missing {ref}"
            )


def exported_metric_names():
    names = set()
    for n in re.findall(r'"(jobmcp_[a-z_]+)"', METRICS_SRC):
        names.add(n)
        names |= {
            n + "_total",
            n + "_bucket",
            n + "_sum",
            n + "_count",
        }  # prometheus_client suffixes
    return names


def test_alert_rules_only_use_metrics_that_exist():
    yaml = pytest.importorskip("yaml")
    rules = yaml.safe_load((DEPLOY / "prometheus" / "alerts.yml").read_text())[
        "groups"
    ][0]["rules"]
    assert len(rules) >= 6 and all(
        {"alert", "expr", "annotations"} <= r.keys() for r in rules
    )
    exported = exported_metric_names()
    for r in rules:
        for used in set(re.findall(r"\bjobmcp_[a-z_]+\b", r["expr"])):
            assert used in exported, (
                f"alert {r['alert']} uses {used}, which metrics.py "
                "never exports"
            )
    yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text())


def test_install_guide_metrics_table_matches_the_exporter():
    guide = (DOCS / "INSTALL.md").read_text()
    section = guide[
        guide.index("**What you get**") : guide.index(
            "The sync metrics are read from the database"
        )
    ]
    exported = exported_metric_names()
    for used in set(re.findall(r"`(jobmcp_[a-z_]+)", section)):
        assert used in exported, (
            f"INSTALL.md documents {used}, which metrics.py doesn't export"
        )


def test_proxy_configs_hide_metrics_from_the_internet():
    assert re.search(
        r"@metrics path /metrics.*\n\s*respond @metrics 404",
        (DEPLOY / "caddy" / "Caddyfile").read_text(),
    )
    assert re.search(
        r"location = /metrics \{ return 404; \}",
        (DEPLOY / "nginx" / "jobmcp.conf").read_text(),
    )
    apache = (DEPLOY / "apache" / "jobmcp.conf").read_text()
    assert 'RedirectMatch 404 "^/metrics(/.*)?$"' in apache
    assert 'ProxyPass "/metrics" "!"' in apache


def test_apache_config_does_not_pass_on_a_client_supplied_address():
    apache = (DEPLOY / "apache" / "jobmcp.conf").read_text()
    assert "ProxyAddHeaders Off" in apache
    assert 'RequestHeader set X-Forwarded-For "%{JOBMCP_CLIENT_IP}e"' in apache


def test_inventory_refresh_is_scheduled_monthly_with_daily_retries():
    timer = (DEPLOY / "systemd" / "jobmcp-inventory.timer").read_text()
    assert re.search(
        r"^OnCalendar=\*-\*-05\.\.12 03:00:00$", timer, re.M
    )  # days 5-12: retries until the crawl is complete
    assert (
        "inventory --refresh"
        in (DEPLOY / "systemd" / "jobmcp-inventory.service").read_text()
    )
    cron = (DEPLOY / "cron" / "jobmcp").read_text()
    assert re.search(
        r"^0\s+3\s+5-12\s+\*\s+\*\s+jobmcp\s+/opt/jobmcp/deploy/bin/jobmcp-cron-inventory\.sh",
        cron,
        re.M,
    )
    script = (DEPLOY / "bin" / "jobmcp-cron-inventory.sh").read_text()
    assert (
        "inventory --refresh" in script and "75)" in script
    )  # "incomplete" is retried, not treated as a failure


# ── the containerised stack ─────────────────────────────────────────────
def compose():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_compose_runs_the_whole_application_not_just_the_database():
    c = compose()["services"]
    assert set(c) == {"db", "web", "scheduler"}
    assert c["web"]["build"] == "." and c["scheduler"]["build"] == "."
    assert (
        c["web"]["environment"]["JOBMCP_RUN_MIGRATIONS"] == "1"
    )  # exactly one service prepares the schema
    assert "JOBMCP_RUN_MIGRATIONS" not in c["scheduler"]["environment"]
    assert c["web"]["ports"] == [
        "127.0.0.1:8000:8000"
    ]  # published on loopback only
    assert c["scheduler"]["command"] == ["jobmcp", "scheduler"]
    assert "db" in c["web"]["depends_on"] and {"db", "web"} <= set(
        c["scheduler"]["depends_on"]
    )


def test_compose_has_no_variable_substitution_so_both_runtimes_agree():
    """Apple's container-compose passes ${VAR:-default} through as literal text
    (the app then crashed parsing it)."""
    code = "\n".join(
        line
        for line in (ROOT / "docker-compose.yml").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert not re.search(r"\$\{", code)
    assert (ROOT / "docker" / "app.env").exists()
    for svc in ("web", "scheduler"):
        assert compose()["services"][svc]["env_file"] == ["docker/app.env"]


def test_compose_services_reach_the_database_by_a_name_both_resolve():
    c = compose()["services"]
    alias = c["db"]["networks"]["default"]["aliases"]
    assert (
        alias == ["db.jobmcp"]
        # Docker: network alias. Apple container: DNS domain `jobmcp`
        # (one-time
        # sudo step)
    )
    for svc in ("web", "scheduler"):
        url = c[svc]["environment"]["JOBMCP_DATABASE_URL"]
        assert "@db.jobmcp:3306/" in url and url.endswith("charset=utf8mb4")
    assert (
        "container system dns create jobmcp"
        in (ROOT / "docker-compose.yml").read_text()
    )


def test_app_env_file_is_plain_and_only_sets_known_settings():
    names = []
    for line in (ROOT / "docker" / "app.env").read_text().splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"JOBMCP_[A-Z_]+=[^'\"$]*", line), (
                f"not plain KEY=value: {line}"
            )  # env_file does no quoting
            names.append(line.split("=")[0])
    assert {
        "JOBMCP_PUBLIC_BASE_URL",
        "JOBMCP_MAIL_BACKEND",
        "JOBMCP_ADMIN_EMAIL",
    } <= set(names)
    assert len(names) == len(set(names))  # no accidental duplicates


def test_dockerfile_and_entrypoint():
    docker = (ROOT / "Dockerfile").read_text()
    assert (
        'ENTRYPOINT ["jobmcp-entrypoint"]' in docker
        and "COPY docker/entrypoint.sh" in docker
    )
    argv = json_cmd(docker)
    cli.build_parser().parse_args(
        argv[1:]
    )  # `jobmcp serve --host 0.0.0.0 --port 8000` is a real invocation
    assert "--host" in argv and "0.0.0.0" in argv
    assert (
        "pip install ." in docker
        # the installed package, so package-data (the website) is what is
        # tested
        # here
    )
    entry = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert (
        subprocess.run(
            ["sh", "-n", str(ROOT / "docker" / "entrypoint.sh")]
        ).returncode
        == 0
    )
    # it must wait for the database, prepare the schema, and drop privileges
    # for good
    assert (
        "wait-db" in entry and "init-db" in entry and "ensure-admin" in entry
    )
    assert "setpriv --reuid=jobmcp" in entry and "--no-new-privs" in entry
    # and it must FAIL CLOSED: no silent fallback to running the app as root
    code = "\n".join(
        line
        for line in entry.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "|| exec" not in code and "2>/dev/null" not in code
    assert "USER " not in docker.replace(
        "# ", "#"
    )  # the entrypoint (not a USER line) drops privileges, see its comment


def json_cmd(dockerfile):
    import json

    return json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))


def test_wait_db_explains_an_unresolvable_database_host(monkeypatch):
    from jobmcp import db

    monkeypatch.setattr(
        db, "init", lambda url=None: None
    )  # main() would rebind the engine; keep the one set below
    monkeypatch.setattr(
        db,
        "_engine",
        db.create_engine(
            db.mysql_url("mysql+pymysql://u:p@no-such-host.invalid:3306/x")
        ),
    )
    with pytest.raises(SystemExit) as e:
        cli.main(["wait-db", "--timeout", "3"])
    msg = str(e.value)
    assert (
        "does not resolve" in msg
        and "container system dns create jobmcp" in msg
    )


def test_pep8_workflow_runs_on_every_pull_request_and_matches_the_config():
    import yaml

    wf = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "pep8.yml").read_text()
    )
    triggers = wf.get("on", wf.get(True))  # YAML reads a bare `on` as True
    assert "pull_request" in triggers
    steps = "\n".join(s.get("run", "") for s in wf["jobs"]["pep8"]["steps"])
    assert "ruff check" in steps and "ruff format --check" in steps
    cfg = (ROOT / "pyproject.toml").read_text()
    assert 'select = ["E", "W", "F"]' in cfg and "line-length = 79" in cfg
