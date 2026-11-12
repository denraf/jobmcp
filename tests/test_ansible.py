"""Static checks for the Ansible playbook (no Ansible or servers needed)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / "ansible"
YAML_FILES = sorted(
    p
    for p in ANSIBLE.rglob("*")
    if p.suffix in (".yml", ".example") and p.is_file()
)


def test_there_is_a_playbook():
    assert (ANSIBLE / "site.yml").exists() and YAML_FILES


@pytest.mark.parametrize(
    "path", YAML_FILES, ids=lambda p: str(p.relative_to(ANSIBLE))
)
def test_every_yaml_file_parses(path):
    list(yaml.safe_load_all(path.read_text()))


def test_the_playbook_runs_the_database_before_the_web_server():
    plays = yaml.safe_load((ANSIBLE / "site.yml").read_text())
    hosts = [p["hosts"] for p in plays]
    assert hosts.index("db") < hosts.index("web")
    web = next(p for p in plays if p["hosts"] == "web")
    assert web["roles"] == ["app", "apache"]


@pytest.fixture
def jinja():
    return pytest.importorskip("jinja2")


def render(jinja, template: Path, **ctx) -> str:
    env = jinja.Environment(
        trim_blocks=True,
        undefined=jinja.StrictUndefined,
        extensions=[],
    )
    env.filters["quote"] = lambda s: s
    return env.from_string(template.read_text()).render(**ctx)


TEMPLATES = sorted(ANSIBLE.rglob("*.j2"))
VARS = dict(
    db_user="livejobs",
    vault_db_password="a" * 24,
    db_host="10.0.0.20",
    db_port=3306,
    db_name="livejobs",
    livejobs_domain="jobs.example.org",
    mail_from="x <x@example.org>",
    smtp_host="smtp",
    smtp_port=587,
    smtp_user="u",
    vault_smtp_password="p" * 12,
    smtp_security="starttls",
    livejobs_allow_registration=1,
    livejobs_env_extra={"SYNC_WORKERS": 16},
    livejobs_user="livejobs",
    livejobs_app_dir="/opt/livejobs",
    livejobs_port=8000,
    db_bind_address="10.0.0.20",
    mysql_max_allowed_packet="64M",
    mysql_innodb_buffer_pool_size="1G",
    apache_cert_file="/c/fullchain.pem",
    apache_key_file="/c/privkey.pem",
    tls_ready=True,
)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_every_template_renders(jinja, path):
    assert render(jinja, path, **VARS).strip()


def test_env_template_only_sets_real_settings_and_is_systemd_safe(jinja):
    text = render(
        jinja, ANSIBLE / "roles/app/templates/livejobs.env.j2", **VARS
    )
    config = (ROOT / "livejobs" / "config.py").read_text()
    keys = re.findall(r"^(LIVEJOBS_[A-Z_]+)=", text, re.M)
    assert keys
    for k in keys:
        assert f'"{k}"' in config, f"{k} is not a livejobs setting"
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z_]+=('[^'$]*'|[0-9]+)", line) or (
                line.startswith("LIVEJOBS_DATABASE_URL='")
            ), line
    assert "LIVEJOBS_TRUST_PROXY=1" in text


def test_the_vhost_cannot_be_spoofed_and_hides_metrics(jinja):
    vhost = render(
        jinja, ANSIBLE / "roles/apache/templates/livejobs.conf.j2", **VARS
    )
    assert "ProxyAddHeaders Off" in vhost  # the client's own header is dropped
    assert (
        'RequestHeader set X-Forwarded-For "%{LIVEJOBS_CLIENT_IP}e"' in vhost
    )
    assert vhost.index('ProxyPass "/metrics" "!"') < vhost.index(
        'ProxyPass "/" '
    )
    assert "flushpackets=on" in vhost  # MCP streams
    assert "RedirectMatch 404" in vhost and "/metrics" in vhost
    http_only = render(
        jinja,
        ANSIBLE / "roles/apache/templates/livejobs.conf.j2",
        **{**VARS, "tls_ready": False},
    )
    assert "<VirtualHost *:443>" not in http_only
    assert "ProxyPass" not in http_only  # never serve logins over plain HTTP


def test_mysql_listens_on_the_private_address_only(jinja):
    cnf = render(
        jinja, ANSIBLE / "roles/database/templates/livejobs.cnf.j2", **VARS
    )
    assert "bind-address = 10.0.0.20" in cnf
    settings = [line for line in cnf.splitlines() if not line.startswith("#")]
    assert not any("0.0.0.0" in line for line in settings)


def test_the_database_user_is_tied_to_the_web_servers_address():
    tasks = (ANSIBLE / "roles/database/tasks/main.yml").read_text()
    assert "'{{ db_user }}'@'{{ db_client_address }}'" in tasks
    assert "'%'" not in tasks  # never "from anywhere"
    assert "from {{ db_client_address }} to {{ db_bind_address }}" in tasks


def test_no_real_secret_is_committed():
    main = (ANSIBLE / "group_vars/all/main.yml").read_text()
    assert "vault_" not in main  # secrets live only in the vault file
    assert not (ANSIBLE / "group_vars/all/vault.yml").exists()
    assert not (ANSIBLE / "inventory.ini").exists()
