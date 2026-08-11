"""Gunicorn config for the Brave POS backend.

The important setting here is ``preload_app``.  See ``bravepos_api/wsgi.py``
for the full story: without it, Django imports the view layer lazily inside
whichever worker serves the first request, so a request arriving mid-deploy
can import a half-written tree and poison that worker's ``sys.modules`` for
the rest of its life.  Preloading moves that import into the master, before
any worker forks — a tree that cannot import now fails at boot, loudly, while
systemd retries, instead of quietly serving 500s for hours.
"""
import os


bind = "127.0.0.1:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))
timeout = 120
accesslog = "-"
errorlog = "-"

# Import the app (and, via wsgi.py, the whole URLconf) in the master process
# before forking. Requires a restart to pick up new code — which is exactly
# what deploy.sh does.
preload_app = True


def post_fork(server, worker):
    """Drop any inherited DB connection.

    ``preload_app`` forks workers from a master that has already run
    ``django.setup()``.  Anything that opened a socket before the fork would
    have that same socket shared across every worker — two processes reading
    one Postgres connection corrupts both.  Django reopens lazily on first
    use, so closing here costs nothing and removes the whole class of bug.
    """
    from django.db import connections

    for conn in connections.all():
        conn.close()
