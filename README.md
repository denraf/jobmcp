# livejobs

Discover every company career board on the major ATSs via Common Crawl, sync the ones **you** choose into MySQL on a schedule, and let each user's own AI agent (Claude, Codex, Gemini — anything that speaks MCP) work through the jobs that match their preferences, with a per-user ledger of what was applied to.

**Privacy by construction:** livejobs never applies on anyone's behalf and has no place to put a résumé, name, phone number or address. Users keep those with their own agent; the service holds an email, a password hash, job preferences, saved job ids and a status per job. The website says so, and `tests/test_site.py` checks the schema keeps that promise.

Inspired by [ybendiks/jobsweep](https://github.com/ybendiks/jobsweep).

```
Common Crawl ─► inventory ─► boards (status: enabled, synced at once)
                                   │  admin enables slugs (API / CLI)
                                   ▼
                  worker: claim due boards ─► vendor parser ─► jobs (MySQL)
                                                                   │
        user preferences ─► matching ◄─────────────────────────────┘
                                │
              MCP (per-user token) ─► agent applies ─► applications ledger
```

## Try it locally

livejobs runs on **MySQL 8** (there is no other supported database). You need a container runtime for the bundled
MySQL (Docker with the Compose plugin, **or** Apple's `container` on macOS 26), or a MySQL 8 of your own. No mail
server is needed.

```bash
scripts/dev.sh                 # http://localhost:8000
scripts/dev.sh --reset         # drop and recreate all tables first
scripts/dev.sh --no-seed       # skip the sample boards (works offline)
scripts/test.sh                # run the tests
```

`scripts/dev.sh` creates a virtualenv, installs the dependencies, makes sure MySQL is running (it starts the one in
`docker-compose.yml` with whichever runtime is running, if nothing is listening on `127.0.0.1:3306`; allow about 30 s
the first time while MySQL initialises), creates the tables and
`admin@example.com` / `dev-admin-password` and `user@example.com` / `dev-user-password`, adds three sample boards, runs
the sync worker in the background, and serves the site, API docs (`/api/docs`), `/metrics` and the MCP endpoint.
**Emails are printed in that terminal**, so to try signup, sign up with any address and copy the verification link from
the log (the same for "Forgot your password").

**Apple's `container` (macOS 26)** instead of Docker: install it once and start its service (no Docker needed; both
`scripts/dev.sh` and `scripts/test.sh` then use `container-compose` automatically):

```bash
brew install container container-compose
container system start --enable-kernel-install     # once per boot; or: brew services start container
```

Stop the database with `docker compose down` or `container-compose down` (the data lives in a volume and survives).
If `container-compose up` fails with "XPC connection error: Connection invalid", the `container` service isn't
installed or running: do the `brew install` and `container system start` commands above.

Using your own MySQL instead of a container:

```bash
LIVEJOBS_DATABASE_URL='mysql+pymysql://user:password@127.0.0.1:3306/livejobs_dev?charset=utf8mb4' scripts/dev.sh
```

The dev database's name must end in `_dev`: `--reset` drops every table and refuses to run on anything else.

## Run the whole app in containers

`docker-compose.yml` runs the complete stack: **`db`** (MySQL 8), **`web`** (the site, API and MCP endpoint) and
**`scheduler`** (fetches the latest jobs every hour and refreshes the Common Crawl inventory once a month, with no cron
needed). Everything is built from the `Dockerfile` in this repository.

```bash
docker compose up -d --build        # Docker
container-compose build && container-compose up -d     # Apple's container (macOS 26), see the one-time step below

docker compose logs web             # first start: the generated admin password, and every verification/reset link
open http://localhost:8000
docker compose down                 # stop; the database lives in a volume and survives
```

- **First admin:** created on first start from `LIVEJOBS_ADMIN_EMAIL` (default `admin@example.com`) in `docker/app.env`.
  With no `LIVEJOBS_ADMIN_PASSWORD` set, a random password is generated and printed **once** in the `web` logs. An
  existing account is never changed on restart.
- **Settings:** edit `docker/app.env` (public URL, SMTP, registration, metrics token, sync interval). With the default
  `LIVEJOBS_MAIL_BACKEND=console` no email is sent; confirmation and reset links appear in `docker compose logs web`.
- **Startup order doesn't matter:** each container waits for MySQL (and the scheduler for the schema) and reports
  progress while it waits, instead of looking hung. `web` runs `init-db` (which also upgrades an older database).
- **Security:** the app runs as an unprivileged user with no way to regain privileges (the entrypoint starts as root only
  to hand over immediately, and stops the container if it can't). The site is published on `127.0.0.1` only. `/metrics`
  is refused through the published port; set `LIVEJOBS_METRICS_TOKEN` and send it as a bearer token to scrape.
- **Add jobs:** sign in as the admin and open **Job boards** to add companies by slug, or let Common Crawl find them; both start syncing on their own.

**Apple's `container` needs one extra step, once.** Unlike Docker, it has no DNS between containers until you register
a domain, and without it the app can't find the database (`wait-db` says so, after waiting):

```bash
brew install container container-compose
container system start --enable-kernel-install
sudo container system dns create livejobs        # once; needs your admin password
```

The compose file reaches MySQL as `db.livejobs`, which that domain resolves (and a network alias makes the same name
work in Docker). `scripts/dev.sh` and `scripts/test.sh` use only the `db` service and need none of this.

## Run it on a server

[`docs/INSTALL.md`](docs/INSTALL.md) is a step-by-step guide for a self-hosted VPS (Ubuntu/Debian): MySQL, systemd,
HTTPS with Caddy or nginx, SMTP, the hourly fetch, backups, Prometheus and troubleshooting. The files it uses are in
[`deploy/`](deploy/).
For two machines (web and database separately) and an Apache that is already running, there is an Ansible playbook in
[`ansible/`](ansible/README.md).

## Setup

For a real deployment, follow [`docs/INSTALL.md`](docs/INSTALL.md). The short version, with any MySQL 8 database
(create the database and user first; `docker compose up -d db` gives you a throwaway one named `livejobs_dev`):

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
export LIVEJOBS_DATABASE_URL='mysql+pymysql://USER:PASSWORD@127.0.0.1:3306/DATABASE?charset=utf8mb4'

livejobs init-db            # creates the tables, and upgrades an older database
LIVEJOBS_ADMIN_PASSWORD='…' livejobs create-admin --email you@example.com
```

Run two long-lived processes (systemd / supervisor / compose services):

```bash
livejobs worker                       # syncs boards on their schedule
livejobs serve --port 8000            # website + REST API (/api) + MCP (/mcp/), one origin
```

Fetch the latest jobs **every hour** with cron (`deploy/cron/livejobs`) or a systemd timer
(`deploy/systemd/livejobs-sync.timer`); both run `livejobs sync-once --slack 10`. Or run `livejobs worker` as a
daemon instead. See [Hourly fetch](#hourly-fetch).

Behind a reverse proxy (do this: it terminates TLS), set `LIVEJOBS_PUBLIC_BASE_URL=https://your.host`
so the portal's copy-paste agent commands use the right URL, and `LIVEJOBS_TRUST_PROXY=1` so rate
limiting sees real client IPs. `livejobs mcp` (stdio) and `livejobs mcp --http` remain available for
running the MCP server on its own.

## The website

Served by `livejobs serve`, no build step (plain HTML, CSS and ES modules in `livejobs/web/`).

| page | what |
|---|---|
| `/` | what the service is, and what it deliberately doesn't do (it doesn't apply, and never asks for a résumé or contact details) |
| `/jobs` | public job browser: title, description keywords, location, platform, recency, remote; shareable URLs; no login needed |
| `/register`, `/login`, `/verify`, `/forgot`, `/reset` | self-service accounts: email and password, nothing else; email confirmation and password reset |
| `/portal` | connect-your-agent instructions (create a token; Claude Code, Claude Desktop, Codex, Gemini CLI snippets), preferences, saved jobs, application record, account (change password, delete account); **admins also get a Job boards tab** (add boards by slug or URL, enable/disable, see the Common Crawl refresh history) |
| `/privacy` | exactly what is and isn't stored |

Security posture: strict CSP with no inline script or style (`tests/test_site.py` forbids inline styles,
handlers, `innerHTML` and `eval` in the frontend), job text from third-party career pages is only ever
inserted with `textContent`, job URLs are only linked when `http(s)`, and the session token lives in
localStorage and is revoked on password change and logout.

## Email: verification and password reset

- **Signup** sends a confirmation link; the account can't sign in until it is opened (links work once and expire after
  24 h). **Forgot password** sends a reset link (works once, expires after 1 h); using it signs the user out everywhere
  and also confirms the address. Agent tokens keep working after a reset.
