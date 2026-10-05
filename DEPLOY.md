# Deploying Strat360 EMS (Django) on the shared EC2 server

This app shows **real voter data** (names, addresses, precincts from `cvl_national`) and records
EMS changes in `generic_360_db`. Treat it as personal data under the Data Privacy Act (RA 10173):
HTTPS only, named staff accounts only, least-privilege database access, and backups.

It runs on the team's EC2 server next to the other Django apps (Nginx in front, each app under its
own path, e.g. `https://<host>/marilao/fms/`). This app goes under **`/strat360/`**.

## 0. How this differs from a basic EC2 tutorial — and why

| Here | Why |
|---|---|
| `urls.py` stays in git; the path prefix comes from `.env` (`DJANGO_URL_PREFIX=strat360/`) | A hand-kept server `urls.py` silently misses new routes (e.g. the Barangay level would 404). One file, the same everywhere. |
| Own Linux user `strat360`, own venv, own Gunicorn service on a **unix socket** | Isolated from the other apps; no open app port; a fault here can't touch them. |
| systemd hardening (read-only system, writes only to its own folder) | Limits what a compromised process could do. |
| Own cookie names (`strat360_sessionid`, `strat360_csrftoken`) scoped to `/strat360/` | Apps on one host with Django's default `sessionid` overwrite each other's sign-ins. |
| Secrets only in `.env` on the server (`chmod 600`), never in git | The repo has no secrets and never had any. |
| Read-only GitHub **deploy key** | No personal password or token on the server. |
| Two least-privilege database accounts | The web app can't drop or alter tables, or write to the voter roll. |
| `deploy/update.sh` (fast-forward only, checks before restart, health check after) | Repeatable updates; refuses to run if the server copy was edited by hand. |
| `DEBUG=False`, HTTPS-only cookies, HSTS | No debug pages or toolbar in production. |

## 1. Check with the server owner first

1. The EC2 instance is in **ap-southeast-1 (Singapore)**, the RDS region (≈1 ms queries).
2. The **RDS security group** allows MySQL (3306) only from the EC2 instance's security group.
3. SSH is limited to known IPs (or SSM Session Manager is used and port 22 is closed).
4. The site's Nginx `server { … }` already has HTTPS (certificate) for the domain.
5. Nginx runs as `www-data` (Ubuntu) — on Amazon Linux it is `nginx`; change `Group=` in `deploy/strat360.service`.

## 2. Database accounts (once, as the RDS admin, from your own machine)

Replace the passwords, and `%` with the EC2 private IP or subnet (e.g. `'10.0.1.%'`) if possible.

```sql
-- Voter roll, read-only. Also reads the EMS tables the Voters List filters on.
CREATE USER 'strat360_ro'@'%' IDENTIFIED BY 'long-random-password-1';
GRANT SELECT ON cvl_national.* TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_smart_cards       TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_voter_details     TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_voter_sectors     TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_household_members TO 'strat360_ro'@'%';

-- EMS changes: exactly the tables the app uses. No CREATE / DROP / ALTER / GRANT.
CREATE USER 'strat360_rw'@'%' IDENTIFIED BY 'long-random-password-2';
GRANT SELECT ON cvl_national.* TO 'strat360_rw'@'%';                         -- joins to the roll
GRANT SELECT                         ON generic_360_db.ems_political_role     TO 'strat360_rw'@'%';
GRANT SELECT, INSERT                 ON generic_360_db.ems_voter_audit        TO 'strat360_rw'@'%';
-- political machinery, one table per EMS level
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.brgy_voter_political   TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_voter_political   TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.prov_voter_political   TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.ems_voter_political    TO 'strat360_rw'@'%';
-- shared EMS tables
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_social_services   TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_smart_cards       TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_voter_details     TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_voter_sectors     TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_household_members TO 'strat360_rw'@'%';
-- pre-computed tables (staff "Prepare province" / "Locate" buttons refresh them)
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_roll_summary      TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_roll_precincts    TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_barangay_geo      TO 'strat360_rw'@'%';
```

All these tables already exist. Commands that **create** tables or bulk-delete (`*_setup`,
`seed_*`, `clear_*`, `build_roll_summary --all`) are run with the admin account from your own
machine, never by the server. Then **rotate the RDS `admin` password** (it is also a fallback in the
PHP apps, see the security notes).

## 3. First install on the server

