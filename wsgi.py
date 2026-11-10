"""gunicorn entry point: `gunicorn wsgi:app --workers 1 --threads 16 --timeout 120`.

Building checks every app's settings; a mistake raises ConfigError here, the service doesn't
start, and Render keeps the previous version running. Building imports no app and starts no
thread (apps load on their first request; see router/dispatcher.py).
"""

from router.root import build

app = build()
