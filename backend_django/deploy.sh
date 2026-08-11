#!/usr/bin/env bash
#
# Deploy the Brave POS backend.  Run ON the server:
#
#     ssh bravepos 'bash /home/azureuser/silom-pos/backend_django/deploy.sh'
#
# Replaces the old by-hand "git pull, then restart whenever" routine, which on
# 2026-08-11 left production serving 500s for hours: a request arrived while
# the pull was still writing files, a gunicorn worker imported the new
# bravepos/peak.py against the old bravepos/models.py, and Python cached the
# broken modules for the life of that process.
#
# The ordering below is the point:
#   1. pull
#   2. migrate
#   3. verify the new tree actually imports -- in a throwaway process, while
#      the old workers keep serving
#   4. only then restart
#   5. smoke-test, and roll back if the new code cannot serve a request
#
# Step 3 is what the old routine lacked. A tree that cannot import is caught
# before it can reach a worker, so a bad commit is a 5-second no-op instead of
# an outage nobody notices until the tablets start failing.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$APP_DIR/.." && pwd)"
SERVICE="bravepos.service"
VENV_PY="/home/azureuser/.local/share/virtualenvs/backend_django-43C1HBFU/bin/python"
HEALTH_URL="http://127.0.0.1:8000/api/branches"
BRANCH="main"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

cd "$REPO_DIR"

[ -x "$VENV_PY" ] || die "virtualenv python not found at $VENV_PY"

# A dirty tree means someone edited files on the server. Pulling over that
# either fails or silently clobbers their work -- stop and let a human decide.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short
    die "working tree has uncommitted changes -- refusing to deploy over them"
fi

PREVIOUS="$(git rev-parse HEAD)"
say "current: $PREVIOUS"

say "fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"

TARGET="$(git rev-parse "origin/$BRANCH")"
if [ "$PREVIOUS" = "$TARGET" ]; then
    say "already at origin/$BRANCH -- nothing to pull"
else
    git log --oneline "$PREVIOUS..$TARGET"
    say "checking out $TARGET"
    git checkout --quiet "$BRANCH"
    git merge --quiet --ff-only "origin/$BRANCH"
fi

cd "$APP_DIR"

# Stamp every Sentry event with the commit that produced it, so "is this fixed
# yet?" is answerable from the issue page rather than by guesswork.
export SENTRY_RELEASE="$TARGET"

say "applying migrations"
"$VENV_PY" manage.py migrate --noinput

# The guard rail. `check` imports settings, every app, and -- because wsgi.py
# now touches get_resolver().url_patterns -- the entire URLconf and view layer.
# Anything that would have poisoned a worker raises here instead, in a process
# that is not serving traffic.
say "verifying the new tree imports cleanly"
"$VENV_PY" manage.py check --deploy --fail-level ERROR \
    || die "django check failed -- NOT restarting, old code still serving"

"$VENV_PY" -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bravepos_api.settings')
django.setup()
from django.urls import get_resolver
get_resolver().url_patterns
print('URLconf imports cleanly')
" || die "URLconf failed to import -- NOT restarting, old code still serving"

say "restarting $SERVICE"
sudo systemctl restart "$SERVICE"

# systemd's unit is Type=notify, so `restart` already waits for gunicorn to
# signal readiness. Poll the health endpoint anyway: readiness only means the
# master booted, not that a request can round-trip to Postgres and back.
say "smoke-testing $HEALTH_URL"
ok=""
for attempt in $(seq 1 10); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" || true)"
    # 401/403 are fine -- the endpoint is auth-scoped, and any of these means
    # Django resolved the URL and ran a view. 5xx or a dead socket is not.
    case "$code" in
        200|401|403) ok="$code"; break ;;
    esac
    printf '  attempt %s/10: HTTP %s\n' "$attempt" "${code:-none}"
    sleep 3
done

if [ -z "$ok" ]; then
    say "smoke test FAILED -- rolling back to $PREVIOUS"
    cd "$REPO_DIR"
    git checkout --quiet --force "$PREVIOUS"
    sudo systemctl restart "$SERVICE"
    die "deploy rolled back; investigate before retrying"
fi

say "deployed $TARGET (health check HTTP $ok)"
systemctl status "$SERVICE" --no-pager --lines 5 || true