```bash
# 3.1 Packages (Ubuntu 24.04) — skip what the server already has
sudo apt update && sudo apt install -y python3-venv python3-dev build-essential default-libmysqlclient-dev pkg-config git sqlite3

# 3.2 The app's own user and folder
sudo useradd --system --create-home --home-dir /srv/strat360 --shell /bin/bash strat360
sudo chmod 750 /srv/strat360
sudo -iu strat360

# 3.3 Read-only deploy key → add the .pub in GitHub: repo → Settings → Deploy keys (no write access)
ssh-keygen -t ed25519 -f ~/.ssh/strat360_deploy -N ""
cat ~/.ssh/strat360_deploy.pub
printf 'Host github.com\n  IdentityFile ~/.ssh/strat360_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone git@github.com:arjaygagui/pydjango-project-strat360-ems.git ~/app
cd ~/app

# 3.4 Python environment
python3 -m venv venv
venv/bin/pip install --upgrade pip && venv/bin/pip install -r requirements.txt

# 3.5 Secrets (see below), then the local login database and static files
cp .env.example .env && chmod 600 .env && nano .env
venv/bin/python manage.py migrate
venv/bin/python manage.py collectstatic --noinput
venv/bin/python manage.py check --deploy
venv/bin/python manage.py createsuperuser
exit                                           # back to your own account
```

`.env` on the server (the rest of `.env.example` keeps its defaults):

```
DJANGO_SECRET_KEY=<new: python3 -c "import secrets; print(secrets.token_urlsafe(50))">
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=<the site's host name>
DJANGO_CSRF_TRUSTED_ORIGINS=https://<the site's host name>
DJANGO_URL_PREFIX=strat360/
DJANGO_BEHIND_PROXY=True
DJANGO_ADMIN_URL=<something-not-obvious>/
RDS_HOST=<rds endpoint>   RDS_USER=strat360_ro   RDS_PASSWORD=…
EXT_DB_USER=strat360_rw   EXT_DB_PASSWORD=…
GEMINI_API_KEY=…          (optional)
```

Never copy your local `.env` or `strat360.db` to the server.

```bash
# 3.6 Service (Gunicorn on /run/strat360/gunicorn.sock, hardened — see deploy/strat360.service)
sudo cp /srv/strat360/app/deploy/strat360.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now strat360
sudo systemctl status strat360 --no-pager

# 3.7 Let the strat360 user restart only its own service (used by deploy/update.sh)
echo 'strat360 ALL=(root) NOPASSWD: /usr/bin/systemctl restart strat360' | sudo tee /etc/sudoers.d/strat360
sudo chmod 440 /etc/sudoers.d/strat360 && sudo visudo -c

# 3.8 Nginx: paste deploy/nginx-strat360.conf INSIDE the site's existing HTTPS server { … }
sudo nano /etc/nginx/sites-available/<the site>
sudo nginx -t && sudo systemctl reload nginx

# 3.9 Check
curl -s https://<host>/strat360/healthz        # {"ok": true}
```

Static files are served by the app itself (WhiteNoise, pre-compressed) under `/strat360/static/`,
so Nginx needs no static block and no access to the app folder.

## 4. Updating

```bash
sudo -iu strat360
cd ~/app && ./deploy/update.sh
```

It fetches `main` (fast-forward only — it stops if someone edited files on the server), installs
requirements, migrates the login database, collects static files, runs `check --deploy`, restarts
the service and calls the health check. Nothing is restarted if an earlier step fails.

## 5. After going live

- **Accounts**: one named account per staff member (Django admin → Users); nobody shares `admin`.
  Five wrong passwords lock a username (and 20 one IP) for 15 minutes.
- **Backups**: the EMS data is in RDS (check its automated backup retention). Back up the login
  database daily, e.g. a cron entry for the strat360 user:
  `mkdir -p /srv/strat360/backups` once, then `sqlite3 /srv/strat360/app/strat360.db ".backup '/srv/strat360/backups/strat360-$(date +\%F).db'"`.
- **Mock data**: remove the demo data (Bustos and Pulilan, Bulacan) before real use:
  `clear_demo_mock`, `clear_card_mock`, `clear_social_mock` — see README.
- **Logs**: `journalctl -u strat360` (failed sign-ins, lockouts, errors).
- **HSTS**: after a week of working HTTPS, raise `DJANGO_HSTS_SECONDS` to `31536000`. Only set
  `DJANGO_HSTS_INCLUDE_SUBDOMAINS=True` if every subdomain is HTTPS.
- **Map tiles**: the maps use OpenStreetMap's free tiles, fine for staff use; for heavy public use,
  switch to a tile provider with an API key.

## 6. Performance notes

- Gunicorn `gthread`, 2 workers × 4 threads: the app mostly waits on RDS, so threads cost far less
  memory than processes on a shared instance. Raise `--workers` on a larger instance.
- Database connections are reused (`CONN_MAX_AGE = 60`); the file cache (`cache/`) is shared by all
  workers and survives restarts — big aggregates (province / national rolls, barangay rolls) are
  computed once and reused for hours.
- Province and national pages read the pre-computed `muni_roll_summary` / `muni_roll_precincts`
  tables (already built for all 84 provinces) — never a live scan of the 67.8M-row roll.
