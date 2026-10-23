"""The test-mode Postgres carries its own marker: cluster_name = 'test', set on the server command line (tj-grna9p.198).

WHY THIS IS PINNED. tools/dev_datasets' seed rail refuses to reset or degrade a database unless that database says,
on its own connection, that it is dev or test: it reads cluster_name and its source from pg_settings and accepts
only 'dev' or 'test' whose source is 'command line' (tj-grna9p.194). The source is what makes it a proof: a value
that arrived any other way (a client option such as PGOPTIONS, a configuration file) is not the marker, and a prod
database never has it from the command line. docker-compose.override.yaml marks the dev postgres, and that was verified live (tj-grna9p.180, step C).
docker-compose.fake.yaml marks the test-mode postgres with `command: ["postgres", "-c", "cluster_name=test"]`, but
the agent container cannot reach the test stack's postgres, so nothing proved it until this test. It runs where the
system tier runs (make test-system, the agent-stack MCP's run_system_tests, and CI's system job), all of them on
the fake-mode compose set, so a dropped or reworded `command:` reds here rather than leaving the rail refusing, or
worse, the rail quietly re-pinned to something weaker.

EXACTLY ONE ROW, BOTH COLUMNS, ONE TUPLE. pg_settings holds one row per name, so the row count is a check that the
query names a real setting; the tuple comparison means neither the value nor its source can pass alone. The
expected tuple is literal on purpose: it is the marker the compose file and the rail agree on, not a value read
back from either. Read through the suite's ordinary pg_conn, the test client's own database access, never a
connection opened here.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection


pytestmark = pytest.mark.data_store

MARKER_QUERY = sa.text("SELECT setting, source FROM pg_settings WHERE name = 'cluster_name'")
# The test-mode marker: the value docker-compose.fake.yaml passes, and the source Postgres reports for `-c`.
EXPECTED_MARKER = ('test', 'command line')


def test_the_test_mode_postgres_reports_cluster_name_test_from_the_command_line(pg_conn: Connection):
    """The database the system suite runs against says 'test', and says so from the server command line."""
    rows = [tuple(row) for row in pg_conn.execute(MARKER_QUERY).all()]
    assert rows == [EXPECTED_MARKER], (
        f'pg_settings cluster_name on the test-mode postgres is {rows!r}, expected exactly [{EXPECTED_MARKER!r}]. '
        'docker-compose.fake.yaml sets -c cluster_name=test on postgres, and the tools/dev_datasets seed rail '
        'refuses a database that does not report it from the command line (tj-grna9p.194)'
    )
