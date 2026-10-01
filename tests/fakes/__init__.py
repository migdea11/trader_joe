"""Test doubles that run INSIDE the production data_ingest image (decision tj-j4wknb R4).

The fake-mode compose overlay mounts this directory read-only into data_ingest and swaps the
service command to tests.fakes.ingest_launcher:app. That is the only way a fake reaches a running
service: production code holds no fake, no mode flag and no swap logic.

IMPORT DISCIPLINE, because this package runs in the prod image: every module here imports only
the standard library, first-party code that image contains and data_ingest's production
dependencies. Never pytest, never a testing-group package, never a mock library.
data/ingest/tests/test_fake_read.py enforces it with an AST scan over every module here.

Nothing here carries a test_ prefix, so pytest never collects this package as tests.
"""
