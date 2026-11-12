# Install jobmcp with Ansible: web server and database server on separate machines

```
  internet ─443─▶ Apache (already running, other sites too)
                    │  reverse proxy, 127.0.0.1:8000
              ┌─────▼──────────────────────────┐             ┌───────────────────┐
  web VM      │ jobmcp-web   jobmcp-scheduler ├── 3306 ──▶│ MySQL             │  db VM
              └────────────────────────────────┘  private net │ bind: private IP  │
                                                              │ nightly backup    │
                                                              └───────────────────┘
```

The playbook (`site.yml`) does, in order:

| Machine | What |
|---|---|
| **db** | Installs MySQL, makes it listen on the private address only, creates the database, the app user (allowed from the web VM's address only) and a backup user, opens port 3306 to the web VM only (ufw), installs the nightly backup |
| **web** | Creates the `jobmcp` user, ships the code, builds the virtualenv, writes `/etc/jobmcp/jobmcp.env`, creates the tables and the first admin, runs `jobmcp-web` and `jobmcp-scheduler` under systemd |
| **web** | Adds **one** Apache site for your domain (modules `proxy proxy_http headers rewrite ssl setenvif alias`), gets a Let's Encrypt certificate with certbot, redirects HTTP to HTTPS, hides `/metrics`. Your other Apache sites are not touched |

Only Ansible's own modules are used, so `ansible-core` is enough (no extra collections). Target machines: Ubuntu
22.04 or 24.04.

## Before you run it

1. **A private network between the two VMs.** MySQL's traffic is not encrypted, so use your provider's private
   network or a VPN (WireGuard), and put those addresses in `private_ip`. Do not point it at public addresses.
2. **DNS:** an A (and AAAA) record for your domain pointing at the web VM, and port 80 reachable from the internet
   (the certificate check uses it).
3. **SSH access with sudo** to both machines from the machine you run Ansible on.

```bash
cd ansible
pip install ansible-core                      # or: brew install ansible / apt install ansible
cp inventory.example.ini inventory.ini        # your hosts and their private IPs
$EDITOR group_vars/all/main.yml               # domain, admin email, SMTP, buffer pool size
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
$EDITOR group_vars/all/vault.yml              # passwords: letters and digits only (openssl rand -hex 24)
ansible-vault encrypt group_vars/all/vault.yml
```

## Run it

```bash
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --ask-vault-pass --check --diff      # a dry run first (the certificate step needs a real run)
ansible-playbook site.yml --ask-vault-pass
```

Run it again whenever you change a setting or want to deploy new code: it only changes what differs, creates or
upgrades the tables (`init-db`), and restarts the app when the code or settings changed. To do one machine:
`--limit db` or `--limit web`.

When it ends it has already checked `https://YOUR.DOMAIN/healthz` (200) and `/metrics` (404) from your machine. Then
sign in at your domain as `jobmcp_admin_email` with `vault_admin_password`, open **Job boards** and add companies,
or press **Refresh from Common Crawl**. The scheduler fetches every hour on its own.

## Choices you can make in `group_vars/all/main.yml`

- **Code delivery.** `jobmcp_source: archive` (default) packs this checkout on your machine and unpacks it on the
  web VM, so you need no git server. `git` clones `jobmcp_git_repo` instead.
- **Certificate.** `apache_certbot: true` obtains and renews one with certbot. Set it `false` and give
  `apache_cert_file` / `apache_key_file` to use your own (an existing wildcard certificate, say).
- **Firewalls.** `db_manage_firewall` (default on) allows SSH and the database port from the web VM, nothing else.
  `web_manage_firewall` is off by default because that VM already runs Apache and may have rules of its own.
- **Anything else:** any `JOBMCP_*` setting from [`docs/INSTALL.md`](../docs/INSTALL.md) goes in `jobmcp_env_extra`.

## What to know

- **Client IP and the login rate limit.** jobmcp trusts the first entry of `X-Forwarded-For`. Apache normally keeps
  whatever header the client sent in front of the real address, so the vhost turns that off (`ProxyAddHeaders Off`) and
  sets the header from the connection itself.
- **One web VM, one database VM.** The scheduler must run on one machine only, so the playbook refuses an inventory
  with more than one host in `[web]`.
- **Backups** land in `/var/backups/jobmcp` on the **database** VM (14 kept). Copy them off that machine: a backup on the
  same disk is not a backup.
- **Passwords** in `vault.yml` change the live passwords on the next run (the MySQL users are altered, the env file is
  rewritten, the app restarts).
- **Upgrades** are a re-run. Files deleted from the source are not removed from `/opt/jobmcp` in archive mode.
- **Not run on real servers.** The playbook was checked statically (YAML, templates, settings against `config.py`) but
  `ansible-core` was not available where it was written, so use `--syntax-check` and `--check --diff` first and expect
  to adjust.
