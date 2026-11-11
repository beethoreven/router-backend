"""Code every app may use: security guards and plumbing that have nothing to do with any one API.

    shared.google_auth     verify a Google ID token (shape checks, cached keys, 10 s fetch cap)
    shared.app_pass        our own stateless sign-in pass (what a phone or page keeps after sign-in)
    shared.admin_auth      admin-console sessions, the admin list, and the require_admin gate
    shared.database        one Postgres connection per call, per-app and process-wide caps, schema once
    shared.payload         request size limits and gzip-bomb-safe decompression
    shared.quota           per-person daily counters kept in memory (Taipei day)
    shared.rejections      count rejected requests, one summary line a minute
    shared.snapshot_cache  an in-memory copy of public data, reloaded from the database
    shared.flask_app       a Flask app with JSON errors, size limit and CORS set up the same way
    shared.timeutil        now / Taipei day / release stamps
    shared.textutil        characters as people count them

Rules (tests/test_isolation.py checks the first three):
1. Nothing here imports an app (`apps.*`) or reads os.environ. Values an app chooses (its secret, its
   database, its purpose strings) are passed in, usually as a function so they are read after
   create_app() has set the app's config.
2. No per-app state at module level. Counters, caches and locks live in objects that each app creates
   for itself, so one app's cache can never answer for another. The only process-wide state is
   listed in PROCESS_WIDE (tests/test_isolation.py) with the reason next to it.
3. Something moves here only when two apps need the same logic. Code that only one app uses stays in
   that app, even if it is "security" (for example LINE's webhook signature lives in its app until a
   second LINE app joins).
4. Changing anything here changes every app at once. The router-backend session owns this package:
   other sessions ask it for changes instead of editing here (see CLAUDE.md).
"""
