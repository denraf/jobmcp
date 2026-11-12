# Installing jobmcp on your own server

This guide takes a fresh VPS to a running, HTTPS-protected jobmcp with hourly job fetching, email, backups and
Prometheus metrics. It is written for **Ubuntu 24.04 or Debian 12**; other Linux distributions work with different
package names. Commands are meant to be pasted in order.

> **Status of this guide.** The application, the hourly sync, the email flows and the metrics were exercised
> end to end on a development machine against MySQL 8.4. The systemd units (including the scheduler), Caddy/nginx
> configs, cron files and Prometheus files in `deploy/` are standard but were **not** run on a real VPS. Follow the "Verify" steps after each
> section and expect to adjust paths or distro details.

Using **Ansible**, with the web server and the database on separate machines, and Apache instead of Caddy/nginx? Use
[`ansible/`](../ansible/README.md): it does everything in this guide for that layout (steps 2 to 9 and 11).

Prefer containers? `docker-compose.yml` runs the whole stack (database, web, scheduler); see the README. This guide is
the systemd route, which suits a VPS you manage directly.

Want to try it first? Skip all of this and run `scripts/dev.sh` on your own computer (see the
[README](../README.md#try-it-locally)).

---

## 1. What you are installing

```
                  internet
                     │ 443
              ┌──────▼──────┐
              │ Caddy/nginx │  TLS, forwards to 127.0.0.1:8000, hides /metrics
              └──────┬──────┘
                     │
          ┌──────────▼───────────┐        ┌───────────────────────────┐
          │ jobmcp-web         │        │ jobmcp-scheduler        │ hourly job fetch, monthly
          │ site + API + MCP     │        │ (a second systemd service)│ Common Crawl refresh, daily
          │ :8000, /metrics      │        └──────────┬────────────────┘ platform check
          └──────────┬───────────┘                   │
                     └─────────────┬─────────────────┘
                              ┌────▼────┐
                              │  MySQL  │   127.0.0.1 only
                              └─────────┘
        Prometheus (optional) ── scrapes 127.0.0.1:8000/metrics
```

**Requirements**

| | |
|---|---|
| Server | 2 vCPU / 4 GB RAM and 40 GB disk is comfortable once many boards are enabled (1 vCPU / 2 GB starts fine; lower `JOBMCP_SYNC_WORKERS` and `JOBMCP_IMPORT_WORKERS` on a small machine). Job descriptions are stored in full, so the database grows with the number of boards |
| OS | Ubuntu 22.04 or 24.04, or Debian 12 (Python 3.10+; 22.04 has 3.10, 24.04 has 3.12) |
| Domain | A name such as `jobmcp.ai` pointing at the server (A/AAAA record) |
| Email | An SMTP account to send from (your provider's, or a transactional service). Required: signups confirm by email |
| Access | A sudo user over SSH |

---

## 2. Prepare the server

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git curl mysql-server util-linux

# Firewall: SSH and web only. MySQL and the app stay on localhost.
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw --force enable
```

Create an unprivileged user and the directories:

```bash
sudo useradd --system --home-dir /opt/jobmcp --shell /usr/sbin/nologin jobmcp
sudo mkdir -p /opt/jobmcp /etc/jobmcp /var/log/jobmcp /var/backups/jobmcp
sudo chown jobmcp:jobmcp /opt/jobmcp /var/log/jobmcp /var/backups/jobmcp
sudo chown root:jobmcp /etc/jobmcp && sudo chmod 750 /etc/jobmcp
```

---

## 3. MySQL

MySQL listens on `127.0.0.1` by default on Ubuntu/Debian. Keep it that way. jobmcp is developed and tested against
MySQL 8.4; Ubuntu's `mysql-server` package is 8.0, which should work too but has not been tested. It supports MySQL only.

```bash
DBPASS=$(openssl rand -hex 24)      # letters and digits only: it goes inside a URL
echo "$DBPASS"                      # note it down; you need it in step 5

sudo mysql <<SQL
CREATE DATABASE jobmcp CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'jobmcp'@'localhost' IDENTIFIED BY '${DBPASS}';
GRANT ALL PRIVILEGES ON jobmcp.* TO 'jobmcp'@'localhost';
SQL
```

**Verify:** `mysql -ujobmcp -p jobmcp -e 'select 1'` (enter the password) prints `1`.

---

## 4. Get the code and install it

Copy the project to the server, either by git:

```bash
sudo -u jobmcp git clone <your-repository-url> /opt/jobmcp
```

or from your own machine with rsync:

```bash
rsync -a --exclude .venv --exclude .local --exclude __pycache__ ./ you@your-server:/tmp/jobmcp-src/
ssh you@your-server 'sudo rsync -a /tmp/jobmcp-src/ /opt/jobmcp/ && sudo chown -R jobmcp:jobmcp /opt/jobmcp'
```

Then build the virtualenv:

```bash
cd /opt/jobmcp
sudo -u jobmcp python3 -m venv .venv
sudo -u jobmcp .venv/bin/pip install --upgrade pip
sudo -u jobmcp .venv/bin/pip install -e .
```

(`pip install -e .` needs the checkout to stay in `/opt/jobmcp`; use `.venv/bin/pip install .` instead if you
would rather remove the source afterwards. The web assets are packaged either way.)

**Verify:** `sudo -u jobmcp /opt/jobmcp/.venv/bin/jobmcp --help` lists the commands.

---

## 5. Configure

```bash
sudo cp /opt/jobmcp/deploy/jobmcp.env.example /etc/jobmcp/jobmcp.env
sudo chown root:jobmcp /etc/jobmcp/jobmcp.env && sudo chmod 640 /etc/jobmcp/jobmcp.env
sudo nano /etc/jobmcp/jobmcp.env
```

Set at least:

| Variable | Set it to |
|---|---|
| `JOBMCP_DATABASE_URL` | `mysql+pymysql://jobmcp:<DBPASS>@127.0.0.1:3306/jobmcp?charset=utf8mb4` |
| `JOBMCP_PUBLIC_BASE_URL` | `https://jobmcp.ai` (your real address; it goes into emailed links) |
| `JOBMCP_SMTP_*`, `JOBMCP_MAIL_FROM` | your mail account (step 8) |

The file is read both by systemd and by shell scripts, so keep every line as `KEY='value'`, with no `$` or `'` inside a
value. All settings are listed in the [reference](#environment-variable-reference) at the end.

Create the tables and the first admin:

```bash
sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin init-db
sudo -u jobmcp bash -c "JOBMCP_ADMIN_PASSWORD='choose-a-long-password' /opt/jobmcp/deploy/bin/jobmcp-admin create-admin --email you@example.com"
```

`jobmcp-admin` runs any `jobmcp` command with the production environment loaded. `init-db` is safe to run on every
deploy: it also upgrades the schema of an older database.

---

## 6. Run the web app under systemd

```bash
sudo cp /opt/jobmcp/deploy/systemd/jobmcp-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobmcp-web
```

**Verify:**

```bash
systemctl status jobmcp-web --no-pager
curl -s http://127.0.0.1:8000/healthz         # {"ok":true}
journalctl -u jobmcp-web -n 30 --no-pager   # look for "jobmcp.config" warnings
```

If the log shows a warning about the mail backend or `localhost`, fix `/etc/jobmcp/jobmcp.env` and
`sudo systemctl restart jobmcp-web`.

Then start the scheduler. It is what fetches jobs, so a site without it is empty (details in step 9):

```bash
sudo cp /opt/jobmcp/deploy/systemd/jobmcp-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobmcp-scheduler
journalctl -u jobmcp-scheduler -n 20 --no-pager      # "scheduler started ..." and the first hourly sync
```

---

## 7. HTTPS with a reverse proxy

### Option A: Caddy (automatic certificates)

```bash
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt -y install caddy       # if these URLs have moved, see caddyserver.com/docs/install

sudo cp /opt/jobmcp/deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/jobmcp.ai/YOUR.DOMAIN/' /etc/caddy/Caddyfile
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo systemctl reload caddy
```

### Option B: nginx + certbot

```bash
sudo apt -y install nginx certbot python3-certbot-nginx
sudo cp /opt/jobmcp/deploy/nginx/jobmcp.conf /etc/nginx/sites-available/jobmcp.conf
sudo sed -i 's/jobmcp.ai/YOUR.DOMAIN/' /etc/nginx/sites-available/jobmcp.conf
sudo ln -s /etc/nginx/sites-available/jobmcp.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d YOUR.DOMAIN
```

Both configs return 404 for `/metrics`, so it is never public.

**Verify:** open `https://YOUR.DOMAIN`. The home page loads. `curl -I https://YOUR.DOMAIN/metrics` returns 404.
Agents connect to `https://YOUR.DOMAIN/mcp/`.

---

## 8. Email

Signup requires clicking a link in an email, and password reset works the same way, so mail must work.

1. Get SMTP details from your provider (a transactional service such as Postmark, Mailgun, SES or Brevo, or your own
   mail host). Use port 587 with `starttls`, or 465 with `ssl`.
2. Put them in `/etc/jobmcp/jobmcp.env` (`JOBMCP_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_SECURITY`,
   `JOBMCP_MAIL_FROM`, `JOBMCP_MAIL_BACKEND='smtp'`).
3. **Test it:**
   ```bash
   sudo systemctl restart jobmcp-web
   sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin send-test-email --to you@gmail.com
   ```
   It exits non-zero and prints the error if the mail server refuses the message.
4. **Deliverability.** Mail from a new domain lands in spam unless the domain authenticates it. In your DNS add the
   SPF and DKIM records your provider gives you, and a DMARC record such as
   `_dmarc  TXT  "v=DMARC1; p=none; rua=mailto:you@YOUR.DOMAIN"`. Send the test email to a Gmail address and check the
   message's "Show original" for `SPF: PASS` and `DKIM: PASS`.

No mail server and no wish to run one? Set `JOBMCP_REQUIRE_EMAIL_VERIFICATION=0`. Signups are then confirmed
immediately and can sign straight in. Password reset still needs mail; without it, an admin sets a new password with
`PATCH /api/admin/users/{id}`.

---

## 9. Fetch jobs every hour

**Use the scheduler service from step 6.** `jobmcp scheduler` is one process that runs everything recurring:

| What | When |
|---|---|
| Fetch every enabled board that is due, and record the day's per-platform counts (Platform health) | Every hour, and once at startup |
| Refresh the board inventory from Common Crawl | Monthly (see below) |
| Check each platform against a live board (the canary) | Daily at 05:30 UTC |

It logs to the journal (`journalctl -u jobmcp-scheduler -f`) and survives restarts. Do not also install the cron
file or the timers below: they would do the same work twice.

The two alternatives below also work, but **neither runs the daily platform check or records the platform counts**,
so the Platform health table and the `jobmcp_canary_ok` / `jobmcp_vendor_anomaly` alerts stay empty unless you add
`jobmcp canary` to cron yourself. Prefer the scheduler.

**Cron** (an alternative to the scheduler service):

```bash
sudo cp /opt/jobmcp/deploy/cron/jobmcp /etc/cron.d/jobmcp
sudo cp /opt/jobmcp/deploy/logrotate/jobmcp /etc/logrotate.d/jobmcp
echo '30 5 * * * jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin canary >> /var/log/jobmcp/canary.log 2>&1' | sudo tee -a /etc/cron.d/jobmcp
```

That runs `deploy/bin/jobmcp-cron-sync.sh` at minute 0 of every hour as the `jobmcp` user, logging to
`/var/log/jobmcp/sync.log`. A run that is still going when the next one starts is skipped, not stacked.

**systemd timer** (an alternative to the scheduler service; journald logs, catches up after downtime):

```bash
sudo cp /opt/jobmcp/deploy/systemd/jobmcp-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobmcp-sync.timer
systemctl list-timers jobmcp-sync.timer
```

Either way the command is `jobmcp sync-once --slack 10`. **The `--slack 10` matters:** each board syncs on a
60-minute interval (plus up to 10% jitter), so a board synced at 10:00 becomes due at 11:00 to 11:06. Without the slack
window an hourly job would find such boards "not yet due" and each board would end up syncing every *other* hour.
`sync-once` keeps claiming batches until nothing is due, and boards with a per-board interval shorter or longer than an
hour are honoured.

*Prefer a daemon?* `deploy/systemd/jobmcp-worker.service` runs `jobmcp worker`, which syncs boards as they come
due, with no cron or timer. Use it instead of, not alongside, the cron job.

### Choose which companies to fetch

Nothing is fetched until an admin enables a board. There are two ways boards appear:

- **Added by hand** (you know the company): in the web portal, sign in as an admin and open **Job boards**. Pick the
  platform, type the slug (or paste the company's careers page URL) and press **Check board**. It asks the platform
  whether the board exists and shows how many jobs it has. A typo is caught here instead of becoming a dead board.
  **Add and start syncing** then stores the jobs it just found, so they appear immediately. You can add many at once
  (one `platform:slug` or URL per line). Only admins can do this; the portal hides the tab from everyone else and the
  server refuses the requests.
- **Found by Common Crawl** (discovery): boards seen on the web appear in the same list, already *Syncing*; an
  unknown slug disables itself after repeated 404s, and you can disable any board.

The same operations from the command line (`A='sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin'`):

```bash
$A boards add greenhouse stripe              # checks it exists, then adds and enables it
$A boards add ashby Notion --interval 30     # sync this one every 30 minutes (Ashby slugs keep their capitals)
$A boards add-url https://jobs.lever.co/spotify     # from a careers page URL
$A boards import my-boards.txt               # one "platform:slug" or URL per line; "-" reads stdin
$A boards add greenhouse newcorp --no-verify # skip the existence check (board temporarily unreachable)
$A boards list --source manual               # what was added by hand
$A boards enable-top 500 --vendor greenhouse # enable the 500 most-referenced discovered boards
$A boards stats
```

Slugs are normalised (`Stripe` and `stripe` are one Greenhouse board), so adding a board that discovery already found
*adopts* it: it becomes "added by hand" and enabled, never a duplicate. The same actions are in the API under
`/api/admin/boards` (`/probe`, `/import`), browsable at `https://YOUR.DOMAIN/api/docs`.

### The monthly Common Crawl refresh

Discovery reads the newest Common Crawl index, which is published about once a month. `jobmcp inventory --refresh`
does it: if the newest crawl is already fully ingested it prints "nothing to do" and exits 0, otherwise it ingests it,
resuming any pages an earlier attempt missed (Common Crawl's index server often answers 504, so a run can finish
*incomplete*, exit code 75). Every attempt is recorded and shown in the portal under **Job boards**.

With the scheduler service it runs on its own, and you do nothing. The cron and timer alternatives schedule it **daily
on the 5th to the 12th** of each month. Because it does nothing once the crawl is done, that is
real work once a month, plus automatic retries for a run that was cut short. Two equivalent ways to schedule it
without the scheduler service, pick one:

- **cron:** the file you already installed (`/etc/cron.d/jobmcp`) has the line, running
  `deploy/bin/jobmcp-cron-inventory.sh` at 03:00 on days 5-12, logging to `/var/log/jobmcp/inventory.log`.
- **systemd timer:**
  ```bash
  sudo cp /opt/jobmcp/deploy/systemd/jobmcp-inventory.{service,timer} /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now jobmcp-inventory.timer
  ```
  (If you use the timer, remove the inventory line from `/etc/cron.d/jobmcp`.)

You can also start one from the web: an admin opens **Job boards** and presses **Refresh from Common Crawl** (progress
is shown live). Only one refresh runs at a time across the button, the timer/cron and the command line, via a MySQL
lock that frees itself if a process dies.

Run it by hand any time, for example for the first fill (it can take a long time, so use `tmux`):
`$A inventory --refresh`. If Common Crawl refuses or fails 12 requests in a row (`JOBMCP_CC_GIVE_UP_AFTER`) the run
stops instead of working through every remaining query; pages already ingested are kept and the next run resumes.
If it never gets through from your server, test `curl -sI https://index.commoncrawl.org/collinfo.json` from the VM. To limit it to some platforms: `$A inventory --refresh --vendor greenhouse --vendor lever`.
Discovery only proposes boards: **nothing syncs until you enable it.** The metric
`jobmcp_inventory_last_success_timestamp_seconds` and the `JobmcpInventoryStale` alert watch that it keeps working.

**Verify:** run one fetch by hand, then look at the numbers.

```bash
sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin sync-once --slack 10   # "synced N boards: ..."
$A boards stats
curl -s http://127.0.0.1:8000/api/public/stats
```

---

## 10. Metrics (Prometheus)

`/metrics` is on the web app. It answers only to requests that come **directly from 127.0.0.1**; any request that went
through the proxy (it carries an `X-Forwarded-For` header) is refused. To scrape from another machine, set
`JOBMCP_METRICS_TOKEN` and send `Authorization: Bearer <token>`.

```bash
curl -s http://127.0.0.1:8000/metrics | grep ^jobmcp_ | head
```

Install Prometheus on the same server and scrape it:

```bash
sudo apt -y install prometheus
sudo cp /opt/jobmcp/deploy/prometheus/alerts.yml /etc/prometheus/jobmcp-alerts.yml
# add the jobmcp job and rule_files from deploy/prometheus/prometheus.yml to /etc/prometheus/prometheus.yml
promtool check config /etc/prometheus/prometheus.yml
sudo systemctl restart prometheus
```

**What you get**

| Metric | Meaning |
|---|---|
| `jobmcp_sync_last_success_timestamp_seconds` | When any board last synced. Alert if it goes stale |
| `jobmcp_sync_runs_total{outcome}` | Sync runs, `ok` / `error` |
| `jobmcp_sync_jobs_new_total`, `…_closed_total` | Jobs discovered / closed by syncing |
| `jobmcp_boards{vendor,status}` | Boards by platform and `discovered`/`enabled`/`disabled` |
| `jobmcp_vendor_anomaly{vendor,direction}`, `jobmcp_vendor_jobs_zscore{vendor}` | `1` when a platform's open postings are far outside their previous week (`drop` is the one to alert on); the z-score behind it |
| `jobmcp_canary_ok{vendor}`, `jobmcp_canary_checked_timestamp_seconds{vendor}` | Daily live check of each platform: `0` means its parser no longer finds real postings |
| `jobmcp_boards_by_source{source,status}` | Boards `manual` (added by an admin) vs `crawl` (found by Common Crawl) |
| `jobmcp_inventory_running`, `jobmcp_inventory_last_success_timestamp_seconds`, `jobmcp_inventory_runs_total{status}`, `jobmcp_inventory_boards_new_total` | The monthly Common Crawl refresh: last complete run, runs by outcome, boards discovered |
| `jobmcp_boards_failing{vendor}`, `jobmcp_boards_overdue` | Boards whose last sync failed / that missed their time |
| `jobmcp_jobs_active{vendor}`, `jobmcp_jobs_stored` | Open and total jobs |
| `jobmcp_users{role,verified}`, `jobmcp_applications{status}`, `jobmcp_saved_jobs`, `jobmcp_mcp_tokens_active` | Usage |
| `jobmcp_http_requests_total{method,handler,status}`, `jobmcp_http_request_duration_seconds` | Web traffic, by route template (not raw URLs) |
| `jobmcp_auth_events_total{event}` | `login`, `login_failed`, `register`, `email_verified`, `reset_requested`, `password_reset` |
| `jobmcp_emails_total{purpose,outcome}` | Emails `sent`/`failed` per purpose |
| `jobmcp_mcp_tool_calls_total{tool,outcome}` | Agent tool calls, `ok`/`refused`/`error` |
| `jobmcp_db_up` | 1 if the metrics query could reach the database |

The sync metrics are read from the database at scrape time (cached 30 s), not kept in memory, because the hourly sync
runs in a different process from the web server. So they are correct whichever process did the work. Process, Python
and GC metrics come from the standard client library.

Useful queries: jobs added in the last day `increase(jobmcp_sync_jobs_new_total[1d])`; 95th percentile latency
`histogram_quantile(0.95, sum by (le) (rate(jobmcp_http_request_duration_seconds_bucket[5m])))`; applications per
status `jobmcp_applications`. `deploy/prometheus/alerts.yml` has ready alerts for a stale sync, overdue boards, a high
failure rate, 5xx responses, mail failures and login floods.

---

## 11. Backups

```bash
sudo tee /etc/jobmcp/backup.my.cnf >/dev/null <<EOF
[client]
user=jobmcp
password=YOUR_DB_PASSWORD
EOF
sudo chown jobmcp:jobmcp /etc/jobmcp/backup.my.cnf && sudo chmod 600 /etc/jobmcp/backup.my.cnf
sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-backup.sh        # writes /var/backups/jobmcp/jobmcp-<time>.sql.gz
```

Schedule it nightly at 03:15 (14 dumps are kept). With the scheduler service use the backup-only cron file; the
all-cron setup in step 9 already includes the same line:

```bash
sudo cp /opt/jobmcp/deploy/cron/jobmcp-backup /etc/cron.d/jobmcp-backup
```

 **Copy the dumps off the server** (rsync, rclone
or your provider's snapshots). A backup on the same disk is not a backup. Test a restore at least once:
`gunzip -c dump.sql.gz | mysql jobmcp_restore_test`.

---

## 12. Upgrading

```bash
cd /opt/jobmcp
sudo -u jobmcp git pull                      # or rsync the new source over it
sudo -u jobmcp .venv/bin/pip install -e .
sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin init-db      # applies schema upgrades
sudo systemctl restart jobmcp-web jobmcp-scheduler
```

If you are upgrading from a version that cut job descriptions at 4,000 characters, re-fetch everything once so stored
descriptions are replaced immediately: `sudo -u jobmcp /opt/jobmcp/deploy/bin/jobmcp-admin sync-once --all`.

Take a backup first. Upgrading from a version before email verification: `init-db` adds the column and marks all
existing accounts as verified, so nobody is locked out.

---

## 13. Hardening checklist

- [ ] `ufw status` shows only 22, 80 and 443 open; MySQL listens on 127.0.0.1 (`ss -ltnp | grep 3306`).
- [ ] `/etc/jobmcp/jobmcp.env` is `640 root:jobmcp`.
- [ ] SSH uses keys only (`PasswordAuthentication no`); consider `fail2ban`.
- [ ] `curl -I https://YOUR.DOMAIN/metrics` returns 404.
- [ ] SPF, DKIM and DMARC pass for your sending domain.
- [ ] With nginx, `X-Forwarded-For` is set to `$remote_addr` (the shipped config does this). `$proxy_add_x_forwarded_for`
      passes a client-supplied header through and lets attackers dodge the login rate limit. Caddy replaces it.
- [ ] `JOBMCP_TRUST_PROXY=1` only because Caddy/nginx is in front. Without a proxy, leave it `0`, or clients could
      spoof their IP past the login rate limit.
- [ ] Backups run and are copied off the machine.
- [ ] `unattended-upgrades` is enabled for security patches.
- [ ] Registration is the setting you want (`JOBMCP_ALLOW_REGISTRATION=0` to close it after inviting people).

Know the limits: the login rate limiter is per process and per IP (add proxy-level limits for heavier exposure), there
is no admin web UI (use the CLI or `/api/docs`), and the browser session token lives in `localStorage`, protected by the
site's strict content-security policy.

---

## 14. Troubleshooting

| Symptom | Check |
|---|---|
| Site returns 502 | `systemctl status jobmcp-web`; `journalctl -u jobmcp-web -n 50` |
| `Access denied for user 'jobmcp'` | DB password in `JOBMCP_DATABASE_URL` matches step 3; special characters must be URL-encoded (use hex) |
| `cryptography package is required` | `.venv/bin/pip install cryptography` (it is a dependency; reinstall step 4) |
| Users never get the confirmation email | `jobmcp-admin send-test-email`; `jobmcp_emails_total{outcome="failed"}`; the startup warning about the mail backend; spam folder |
| Links in emails point at `localhost` | Set `JOBMCP_PUBLIC_BASE_URL`, restart the web service |
| The Common Crawl refresh never finishes | `tail /var/log/jobmcp/inventory.log`; "incomplete" means Common Crawl's index server errored, and the next daily run resumes it; the history is in the portal under Job boards |
| No new jobs appear | `systemctl status jobmcp-scheduler` and `journalctl -u jobmcp-scheduler -n 50` (or `tail /var/log/jobmcp/sync.log` / `journalctl -u jobmcp-sync` for the cron/timer setup); `boards stats` shows no *enabled* boards; `jobmcp_sync_last_success_timestamp_seconds` |
| Some boards keep failing | `boards list --status enabled`; a board that returns 404 three times in a row is disabled automatically |
| Sync log says "previous sync is still running" every hour | One run takes over an hour; lower the number of enabled boards, or raise `--workers`, or use the worker daemon |
| `/metrics` returns 403 | By design from anywhere but 127.0.0.1 with no proxy header. Scrape `127.0.0.1:8000` directly, or set `JOBMCP_METRICS_TOKEN` |
| Agent can't connect | The URL is `https://YOUR.DOMAIN/mcp/` (trailing slash), with header `Authorization: Bearer <token>` from the portal |
| Rate-limited while testing | `JOBMCP_AUTH_RATE_PER_MIN` (default 10 per IP per minute) |

---

## Environment variable reference

All variables are optional except the database URL, the public base URL and (with verification on) SMTP.

| Variable | Default | Purpose |
|---|---|---|
| `JOBMCP_DATABASE_URL` | local MySQL | SQLAlchemy URL. `mysql+pymysql://user:pw@host/db?charset=utf8mb4` |
| `JOBMCP_PUBLIC_BASE_URL` | `http://localhost:8000` | Public address; used in emailed links and agent setup commands |
| `JOBMCP_PUBLIC_MCP_URL` | `<base>/mcp/` | Override the MCP URL shown in the portal |
| `JOBMCP_TRUST_PROXY` | `0` | `1` when behind Caddy/nginx: use `X-Forwarded-For` for the client IP |
| `JOBMCP_ALLOW_REGISTRATION` | `1` | `0` closes self-signup |
| `JOBMCP_REQUIRE_EMAIL_VERIFICATION` | `1` | `0` confirms signups immediately (no mail needed) |
| `JOBMCP_VERIFY_TTL_HOURS` | `24` | Lifetime of the confirmation link |
| `JOBMCP_RESET_TTL_MINUTES` | `60` | Lifetime of the password-reset link |
| `JOBMCP_EMAIL_COOLDOWN_SECONDS` | `60` | Minimum gap between emails of one kind to one address |
| `JOBMCP_MAIL_BACKEND` | `console` | `smtp` in production; `console` logs the message (dev) |
| `JOBMCP_MAIL_FROM` | `jobmcp <no-reply@localhost>` | From header |
| `JOBMCP_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` | `localhost` / `587` / – / – | SMTP account |
| `JOBMCP_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` |
| `JOBMCP_AUTH_RATE_PER_MIN` | `10` | Login/signup/reset attempts per IP per minute |
| `JOBMCP_SESSION_TTL_HOURS` | `24` | Browser session lifetime |
| `JOBMCP_DAILY_APPLY_CAP` | `20` | Default daily application cap for new users |
| `JOBMCP_SYNC_INTERVAL_MIN` | `60` | Default minutes between syncs of one board |
| `JOBMCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `JOBMCP_LOG_FORMAT` | `text` | `text`, or `json` (one object per line) |
| `JOBMCP_LOG_LEVELS` | empty | Per-logger levels: `jobmcp.sync=DEBUG,jobmcp.http=DEBUG` |
| `JOBMCP_CC_MIN_INTERVAL` | `1.0` | Minimum seconds between Common Crawl requests, across all threads |
| `JOBMCP_CC_WORKERS` | `3` | Threads reading Common Crawl (their own pool, apart from the sync workers) |
| `JOBMCP_CC_RETRIES` | `8` | Retries per Common Crawl request, with a shared, growing pause |
| `JOBMCP_CC_GIVE_UP_AFTER` | `12` | Consecutive Common Crawl failures after which a crawl stops (and resumes next run) |
| `JOBMCP_IMPORT_WORKERS` | `6` | Boards a long import checks and stores at once (lower than the sync workers because of memory) |
| `JOBMCP_CANARY_BOARDS` | empty | Extra boards for the daily platform check: `vendor:board,vendor:board` |
| `JOBMCP_SYNC_WORKERS` | `16` | Boards fetched in parallel by one sync run (thread pool) |
| `JOBMCP_SYNC_AFTER_REFRESH` | `1` | Start syncing new boards as soon as a Common Crawl refresh ends |
| `JOBMCP_GONE_DISABLE_AFTER` | `3` | Consecutive 404s before a board is disabled |
| `JOBMCP_CACHE_SECONDS` | `30` | How long the public jobs list is reused (server memory, and `Cache-Control` for browsers and proxies). `0` turns it off |
| `JOBMCP_METRICS_TOKEN` | – | If set, `/metrics` requires this bearer token (and loopback alone no longer suffices) |
| `JOBMCP_METRICS_PUBLIC` | `0` | `1` serves `/metrics` to anyone (not recommended) |
| `JOBMCP_METRICS_CACHE_SECONDS` | `30` | How long database-derived metrics are cached |
