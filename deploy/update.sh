#!/usr/bin/env bash
# Deploy the latest main to this server. Run as the strat360 user from /srv/strat360/app:
#   ./deploy/update.sh
# Stops at the first failing step, before the running app is restarted.
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch --quiet origin
git merge --ff-only origin/main                    # never rewrites local history; fails if the server copy was edited
venv/bin/pip install --quiet -r requirements.txt
venv/bin/python manage.py migrate --noinput        # local login database only
venv/bin/python manage.py collectstatic --noinput --verbosity 0
venv/bin/python manage.py check --deploy --fail-level ERROR
sudo /usr/bin/systemctl restart strat360        # allowed by /etc/sudoers.d/strat360 (DEPLOY.md)

# Health check straight to the app (as Nginx would call it: real host name, over HTTPS).
prefix=$(grep -E '^DJANGO_URL_PREFIX=' .env | cut -d= -f2 | tr -d '/ ' || true)
host=$(grep -E '^DJANGO_ALLOWED_HOSTS=' .env | cut -d= -f2 | cut -d, -f1 | tr -d ' ')
sleep 2
curl -fsS --unix-socket /run/strat360/gunicorn.sock -H "Host: ${host}" -H "X-Forwarded-Proto: https"      "http://${host}/${prefix:+$prefix/}healthz" && echo "  <- strat360 is up"
