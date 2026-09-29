# Deploying the Strat360 Municipal EMS (Django)

This app shows **real voter data** (names, addresses, precincts from `cvl_national`) and records
EMS changes in `generic_360_db`. Treat it as personal data under the Data Privacy Act (RA 10173):
HTTPS only, named staff accounts only, least-privilege database access, and backups.

## 1. Where to host

Hostinger **shared** hosting (where the PHP apps run) cannot run Python web apps. Options:

| Option | Why | Watch out for |
|---|---|---|
| **AWS Lightsail / EC2 in `ap-southeast-1` (Singapore)** — *recommended* | Same region as the RDS database: ~1 ms queries instead of the 0.3–8 s connects seen from a home connection. The RDS security group can then be closed to the public internet and opened only to this server. | You manage the Linux box (updates). Lightsail from ~US$7/mo. |
| Hostinger **VPS** (KVM) | Same provider/billing as the PHP apps. | Pick the Singapore location if offered; RDS must stay publicly reachable (lock it to the VPS IP). |
| PaaS (Render, Railway, Fly.io — Singapore region) | No server to maintain; git-push deploys. | The login database is SQLite: give it a **persistent disk**, or it is wiped on every deploy. RDS must accept the platform's outbound IPs. |

Whichever you pick, the steps below are the same apart from section 4.

## 2. Database accounts (least privilege)

The app currently connects as the RDS `admin` superuser. Create two dedicated accounts instead
(run once as `admin`; replace the passwords and the host `%` with the app server's IP if you can):

```sql
-- Voter roll: read-only. Also reads the EMS tables it filters on.
CREATE USER 'strat360_ro'@'%' IDENTIFIED BY 'long-random-password-1';
GRANT SELECT ON cvl_national.* TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_smart_cards      TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_voter_details    TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_voter_sectors    TO 'strat360_ro'@'%';
GRANT SELECT ON generic_360_db.muni_household_members TO 'strat360_ro'@'%';

-- EMS changes: read/write on exactly the tables the app uses (no DROP/ALTER/GRANT).
CREATE USER 'strat360_rw'@'%' IDENTIFIED BY 'long-random-password-2';
GRANT SELECT ON cvl_national.* TO 'strat360_rw'@'%';                       -- joins to the roll
GRANT SELECT ON generic_360_db.ems_political_role TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.ems_voter_political     TO 'strat360_rw'@'%';
GRANT SELECT, INSERT                 ON generic_360_db.ems_voter_audit         TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_social_services    TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_smart_cards        TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_voter_details      TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_voter_sectors      TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_household_members  TO 'strat360_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON generic_360_db.muni_barangay_geo       TO 'strat360_rw'@'%';
-- (No grant on information_schema is needed: an account automatically sees the tables it has
--  rights to, which is all the app's table_exists() checks look for.)
```

Put them in `.env` as `RDS_USER`/`RDS_PASSWORD` (read-only) and `EXT_DB_USER`/`EXT_DB_PASSWORD`
(read/write). The setup and mock commands (`*_setup`, `seed_*`, `clear_*`) create tables and bulk
delete, so run those with the admin account from your own machine, not the server's.

Then **rotate the `admin` password** — it is also hard-coded as a fallback in the PHP apps
(`STRAT360-EMS/includes/connect.php`), see the security notes.

## 3. Configuration

Copy `.env.example` to `.env` on the server and fill it in. Minimum for production:

```
DJANGO_SECRET_KEY=<new random value — don't reuse the development one>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=ems.yourdomain.gov.ph
DJANGO_CSRF_TRUSTED_ORIGINS=https://ems.yourdomain.gov.ph
DJANGO_BEHIND_PROXY=True
DJANGO_ADMIN_URL=<something-not-obvious>/
RDS_HOST=… RDS_USER=strat360_ro RDS_PASSWORD=…
EXT_DB_USER=strat360_rw EXT_DB_PASSWORD=…
GEMINI_API_KEY=… (optional)
```

`DJANGO_DEBUG=False` switches on HTTPS-only cookies, the HTTPS redirect, HSTS (1 hour to start),
`X-Frame-Options: DENY` and no debug pages. After a week of working HTTPS, raise
`DJANGO_HSTS_SECONDS` to `31536000`. Only set `DJANGO_HSTS_INCLUDE_SUBDOMAINS=True` if **every**
subdomain is HTTPS — HSTS can't be taken back quickly once browsers have seen it.

## 4. Install (Linux VM: Lightsail / EC2 / VPS, Ubuntu 24.04)

```bash
sudo apt update && sudo apt install -y python3-venv python3-dev build-essential \
     default-libmysqlclient-dev pkg-config nginx certbot python3-certbot-nginx
sudo useradd --system --create-home --home-dir /srv/strat360 strat360
sudo -u strat360 git clone <repo-or-copy-the-folder> /srv/strat360/app
cd /srv/strat360/app
sudo -u strat360 python3 -m venv venv
sudo -u strat360 venv/bin/pip install -r requirements.txt
sudo -u strat360 cp .env.example .env && sudo -u strat360 nano .env     # fill in, chmod 600
sudo -u strat360 venv/bin/python manage.py migrate
sudo -u strat360 venv/bin/python manage.py collectstatic --noinput
sudo -u strat360 venv/bin/python manage.py check --deploy
sudo -u strat360 venv/bin/python manage.py createsuperuser
```

`/etc/systemd/system/strat360.service`:

```ini
[Unit]
Description=Strat360 Municipal EMS
After=network.target

[Service]
User=strat360
WorkingDirectory=/srv/strat360/app
ExecStart=/srv/strat360/app/venv/bin/gunicorn strat360.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 90
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/nginx/sites-available/strat360` (then `ln -s` into `sites-enabled`):

```nginx
server {
    server_name ems.yourdomain.gov.ph;
    client_max_body_size 5m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 90s;     # AI Analytics answers take ~5–10 s
    }
}
```

```bash
sudo systemctl enable --now strat360
sudo certbot --nginx -d ems.yourdomain.gov.ph     # free HTTPS certificate + auto-renewal
curl -s https://ems.yourdomain.gov.ph/healthz      # {"ok": true}
```

Static files are served by the app itself (WhiteNoise), so nginx needs no `/static/` block.
On Windows, use `venv\Scripts\python.exe -m waitress --listen=127.0.0.1:8000 strat360.wsgi:application`
instead of gunicorn.

## 5. After going live

- **Accounts**: one named account per staff member (Django admin → Users); nobody shares `admin`.
  Staff accounts only for people who manage users. Five wrong passwords lock a username (and 20
  from one IP) for 15 minutes — see `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES`.
- **Backups**: `strat360.db` (logins) — copy it daily; the EMS data lives in RDS, which has its own
  automated backups (check the retention period in the RDS console).
- **Mock data**: remove the Bustos demo data before real use:
  `clear_demo_mock`, `clear_card_mock`, `clear_social_mock` (see README).
- **Logs**: `journalctl -u strat360` shows failed sign-ins, lockouts and errors.
- **Updates**: `git pull && pip install -r requirements.txt && manage.py migrate && manage.py collectstatic --noinput && sudo systemctl restart strat360`.
- **Map tiles**: the maps use OpenStreetMap's free tile server, which is fine for light use. For
  heavy public use, switch to a tile provider with an API key (OSM's usage policy).