- Signup, resend and forgot-password give the **same answer whether or not the address has an account**, so the forms
  can't be used to find out who is registered. An existing address gets an "you already have an account" email instead.
- Links carry a 256-bit random token that is stored only as a hash. A second email to the same address within 60 s is
  not sent (so the forms can't be used to flood an inbox), and the sign-up/reset pages remove the token from the URL.
- Mail goes through SMTP (`LIVEJOBS_MAIL_BACKEND=smtp`, `LIVEJOBS_SMTP_*`). `console` (the default) only logs the
  message, which is what `scripts/dev.sh` wants; production startup **warns** if verification is on but SMTP isn't set.
  `livejobs send-test-email --to you@example.com` checks your settings and exits non-zero if the server refuses.
- No mail server? `LIVEJOBS_REQUIRE_EMAIL_VERIFICATION=0` confirms signups immediately.
- Accounts created before verification existed are marked verified by `livejobs init-db`, so nobody is locked out.

## Hourly fetch

`livejobs sync-once --slack 10` syncs everything that is due (or due within 10 minutes) and exits; run it hourly. Boards
refresh every `LIVEJOBS_SYNC_INTERVAL_MIN` (default **60**), or per board (`boards add … --interval 30`).

Why `--slack`: a board synced at 10:00 becomes due at 11:00 to 11:06 (interval plus up to 10 % jitter). An hourly job
at 11:00 would see it as "not yet due", and every board would sync every *other* hour. (Verified: without the flag
the run synced 0 boards; with it, all of them.) Overlapping runs are prevented with `flock`, and failures back off
exponentially per board.

## Job descriptions

Descriptions are stored and served **whole**. There is no length cap on what a platform sends (only a safety limit of
200,000 characters, about 13 times the longest real posting, and if it is ever hit the text says so instead of stopping
mid-sentence). Text keeps its structure: paragraphs stay separate and lists stay bulleted, so a qualifications list
reads as a list. Parts of a posting that platforms send in separate fields are included: Lever's `lists` (what you'll
do / who you are) and `additional`, and Recruitee's `requirements`.

