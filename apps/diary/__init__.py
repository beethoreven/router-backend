"""全心日記 (all-mo-diary) backend, mounted at /diary.

Settings come from DIARY__* environment variables via the AppSettings passed to create_app.
Imports inside this package are relative (`from . import x`) or `apps.diary.x`, never a bare
`db` / `config`: every project has packages with those names, and in one process the second
import would silently get the first project's module (tests/test_isolation.py checks this).
"""