Descriptions stored by an earlier version (cut at 4,000 characters, and without Lever's lists) are replaced the next
time their board syncs. To repair everything now instead of waiting for each board's schedule:

```bash
livejobs sync-once --all        # re-fetch every enabled board immediately, ignoring the schedule
# docker compose:   docker compose exec scheduler livejobs sync-once --all
# systemd install:  sudo -u livejobs /opt/livejobs/deploy/bin/livejobs-admin sync-once --all
```

BambooHR, Breezy HR, SmartRecruiters and Workday only expose a job *list* publicly, with no description text, so jobs
from those platforms have none (they link to the company's page).

**Pictures in a posting.** Some companies paste screenshots into their description, and the API delivers them inline
as `[data:image/png;base64,...]` (one Ashby posting has a 298,000-character one). livejobs **keeps them** and the
job page draws them where they were. Only real picture formats are kept (PNG, JPEG, GIF, WebP; an SVG or HTML
"image" becomes `[image]`), the size limit counts text only, and one picture over 2 MB (or more than 8 MB of
pictures per posting) becomes `[image]` rather than being cut in half. Agents and search never see the data: the MCP
`get_job` / `start_application` text has `[image]` where each picture was (a 300 KB base64 blob would swamp an
agent's context), and keyword search ignores it.

## Metrics

`GET /metrics` (Prometheus text format) on the web app. It answers only to requests straight from `127.0.0.1` (never
through a proxy), or with `Authorization: Bearer $LIVEJOBS_METRICS_TOKEN` if you set one. It exports sync health
(last success time, runs by outcome, jobs found, boards failing/overdue), inventory (boards by platform and status,
open jobs by platform), usage (users by role and verification, applications by status, saved jobs, active agent
tokens), HTTP traffic and latency by route template, auth events, email outcomes, MCP tool calls by outcome, and
`livejobs_db_up`. Sync and inventory numbers are computed from the database at scrape time, so they're correct even
though the sync runs in another process. Ready-made scrape config and alert rules are in `deploy/prometheus/`; the
full metric list is in [`docs/INSTALL.md`](docs/INSTALL.md#10-metrics-prometheus).

## Admin: which boards get synced

Every board is synced automatically: one **added by hand** (by an admin) and one **discovered by Common Crawl** both start
as `enabled` and are fetched on the next run (immediately for boards added in the portal, and right after a Common Crawl
refresh finishes). Syncs run in parallel on a thread pool (`LIVEJOBS_SYNC_WORKERS`, default 16). A discovered slug that
turns out not to exist is disabled by its own 404s; an admin can disable any board, and it stays off. Lists of up to 20 lines are
checked within the request; longer ones (up to 100,000 lines) run in the background, a few boards at a time, with live
progress on the page, so a huge paste can't freeze the browser or run the web container out of memory. One import runs
at a time. Boards left `discovered` by an older version are enabled on the next hourly run.

**In the web portal** (admins only: the **Job boards** tab, which opens with counters for how many boards have been added, how many are syncing, added by hand, found by Common Crawl, waiting to be enabled and disabled; non-admins don't see it and the API refuses them):
choose a platform and type the slug, or paste a careers page URL, and **Check board**. It asks the platform whether the
board exists and shows its job count and a few titles, so a typo is caught before it becomes a dead board. **Add and
start syncing** stores those jobs immediately (one fetch, not two). **Add several at once** takes a pasted list and
reports every line (`added`, `already added`, `not found`, `not understood`) without letting bad lines block good ones.
The list below filters by status, source (added by hand / found by Common Crawl), platform and slug, and has
Enable/Disable and Sync now.

**From the command line:**

```bash
livejobs boards add greenhouse stripe             # checks it exists, then adds and enables it
livejobs boards add ashby Notion --interval 30    # sync every 30 minutes
livejobs boards add-url https://jobs.lever.co/spotify
livejobs boards import boards.txt                 # "platform:slug" or a URL per line ("-" reads stdin)
livejobs boards add greenhouse newcorp --no-verify
livejobs boards list --source manual
livejobs boards enable-top 500 --vendor greenhouse   # enable the 500 most-referenced discovered boards
livejobs boards disable greenhouse acme
livejobs boards stats
```

Slugs are validated per platform and **normalised to the same key discovery produces** (`Stripe` and `stripe` are one
Greenhouse board; Ashby and SmartRecruiters keep their capitals; Lever EU boards are `eu/slug`; Personio is `acme.de`;
Workday is `tenant.wd5/site`). Adding a board that discovery already found *adopts* it (becomes "added by hand",
enabled) instead of creating a duplicate. Each manual board records which admin added it. API: `GET
/api/admin/boards/probe`, `POST /api/admin/boards` (`vendor`+`slug` or `url`, `verify`), `POST
/api/admin/boards/import`, `GET /api/admin/vendors` (slug help per platform), plus `PATCH /api/admin/boards/{id}`.

### The monthly Common Crawl refresh

Besides the schedule, an admin can start a refresh at any time with the **Refresh from Common Crawl** button on the Job
boards tab. It returns at once and the run continues in the background (it can take a long time, and keeps going if you
leave the page): the page shows a live progress bar and the board counters rise as boards are discovered. The button
says so if the newest crawl is already ingested, or if Common Crawl can't be reached.

**Only one refresh runs at a time**, whether it was started from the button, by the scheduler or from the command line:
they share a MySQL lock (named per database, so two installs on one server don't interfere). The lock belongs to the
connection, so it frees itself if the process is killed; a run that was cut off is then shown as *failed: interrupted*,
keeps what it found, and starting a refresh again resumes from the pages already ingested rather than from scratch.


`livejobs inventory --refresh` ingests the newest Common Crawl index (published about monthly) **unless it is already
fully ingested**, in which case it does nothing and exits 0. If the index server's 504s cut a run short it exits 75
("incomplete") and the next run resumes only the missing pages. So it is safe to schedule daily on the 5th to the 12th
(`deploy/cron/livejobs` or `deploy/systemd/livejobs-inventory.timer`): real work once a month, with automatic retries.
Each attempt is recorded (`inventory_runs`), shown under **Job boards**, and exported as metrics
(`livejobs_inventory_*`, with an alert if no run has completed in 45 days). Discovery never changes a manual board's
status, interval or owner; it only adds to its `hits`.

**Cadence of cadences:** jobs hourly, Common Crawl monthly, and sync intervals per board
(`LIVEJOBS_SYNC_INTERVAL_MIN`, default 60) are independent.

## Users and roles

| role | can |
|---|---|
| `admin` | everything below, plus create/deactivate users, change roles, control boards, see sync runs |
| `user` | own preferences, saved jobs, matches, applications, MCP tokens |

Anyone can register on the website (always as a plain `user`; set `LIVEJOBS_ALLOW_REGISTRATION=0` to
close it). Admins are created with `livejobs create-admin` or promoted by an admin via `PATCH
/api/admin/users/{id}`. The last active admin can't be demoted, deactivated or deleted. Login and
registration are rate limited per IP (`LIVEJOBS_AUTH_RATE_PER_MIN`, default 10).

Two token kinds, deliberately not interchangeable: **session** tokens (login, expire in 24 h) work on
the REST API only; **MCP** tokens (long-lived, revocable, shown once, stored hashed) work on the MCP
server only — a leaked MCP token can't reach the REST API or any other user's data.

## Preferences (per user, `PUT /me/preferences`)

`title_include` (any-of; **empty matches nothing**), `title_exclude`, `locations`, `remote_only`,
`vendors`, `companies_include/exclude`, `description_exclude`, `max_age_days`,
`max_applications_per_day` (default 20). Terms match whole words; a trailing `*` is a prefix
(`engineer*` → engineering), so excluding `intern` doesn't drop "International".

## Connecting an agent over MCP

1. Log in, then create a token (shown once):
   ```bash
   TOKEN=$(curl -s localhost:8000/api/auth/login -H 'content-type: application/json' \
     -d '{"email":"you@example.com","password":"…"}' | jq -r .token)
   curl -s localhost:8000/api/me/mcp-tokens -H "authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"name":"claude"}'
   ```
2. Point your client at it.

   Remote (HTTP):
   ```bash
   claude mcp add --transport http livejobs https://your-host/mcp --header "Authorization: Bearer lj_m_…"
   ```
   Local (stdio — the process reads the DB directly, so it needs `LIVEJOBS_DATABASE_URL`):
   ```json
   { "mcpServers": { "livejobs": { "command": "livejobs", "args": ["mcp"],
       "env": { "LIVEJOBS_TOKEN": "lj_m_…", "LIVEJOBS_DATABASE_URL": "mysql+pymysql://…" } } } }
   ```
   Codex and Gemini CLI take the same stdio block / HTTP URL + header in their MCP config.

Tools (all act as the token's owner; none takes a user id): `get_my_preferences`, `list_matching_jobs`,
`search_jobs`, `get_job`, `save_job`, `unsave_job`, `list_saved_jobs`, `start_application`, `record_application_result`, `list_my_applications`,
`update_my_preferences`; plus an `apply_to_matching_jobs` prompt.

## Searching jobs

Beyond preference matches, users and agents can search **all** open jobs (from enabled boards) by
title and/or any keyword in the title or description: REST `GET /jobs/search`, MCP tool `search_jobs`.

| param | meaning |
|---|---|
| `title` | terms that must all appear in the **title** |
| `keywords` | terms searched in title **and** description; `match=all` (default) or `any` |
| `location`, `remote_only`, `vendor`, `company`, `max_age_days` | filters |
| `include_applied` | also show jobs this user already applied to / skipped (each hit carries `my_status`) |
| `limit` (≤50), `offset` | paging via `next_offset` |

Syntax for `title` / `keywords`: space-separated terms; `"quoted phrase"`; `engineer*` prefix;
`-intern` to exclude. Matching is whole-word and case-insensitive (`go` ≠ `Google`, but it does match
`Go-to-Market` since `-` is a word boundary). Title hits rank above description-only hits, and each hit
includes a snippet of the matching description text.

```bash
curl -s "localhost:8000/api/jobs/search?title=data+engineer*&keywords=airflow+-intern&remote_only=true" \
  -H "authorization: Bearer $TOKEN"
```

Search scans at most the 2,000 newest candidates that pass the SQL substring filters (`truncated: true`
in the response means the cap was hit — add terms or filters). That is fast at tens of thousands of
jobs; for much larger corpora, add a MySQL FULLTEXT index.

### What "apply" means

**livejobs does not submit applications itself.** It gives the agent the matching jobs and the apply URL;
the agent drives the application form with its own browser tooling, using the résumé and contact
details the *user* keeps on their side (livejobs has no field for them), and reports back only a status. `start_application` claims the job (no duplicate applications, one
row per user+job enforced by a unique key, daily cap enforced), and `record_application_result`
stores `applied` / `failed` / `skipped`. Applied and skipped jobs are never offered again; `failed`
can be retried. Many ATS forms have captchas or logins, so expect some `failed`/`skipped`.

**Questions on the form.** livejobs cannot see the form, so the rules live in the agent's instructions (the MCP
server instructions, the `apply_to_matching_jobs` prompt, and a `before_you_submit` reminder in every
`start_application` reply). The agent must read the whole form and **ask the user** for anything the user has not
already given it: visa sponsorship and work authorization, salary, notice period, relocation, consents and
e-signatures, every custom question the company added, and the voluntary self-identification questions (disability,
veteran status, race or ethnicity, gender, and so on), offering "decline to answer" where the form allows it. It must
not guess or infer an answer (for instance from a name or photo), and asks once at the start of a run about the
common ones so the answers can be reused for the rest of that run. If the user doesn't answer, the job is recorded as
`skipped` with the topic in the note ("needs a visa sponsorship answer"), never the answer itself: the answers go to
the company on its form and are not stored in livejobs. These rules are text the agent follows, not something
livejobs can enforce, so use an agent that asks before it submits.

## Vendors

Greenhouse, Lever (incl. EU), Ashby, Workable, SmartRecruiters, Recruitee, BambooHR, Personio,
Teamtailor, Breezy, Workday, iCIMS, Paylocity and Gem — one module each in `livejobs/vendors/`. To add one: subclass
`Vendor` (or `SubdomainVendor`), implement `board_from_url` and `fetch`, register it in `vendors/__init__.py`.

Two of them work differently from the JSON-API platforms:

- **iCIMS** (board key is the tenant's subdomain, e.g. `careers-acme`): a sitemap lists the postings and each posting
  page is read for its details (schema.org JSON-LD). Only postings not already stored are fetched (at most 150 per
  sync, the rest follow on the next run), so an hourly sync stays cheap.
- **Paylocity** (board key is the tenant's GUID from `recruiting.paylocity.com/recruiting/jobs/All/<guid>`): the job
  list page embeds its postings as a JSON object. The list carries only the start of each description.

A vendor that answers with a page that is not the data (a block page, a Workday listing whose total changes
part-way) is a failed fetch, not an empty board: the board keeps its stored postings and is retried later, instead
of every posting being closed.

## Labels on every posting

Each posting is labelled when it is synced (rules in `livejobs/classify.py`, no external service):

- **workplace**: `remote` or `hybrid`, from the location and title (empty = not stated);
- **seniority**: `intern`, `entry`, `mid` (no level word in the title), `senior`, `lead` (lead, principal, manager)
  or `executive` (director and above);
- **country**: a two-letter code from country names, US states, Canadian provinces and well-known cities;
- **staffing agency**: per company, by whole-word terms in its name or slug (`staffing`, `recruitment`, `executive
  search`, well-known agency names). An admin can override it per board on the Job boards tab.

They are heuristics, so they are for filtering, not facts. They show on the jobs page, filter the public search, the
REST search and the MCP `search_jobs` tool (`workplace`, `seniority`, `country`, `exclude_agencies`), and can be set
in a user's preferences (levels, countries, skip agencies) so the agent's matches respect them. Existing postings are
labelled by `livejobs init-db` (in batches).

## Platform health

- **Trend and alert.** After every hourly sync the open postings per platform are recorded for the day. If a
  platform is 3 standard deviations and at least 10% away from its previous seven days (it needs five days of
  history first), it shows as *Dropped* or *Jumped* in **Job boards > Platform health**, is logged as a warning, and is
  exported as `livejobs_vendor_anomaly{vendor,direction}` (Prometheus alert `LivejobsPlatformJobsDropped`).
- **Live check ("canary").** Daily at 05:30 UTC the scheduler fetches a known-good board of each platform (the
  smallest few enabled boards that last synced fine, then a fixed sample) and checks that real postings come out (a
  title and a link each). A failure is `livejobs_canary_ok{vendor} 0`, a warning in the log, a red *Failing* in the
  admin portal, and the alert `LivejobsCanaryFailing`. Run it any time with the **Run live check now** button or
  `livejobs canary [--vendor lever]` (exit code 1 if anything fails). Add your own boards with
  `LIVEJOBS_CANARY_BOARDS="bamboohr:acme,teamtailor:acme"`.

## Tests

The suite runs against MySQL, because that is what livejobs runs on (case-sensitive slugs, utf8mb4, large text columns
and enforced foreign keys are all exercised for real).

```bash
scripts/test.sh                      # starts MySQL with Docker if needed, then runs pytest
scripts/test.sh -k metrics -x        # arguments go to pytest

# or against your own MySQL 8 (the database name MUST end in _test: the tests drop and recreate every table)
LIVEJOBS_TEST_DATABASE_URL='mysql+pymysql://user:password@127.0.0.1:3306/livejobs_test?charset=utf8mb4' pytest
```

The tests refuse to run on a database whose name doesn't end in `_test`, and stop with a clear message if MySQL isn't
reachable.

## Known limits

- Schema is created with `create_all`; there are no migrations yet (add Alembic before changing columns in production).
- Login/register rate limiting is in-process and per IP (not shared across instances, no per-account lockout); terminate TLS and add proxy-level limits for anything public.
- Email verification and reset rely on your SMTP server delivering mail (set up SPF/DKIM/DMARC); a person who loses access to their mailbox needs an admin (`PATCH /api/admin/users/{id}` sets a new password).
- If someone signs up with *your* address before you do, you can still register: the latest signup attempt wins until an address is confirmed, and an address that is already confirmed is never changed by a signup.
- No admin web UI: admins use the CLI or `/api/admin/*` (the API is browsable at `/api/docs` when running the REST app standalone).
- The session token is stored in the browser's localStorage, so the strict CSP and `textContent`-only rendering are what protect it; there's no cookie/CSRF surface.
- The `/mcp/` endpoint and the REST API run in one process; the MCP SDK allows its session manager to start once per process, so don't embed `livejobs.site:app` twice in one interpreter.
- Existing databases from before the privacy change keep an orphaned `profiles` table and `users.display_name` column; drop them (the app no longer reads or writes either).
- Common Crawl coverage is partial and its index server is flaky; re-run `inventory` to resume.
- Workday is capped at 400 postings per board; several vendors' list endpoints carry no description,
  so `description_exclude` can't act on those jobs.

## Job order

The jobs page, the public API and search list the **newest first by publish date**. A posting with no publish date
from its company sorts by when livejobs first saw it (shown as "Added …" instead of "Posted …"), and a date in the
future is treated as now, so it cannot pin a job to the top. This uses a stored, indexed column (`jobs.sort_at`):
sorting 300,000 jobs by an expression took about 30 seconds, by the indexed column it takes a millisecond.
`livejobs init-db` adds and fills it for existing jobs. Keyword searches keep ranking title matches above
description-only matches (newest within each) unless you pass `sort=newest` (REST `/jobs/search`, MCP `search_jobs`).

## Light and dark theme

The site is **light by default**, whatever a visitor's operating system is set to. Every page header has a sun/moon
button that switches to the dark theme (and back) for any visitor, signed in or not. The choice is kept in that
browser only (`localStorage`). The palettes are the `:root` and `:root[data-theme="dark"]` blocks at the top of
`livejobs/web/css/site.css`. `js/theme.js` is a plain blocking script in each page's `<head>` (the CSP forbids inline
scripts) so a saved choice is applied before the first paint and the page never flashes the wrong theme.

## Speed and caching

The jobs page needs three things from MySQL: a count, the ids of the page, and those 25 rows.

- **Indexes.** Every filter column (workplace, seniority, country) has an index that also holds `board_id`, and
  `ix_jobs_browse` holds the sort column plus all of them. The count and the walk through the newest postings are
  answered from the indexes alone, without reading a posting's row (descriptions can be hundreds of KB). `init-db`
  adds them to an existing database (about a minute on 300,000 postings; run it when no sync is running).
- **No wide rows in a list.** The page's rows are loaded without their descriptions, together with their board, in
  one query. Write conditions as `column = 1`, not `column IS true`: MySQL does not use an index for `IS true`, and
  that single difference was a 7-second full scan. `tests/test_list_queries.py` pins this.
- **Server cache.** The public list (`/api/public/jobs`) is the same for every visitor, so one answer is kept in
  memory for `LIVEJOBS_CACHE_SECONDS` (default 30; 0 turns it off). Identical requests that arrive together wait for
  one query instead of each running it. Nothing is written to disk and nothing needs clearing, since jobs change
  hourly.
- **Browser and proxy cache.** The same endpoint sends `Cache-Control: public, max-age=30, stale-while-revalidate=60`
  and a single posting `public, max-age=60`, so browsers, Apache's `mod_cache` or a CDN may reuse them. The signed-in
  API is never marked public. Scripts and styles revalidate with an ETag on every load.
- **Compression.** Pages, scripts, styles and `/api/public/*` are gzip-compressed (a posting with embedded pictures
  is hundreds of KB). The signed-in API and `/mcp` are not.
- **Give MySQL memory.** MySQL's default buffer pool is 128 MB. When the table is larger than the pool, every
  uncached read goes to disk. Set `innodb_buffer_pool_size` to about half the database machine's memory and give the
  database container (or VM) at least 2 GB; with Ansible it is `mysql_innodb_buffer_pool_size`.

## Memory

Syncing holds every posting of a board in memory while it is stored, and the biggest boards are tens of megabytes of
text. Memory use therefore scales with how many boards are in flight at once: `LIVEJOBS_SYNC_WORKERS` (default 16)
for scheduled syncs and Sync all, and `LIVEJOBS_IMPORT_WORKERS` (default 6) for a long import. Postings are written to
the database in slices of 400 and never loaded twice. Measured with synthetic 15 MB boards, 16 at once peaked near
380 MB and 6 at once near 190 MB. If a container is killed for memory (exit code 137, `Memory cgroup out of memory` in
`container logs --boot <name>`), lower the worker counts or give the container more memory. The image also sets
`MALLOC_ARENA_MAX=2`, which stops glibc from growing a memory pool per thread.

## Being polite to Common Crawl

Its index server answers 503/504 and refuses connections when it is asked too much. All refresh threads share one gate:
requests are at least `LIVEJOBS_CC_MIN_INTERVAL` seconds apart (default 1), and when the server struggles every thread
pauses together, 10s then 20s, 40s... up to 3 minutes (or the server's own `Retry-After`), with
`LIVEJOBS_CC_RETRIES` (default 8) retries per request. A page that still fails is counted, the run ends as *partial*,
and the next refresh resumes from the pages already ingested.

Common Crawl and the vendor sync use **separate thread pools** (`LIVEJOBS_CC_WORKERS`, default 3, threads named `crawl_N`,
and `LIVEJOBS_SYNC_WORKERS`, default 16, threads named `sync_N`), separate HTTP connection pools and separate pacing, so a
throttled index never slows job syncing and a big sync never starves the refresh.

## Logging

All processes (web, scheduler, worker, CLI) log to **stdout**, which is what the container runtime collects:
`docker compose logs -f app scheduler` (or `container logs -f <name>`), and `journalctl -u livejobs-web -f` under systemd.

| Setting | Meaning |
|---|---|
| `LIVEJOBS_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR`. The CLI also takes `livejobs --log-level DEBUG <command>` |
| `LIVEJOBS_LOG_FORMAT` | `text` (default) or `json`, one object per line, for a log shipper |
| `LIVEJOBS_LOG_LEVELS` | per-logger overrides: `livejobs.sync=DEBUG,livejobs.http=DEBUG,sqlalchemy.engine=INFO` |

Loggers: `livejobs.web` (one line per request with status and time, plus an `X-Request-ID` that tags every line written
while handling it), `livejobs.api` (logins, signups, admin actions), `livejobs.mcp` (each tool call), `livejobs.sync`,
`livejobs.inventory`, `livejobs.http` (outgoing requests and retries), `livejobs.scheduler`, `livejobs.mail`,
`livejobs.db`, `livejobs.ratelimit`. At `INFO` you get requests, admin actions, run summaries and failures; `DEBUG` adds
every board's sync, every Common Crawl page, every outgoing HTTP call and every MCP request. Health checks, `/metrics`
and static files are logged at `DEBUG` only.

Passwords, tokens, query strings and email bodies are never logged, and users appear by id rather than address.
(With `LIVEJOBS_MAIL_BACKEND=console`, a development setting, the email itself is printed so you can click its link.)

## Code style

All Python follows PEP 8 (79 columns), enforced by [ruff](https://docs.astral.sh/ruff/) with the settings in
`pyproject.toml`. A GitHub Action (`.github/workflows/pep8.yml`) runs on every pull request and fails it if the code
is not PEP 8 clean or not formatted.

```
pip install -e '.[dev]'
ruff format .            # fix formatting
ruff check --fix .       # fix what can be fixed automatically, report the rest
ruff check . && ruff format --check .   # what the pull request check runs
```
