"""The dev seed: the trimmed, non-reproducible dev state, exported outside git and loaded back (tj-7zjov1.294, .296).

Decision tj-7zjov1.291 (D1 to D7). The owner's rulings on tj-7zjov1.292 (O1 to O11) apply, with corporate_action kept at
DOUBLE the cutoff.

THE EXPORT (tj-7zjov1.294). In ONE REPEATABLE READ, READ ONLY transaction (so the files are one consistent snapshot and the database
cannot change, whatever this module does), it writes for each of the eighteen reset tables one COPY text file holding the
rows the trim rules keep, and a manifest.json. It writes no table, takes no lock that blocks a writer and never commits.

THE CUTOFF (O11). One constant, CUTOFF_DAYS (90) before the export instant, UTC. Bars and dataset entries use the first
New York midnight at or after it (SESSION_CUTOFF): a bar belongs to the session whose local date its timestamp falls on
(trading_calendar), so a clip on a New York midnight never leaves a part-session. corporate_action uses
CORPORATE_ACTION_DAYS, twice the cutoff, derived and not a second literal (owner ruling 2026-11-03): a transient action
that affected data at the start of the window must not be lost.

THE TRIM RULES, one written-out SELECT per table (no table name is assembled), in SEED_TABLES order:

  store_dataset_entry   (O4) keep an entry that is open-ended or ends after SESSION_CUTOFF, and has no expiry or one in
                        the future; its start is clipped up to SESSION_CUTOFF when earlier.
  stock_market_activity (O4) bars of a kept entry with timestamp >= SESSION_CUTOFF.
  ingest_request        (O1, tj-7zjov1.333) created in the last CUTOFF_DAYS AND in a TERMINAL state (SUCCEEDED, EMPTY or
                        FAILED, a positive list: a state added later is left out until someone decides otherwise). A
                        request still open (PENDING, QUEUED, NOT_DELIVERED) is neither exported nor reloaded: in dev
                        it is an error being fixed or an edge case being tested, and the live ingest re-requests.
  ingest_run            (O1) started in the last CUTOFF_DAYS, plus per kept entry its latest accepted run (SUCCEEDED or
                        EMPTY, with a vendor_asset_id and a granularity: the base of the P50 stop rule, chosen exactly as
                        last_accepted_vendor_asset_id chooses it); then every row whose request_id names an OPEN
                        request is DROPPED, the latest-accepted closure included. Dropped, not nulled: an attempt row
                        with no request_id is a shape the store never writes (attempt rows always name their request;
                        legacy rows have neither).
  ingest_instance, dataset_read, dataset_read_daily, dataset_override_log, dataset_deletion
                        (O1, O3) none: the live ingest re-registers, the rest are usage and audit history.
  asset_list_snapshot   (O6) as_of in the last CUTOFF_DAYS, plus per (source, asset_list) the newest NON-EMPTY one
                        before it (the diff base, chosen as the store's newest_snapshot chooses it: row_count > 0,
                        ordered as_of, received_at, id; asset_type is not part of the key).
  asset_list_row, asset_list_event
                        (O6) those of a kept snapshot.
  corporate_action      (O2, ruling) as_of in the last CORPORATE_ACTION_DAYS.
  panel, panel_settings_version, panel_source_settings_version
                        every row: operator-defined, tiny, and a kept pass needs the version it ran under. (A judgement
                        the design does not state; the foreign keys need these rows whole.)
  panel_member_transition
                        (O5) as_of in the last CUTOFF_DAYS or in a pass that started in it, plus, for each member
                        before the cutoff, judged as the store judges it (members_at and latest_state in
                        server/data/store/app/database/crud/panel_ledger.py: NON-DRY-RUN passes only, ordered as_of,
                        pass started_at, pass_id, seq; no recorded_at): when the newest ENTERED, EXITED or DRAINED row
                        is ENTERED, both that ENTERED row and the member's newest row of any kind.
                        Then a row whose request_id names an OPEN request is DROPPED (only CREATED rows carry one).
                        Not nulled and not kept: member_action answers NONE for a latest CREATED whose request is
                        missing, and created_requests skips a NULL request_id (panel_manager.py ~385-409), so either
                        strands the member for good with no dataset. Dropped, the member's latest row is its ENTERED
                        or DEFERRED and the next pass creates it again.
  panel_pass            (O5) started in the last CUTOFF_DAYS, plus the pass of every kept transition (foreign-key
                        closure).

THE REFERENCES WITH NO FOREIGN KEY MAY DANGLE (R2): panel_pass.inactive_snapshot_id, asset_list_event.previous_snapshot_id
(of the oldest kept snapshot), ingest_run.dataset_id and request_id, panel_member_transition.dataset_id and request_id.
The closure rules keep a referenced row only where that is cheap; nothing here pulls an old snapshot's rows back in. A
request_id that names a request trimmed by AGE may dangle; one that names an OPEN request never does (the two drops above).

UNCHANGED BY THE TERMINAL RULE: asset_list_snapshot.request_id and corporate_action.request_id are written only by a
SUCCEEDED delivery, in the same transaction that finishes the request, so they never name an open request;
store_dataset_entry has no request or last-run pointer (last_run is read from ingest_run); ingest_request.dataset_id
points at entries, not back. No foreign key references ingest_request, so nothing can break the TRUNCATE-and-reload; the
drops are about the store's readers, not constraints.

THE MANIFEST binds the files to a schema. revision is alembic_version read inside the transaction; it must equal
checkout_head, the head of server/data/store/migrations/versions (read as data.store.seeds.manifest.head_revision reads
it; mirrored here because the tool runs from the repository root, where `data` is not importable, and pinned against it
by a test), or the export refuses (DevSeedRefused, which request.py maps to exit 3 like the seed producer's EXIT_REFUSED;
every other failure exits 1). Each table records its row count, byte count, sha256, and the column fingerprint
(names, types and order from information_schema), which a loader compares before it trusts the file.

THE RELOAD (D1, D3). write_seed is the one writer of the files, called by the export (its READ ONLY transaction) and by
the preserving dev reset (request.run_reset, inside the transaction that holds the lock). load_seed puts a seed back into
EMPTY tables: it refuses (DevSeedRefused, exit 3) unless the seed's revision, the database's alembic_version and the
checkout head are the same and every table's column fingerprint matches, checks the sha256 and size of every file before
anything is written, then COPYs the files in SEED_TABLES order (parents first; every foreign key has both ends loaded
before the transaction ends) and setvals the serial sequence to max(id). The caller recounts against the manifest and
commits. A refusal names the three revisions or the tables, never a value.

WHERE IT GOES. output/dev_seed/<revision>/<UTC stamp>/, which is gitignored and in .dockerignore (D5: the seed holds real
tickers, vendor bars and owner names and never enters git, CI or an image). Directories 0700, files 0600, created
O_EXCL and O_NOFOLLOW, fsynced. The newest KEEP_SEEDS stamps of the revision are kept; older ones are removed only after
the new one is complete, and only directories named like a stamp. A failed export removes its own directory, so a stamp
directory that holds manifest.json is complete and one that does not is not a seed.
"""

import ast
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo


CUTOFF_DAYS: Final = 90
# Twice the cutoff, derived: the owner's ruling is "double the cutoff", so the two can never drift apart.
CORPORATE_ACTION_DAYS: Final = 2 * CUTOFF_DAYS
KEEP_SEEDS: Final = 3
MARKET_TZ: Final = ZoneInfo('America/New_York')

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SEED_ROOT: Final = REPO_ROOT / 'output' / 'dev_seed'
VERSIONS_DIR: Final = REPO_ROOT / 'server' / 'data' / 'store' / 'migrations' / 'versions'

MANIFEST_NAME: Final = 'manifest.json'
MANIFEST_FORMAT: Final = 1
# The stacks a seed can come from and a load can target (the rail's deploymentLabel values).
SEED_STACKS: Final = ('dev', 'test')
FILE_SUFFIX: Final = '.copy'
STAMP_FORMAT: Final = '%Y%m%dT%H%M%SZ'
_STAMP = re.compile(r'^\d{8}T\d{6}Z$')
DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600

# The eighteen tables, parents first (the order a reload needs): the bar after its entry, a panel before its versions and
# passes, a pass before its transitions. The same set as request.RESET_TABLES; a test pins the two against each other.
SEED_TABLES: Final = (
    'store_dataset_entry',
    'stock_market_activity',
    'ingest_run',
    'ingest_request',
    'ingest_instance',
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
    'asset_list_snapshot',
    'asset_list_row',
    'asset_list_event',
    'corporate_action',
    'panel',
    'panel_settings_version',
    'panel_source_settings_version',
    'panel_pass',
    'panel_member_transition',
)

# The transaction. SET TRANSACTION must be the first statement, so the caller ends the rail check's transaction first.
# The session settings are the seed manifest's (data.store.seeds.manifest.SESSION_OPTIONS): the text of a timestamp or a
# float then does not depend on the server's configuration.
READ_ONLY_SQL: Final = 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'
TIMEZONE_SQL: Final = "SET LOCAL TimeZone = 'UTC'"
FLOAT_DIGITS_SQL: Final = 'SET LOCAL extra_float_digits = 1'
REVISION_SQL: Final = 'SELECT version_num FROM alembic_version'
COLUMNS_SQL: Final = (
    'SELECT column_name, data_type, udt_name FROM information_schema.columns '
    "WHERE table_schema = 'public' AND table_name = %(table)s ORDER BY ordinal_position"
)

# The open-ended entry's end is stored as the epoch (common.database.sql_alchemy_nullable_datetime).
_NO_END = "'1970-01-01 00:00:00+00'::timestamptz"

# Fragments the statements below repeat. Constants only: no name is built from input.
# An entry that survives (alias e). The same predicate picks the bars' datasets and the runs' base rows.
_ENTRY_KEPT = f'(e."end" = {_NO_END} OR e."end" > %(session_cutoff)s) AND (e.expiry IS NULL OR e.expiry > %(now)s)'  # nosec B608 -- constants and bind placeholders only
# The terminal request states, a POSITIVE list (server/data/store/app/database/models/ingest_request.py): a state added
# later is left out of the seed until someone decides otherwise.
_TERMINAL_STATES = "('SUCCEEDED', 'EMPTY', 'FAILED')"
# The open states, for the rows that name an open request (alias o, the request): they are dropped, not nulled.
_NAMES_OPEN_REQUEST = (
    'EXISTS (SELECT 1 FROM ingest_request o WHERE o.id = {alias}.request_id '
    "AND o.state IN ('PENDING', 'QUEUED', 'NOT_DELIVERED'))"
)
# A snapshot that survives (alias s): recent, or the newest NON-EMPTY one before the cutoff of its (source, list): the diff
# base, chosen as newest_snapshot chooses it (server/data/store/app/database/crud/asset_list.py, ~101-123): row_count > 0
# (an empty snapshot is never a base), per (source, asset_list) and not per asset type, ordered as_of, received_at, id.
_SNAPSHOT_KEPT = (
    's.as_of >= %(cutoff)s OR s.id IN ('
    'SELECT DISTINCT ON (b.source, b.asset_list) b.id FROM asset_list_snapshot b '
    'WHERE b.as_of < %(cutoff)s AND b.row_count > 0 '
    'ORDER BY b.source, b.asset_list, b.as_of DESC, b.received_at DESC, b.id DESC)'
)

# The panel ledger rows before the cutoff, ranked newest first per (panel, vendor asset) as the store ranks them: a mirror
# of _ranked in server/data/store/app/database/crud/panel_ledger.py (~350-389). NON-DRY-RUN passes only (the join to
# panel_pass), ordered as_of, the pass's started_at, pass_id, seq; recorded_at is not in the order.
_RANKED = (
    'SELECT c.id, c.panel_id, c.vendor_asset_id, c.transition, row_number() OVER ('
    'PARTITION BY c.panel_id, c.vendor_asset_id '
    'ORDER BY c.as_of DESC, q.started_at DESC, c.pass_id DESC, c.seq DESC) AS rnk '
    'FROM panel_member_transition c JOIN panel_pass q ON q.id = c.pass_id '
    'WHERE q.dry_run = false AND c.as_of < %(cutoff)s'
)
# The same ranking over the membership-deciding transitions only, as members_at reads them (panel_ledger.py ~411).
_RANKED_DECIDING = _RANKED + " AND c.transition IN ('ENTERED', 'EXITED', 'DRAINED')"
# The ENTERED row of each member before the cutoff: the newest deciding row is ENTERED.
_ENTERED_MEMBERS = f"SELECT m.id, m.panel_id, m.vendor_asset_id FROM ({_RANKED_DECIDING}) m WHERE m.rnk = 1 AND m.transition = 'ENTERED'"  # nosec B608 -- constants only
# A transition that survives (alias t): recent, in a recent pass, or (for a member before the cutoff) the ENTERED row that
# makes it one AND its newest row of any kind (latest_state's answer: the CREATED, DEFERRED or DRAINING that ties with or
# follows the ENTERED). A dry-run pass's rows are never kept before the cutoff.
# A row that names an open request is dropped whatever kept it (tj-7zjov1.333): the outer AND, so panel_pass follows
# only the transitions that stay.
_TRANSITION_KEPT = (
    '(t.as_of >= %(cutoff)s '  # nosec B608 -- constants and bind placeholders only
    'OR t.pass_id IN (SELECT q.id FROM panel_pass q WHERE q.started_at >= %(cutoff)s) '
    f'OR t.id IN (SELECT e.id FROM ({_ENTERED_MEMBERS}) e) '
    f'OR t.id IN (SELECT a.id FROM ({_RANKED}) a WHERE a.rnk = 1 AND EXISTS ('
    f'SELECT 1 FROM ({_ENTERED_MEMBERS}) n WHERE n.panel_id = a.panel_id AND n.vendor_asset_id = a.vendor_asset_id))) '
    f'AND NOT {_NAMES_OPEN_REQUEST.format(alias="t")}'
)


# One SELECT per table, written out. Parameters: cutoff, session_cutoff, corporate_cutoff, now (aware datetimes).
# The entry's start is replaced in place by the record function, so the statement names no column and stays right when a
# revision adds one (the manifest's fingerprint is what catches that). Every statement ends in ORDER BY id.
def _fill(template: str, **fragments: str) -> str:
    """Fill the <SLOT> markers of a statement with the fixed predicates above. Constants only; nothing is input."""
    for name, fragment in fragments.items():
        template = template.replace(f'<{name}>', fragment)
    return template


_TRIM_TEMPLATES: Final = {
    'store_dataset_entry': (
        'SELECT (jsonb_populate_record(e, jsonb_build_object(\'start\', GREATEST(e."start", %(session_cutoff)s)))).* '
        'FROM store_dataset_entry e WHERE <ENTRY> ORDER BY e.id'
    ),
    'stock_market_activity': (
        'SELECT m.* FROM stock_market_activity m WHERE m."timestamp" >= %(session_cutoff)s '
        'AND m.dataset_id IN (SELECT e.id FROM store_dataset_entry e WHERE <ENTRY>) ORDER BY m.id'
    ),
    'ingest_run': (
        'SELECT r.* FROM ingest_run r WHERE (r.started_at >= %(cutoff)s OR r.id IN ('
        'SELECT DISTINCT ON (a.dataset_id) a.id FROM ingest_run a '
        'WHERE a.dataset_id IN (SELECT e.id FROM store_dataset_entry e WHERE <ENTRY>) '
        'AND a.vendor_asset_id IS NOT NULL AND a.granularity IS NOT NULL '
        "AND a.state IN ('SUCCEEDED', 'EMPTY') "
        'ORDER BY a.dataset_id, a.finished_at DESC NULLS LAST, a.id DESC)) '
        'AND NOT <OPEN_RUN> ORDER BY r.id'
    ),
    'ingest_request': (
        'SELECT r.* FROM ingest_request r WHERE r.created_at >= %(cutoff)s AND r.state IN <TERMINAL> ORDER BY r.id'
    ),
    'ingest_instance': 'SELECT i.* FROM ingest_instance i WHERE false ORDER BY i.instance_id',
    'dataset_read': 'SELECT d.* FROM dataset_read d WHERE false ORDER BY d.id',
    'dataset_read_daily': 'SELECT d.* FROM dataset_read_daily d WHERE false ORDER BY d.id',
    'dataset_override_log': 'SELECT d.* FROM dataset_override_log d WHERE false ORDER BY d.id',
    'dataset_deletion': 'SELECT d.* FROM dataset_deletion d WHERE false ORDER BY d.id',
    'asset_list_snapshot': 'SELECT s.* FROM asset_list_snapshot s WHERE <SNAPSHOT> ORDER BY s.id',
    'asset_list_row': (
        'SELECT w.* FROM asset_list_row w WHERE w.snapshot_id IN '
        '(SELECT s.id FROM asset_list_snapshot s WHERE <SNAPSHOT>) ORDER BY w.id'
    ),
    'asset_list_event': (
        'SELECT v.* FROM asset_list_event v WHERE v.snapshot_id IN '
        '(SELECT s.id FROM asset_list_snapshot s WHERE <SNAPSHOT>) ORDER BY v.id'
    ),
    'corporate_action': 'SELECT c.* FROM corporate_action c WHERE c.as_of >= %(corporate_cutoff)s ORDER BY c.id',
    'panel': 'SELECT p.* FROM panel p ORDER BY p.id',
    'panel_settings_version': 'SELECT v.* FROM panel_settings_version v ORDER BY v.id',
    'panel_source_settings_version': 'SELECT v.* FROM panel_source_settings_version v ORDER BY v.id',
    # (B105 is bandit reading the key 'panel_pass' as a password; it is a table name.)
    'panel_pass': (  # nosec B105
        'SELECT p.* FROM panel_pass p WHERE p.started_at >= %(cutoff)s OR p.id IN '
        '(SELECT t.pass_id FROM panel_member_transition t WHERE <TRANSITION>) ORDER BY p.id'
    ),
    'panel_member_transition': 'SELECT t.* FROM panel_member_transition t WHERE <TRANSITION> ORDER BY t.id',
}
TRIM_SQL: Final = {
    table: _fill(
        template,
        ENTRY=_ENTRY_KEPT,
        SNAPSHOT=_SNAPSHOT_KEPT,
        TRANSITION=_TRANSITION_KEPT,
        TERMINAL=_TERMINAL_STATES,
        OPEN_RUN=_NAMES_OPEN_REQUEST.format(alias='r'),
    )
    for table, template in _TRIM_TEMPLATES.items()
}
# What a dry run asks for each table: the rows the trim keeps, and roughly what they weigh in the heap (pg_column_size of
# the whole row; the COPY text is a different size, so the number is an estimate).
ESTIMATE_SQL: Final = {
    table: f'SELECT count(*), coalesce(sum(pg_column_size(k)), 0) FROM ({sql}) k'  # nosec B608 -- see TRIM_SQL
    for table, sql in TRIM_SQL.items()
}

# The reload. One COPY per table, written out (no name is assembled), in SEED_TABLES order. A file holds the table's
# columns in table order (SELECT <alias>.*), which the column fingerprint holds the schema to.
LOAD_SQL: Final = {
    'store_dataset_entry': 'COPY store_dataset_entry FROM STDIN',
    'stock_market_activity': 'COPY stock_market_activity FROM STDIN',
    'ingest_run': 'COPY ingest_run FROM STDIN',
    'ingest_request': 'COPY ingest_request FROM STDIN',
    'ingest_instance': 'COPY ingest_instance FROM STDIN',
    'dataset_read': 'COPY dataset_read FROM STDIN',
    'dataset_read_daily': 'COPY dataset_read_daily FROM STDIN',
    'dataset_override_log': 'COPY dataset_override_log FROM STDIN',
    'dataset_deletion': 'COPY dataset_deletion FROM STDIN',
    'asset_list_snapshot': 'COPY asset_list_snapshot FROM STDIN',
    'asset_list_row': 'COPY asset_list_row FROM STDIN',
    'asset_list_event': 'COPY asset_list_event FROM STDIN',
    'corporate_action': 'COPY corporate_action FROM STDIN',
    'panel': 'COPY panel FROM STDIN',
    'panel_settings_version': 'COPY panel_settings_version FROM STDIN',
    'panel_source_settings_version': 'COPY panel_source_settings_version FROM STDIN',
    # (B105 is bandit reading the key 'panel_pass' as a password; it is a table name.)
    'panel_pass': 'COPY panel_pass FROM STDIN',  # nosec B105
    'panel_member_transition': 'COPY panel_member_transition FROM STDIN',
}
# R3: an explicit-id load leaves a serial's sequence behind, so the next insert would collide. Only
# stock_market_activity.id is a serial (every other key is a uuid or a text), and the loader proves that on the live
# schema before it trusts this list (SERIAL_COLUMNS_SQL). An empty table leaves the sequence at 1, not yet called.
SETVAL_SQL: Final = {
    'stock_market_activity': (
        "SELECT setval(pg_get_serial_sequence('stock_market_activity', 'id'), "
        'coalesce((SELECT max(id) FROM stock_market_activity), 1), '
        '(SELECT count(*) > 0 FROM stock_market_activity))'
    )
}
SERIAL_COLUMNS: Final = frozenset({('stock_market_activity', 'id')})
SERIAL_COLUMNS_SQL: Final = (
    'SELECT table_name, column_name FROM information_schema.columns '
    "WHERE table_schema = 'public' AND table_name = ANY(%(tables)s) "
    "AND (column_default LIKE 'nextval(%%' OR is_identity = 'YES') ORDER BY table_name, column_name"
)
MANIFEST_LIMIT: Final = 1 << 20
_CHUNK: Final = 1 << 20
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class DevSeedError(Exception):
    """A refusal or a failure of the export. The message is written for the operator and never holds a secret."""


class DevSeedRefused(DevSeedError):
    """The database revision is not the checkout head (or not readable): nothing was written. request.py exits 3."""


@dataclass(frozen=True)
class Cutoffs:
    """The instants one export trims by, all derived from one moment."""

    now: datetime
    cutoff: datetime
    session_cutoff: datetime
    corporate_cutoff: datetime

    def parameters(self) -> dict[str, datetime]:
        return {
            'now': self.now,
            'cutoff': self.cutoff,
            'session_cutoff': self.session_cutoff,
            'corporate_cutoff': self.corporate_cutoff,
        }


@dataclass(frozen=True)
class ExportResult:
    """What an export wrote."""

    directory: Path
    revision: str
    counts: dict[str, int]
    pruned: tuple[str, ...]


def session_boundary(instant: datetime) -> datetime:
    """The first New York midnight at or after an instant: where a whole session can begin.

    Args:
        instant: An aware instant.

    Returns:
        datetime: Aware, in New York. The instant itself when it is already a New York midnight.
    """
    local = instant.astimezone(MARKET_TZ)
    day = local.date()
    if local.time() != time(0):
        day += timedelta(days=1)
    return datetime.combine(day, time(0), tzinfo=MARKET_TZ)


def cutoffs_for(moment: datetime) -> Cutoffs:
    """Every cutoff of an export started at `moment` (aware)."""
    if moment.tzinfo is None:
        raise DevSeedError('the export instant must be timezone-aware')
    now = moment.astimezone(UTC)
    cutoff = now - timedelta(days=CUTOFF_DAYS)
    return Cutoffs(
        now=now,
        cutoff=cutoff,
        session_cutoff=session_boundary(cutoff),
        corporate_cutoff=now - timedelta(days=CORPORATE_ACTION_DAYS),
    )


def _module_assignments(path: Path) -> dict[str, object]:
    """The module-level `name = literal` and `name: T = literal` assignments of a source file."""
    values: dict[str, object] = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            targets = [node.target.id]
        elif isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        else:
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            continue
        for name in targets:
            values[name] = value
    return values


def head_revision(versions_dir: Path = VERSIONS_DIR) -> str:
    """The single head of the store's revision chain, read from the migration files without alembic.

    A mirror of data.store.seeds.manifest.head_revision (same rule: the revision no other revision names as its
    down_revision); a test holds the two to the same answer.

    Raises:
        DevSeedError: When there are no revision files, or not exactly one head.
    """
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(versions_dir.glob('*.py')):
        values = _module_assignments(path)
        revision = values.get('revision')
        if not isinstance(revision, str):
            continue
        revisions.add(revision)
        down = values.get('down_revision')
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, tuple):
            parents.update(item for item in down if isinstance(item, str))
    heads = revisions - parents
    if len(heads) != 1:
        raise DevSeedError(f'expected exactly one migration head in {versions_dir}, found {len(heads)}')
    return next(iter(heads))


def column_fingerprint(columns: list[tuple[str, str, str]]) -> str:
    """sha256 (hex) of a table's columns: name, data type and udt name, one line each, in ordinal order."""
    text = ''.join(f'{name}|{data_type}|{udt}\n' for name, data_type, udt in columns)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class _Sink:
    """The file the COPY streams into: counts bytes and rows and hashes what it writes.

    A COPY text row ends in one newline and a newline inside a value is escaped, so the newlines are the rows.
    """

    def __init__(self, handle: Any):
        self._handle = handle
        self._digest = hashlib.sha256()
        self.size = 0
        self.rows = 0

    def write(self, data: bytes | str) -> int:
        chunk = data.encode('utf-8') if isinstance(data, str) else bytes(data)
        self._handle.write(chunk)
        self._digest.update(chunk)
        self.size += len(chunk)
        self.rows += chunk.count(b'\n')
        return len(chunk)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _create_file(path: Path) -> Any:
    """Open a new file, 0600, refusing an existing one and a symlink."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    return os.fdopen(descriptor, 'wb')


def _sync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    """Make sure `path` is a real directory, creating it 0700. Refuses a symbolic link or a non-directory (no-follow)."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        path.mkdir(mode=DIRECTORY_MODE)  # does not follow a link planted since the lstat: it fails instead
        return
    if stat.S_ISLNK(mode):
        raise DevSeedError(f'refused: {path} is a symbolic link; the seed is never written through one')
    if not stat.S_ISDIR(mode):
        raise DevSeedError(f'refused: {path} exists and is not a directory')


def _seed_levels(root: Path, revision: str) -> list[Path]:
    """Every directory from the repository root down to the revision directory; just root's chain when elsewhere."""
    try:
        relative = root.relative_to(REPO_ROOT)
        base = REPO_ROOT
    except ValueError:
        relative = Path(root.name)
        base = root.parent
    levels = []
    current = base
    for part in (*relative.parts, revision):
        current = current / part
        levels.append(current)
    return levels


def _make_directory(root: Path, revision: str, moment: datetime) -> Path:
    """output/dev_seed/<revision>/<stamp>/, each level 0700 and none a symbolic link. Refuses a stamp that exists."""
    for level in _seed_levels(root, revision):
        _ensure_directory(level)
    parent = root / revision
    directory = parent / moment.astimezone(UTC).strftime(STAMP_FORMAT)
    try:
        directory.mkdir(mode=DIRECTORY_MODE)
    except FileExistsError:
        raise DevSeedError(f'{directory.name} already exists under {parent}: wait a second and rerun') from None
    return directory


def _prune(revision_dir: Path, keep: int) -> tuple[str, ...]:
    """Remove all but the newest `keep` COMPLETE stamp directories of a revision, and incomplete ones older than those.

    Complete means manifest.json is a regular file (a SIGKILLed export has none, and is not a seed). An incomplete stamp
    newer than the newest complete one is left alone, since it may be an export still running. Never follows or removes
    anything else.
    """
    complete: list[str] = []
    incomplete: list[str] = []
    for entry in revision_dir.iterdir():
        if not (_STAMP.match(entry.name) and entry.is_dir() and not entry.is_symlink()):
            continue
        try:
            finished = stat.S_ISREG(os.lstat(entry / MANIFEST_NAME).st_mode)
        except OSError:
            finished = False
        (complete if finished else incomplete).append(entry.name)
    complete.sort(reverse=True)
    removed = complete[keep:]
    if complete:
        removed += [name for name in incomplete if name < complete[0]]
    removed.sort(reverse=True)
    for name in removed:
        shutil.rmtree(revision_dir / name)
    return tuple(removed)


def session_settings(cursor: Any) -> None:
    """The transaction-local settings that fix the text of a timestamp and a float, for writing and for reloading."""
    cursor.execute(TIMEZONE_SQL)
    cursor.execute(FLOAT_DIGITS_SQL)


def _begin(cursor: Any) -> None:
    cursor.execute(READ_ONLY_SQL)
    session_settings(cursor)


def _database_revision(cursor: Any) -> str:
    cursor.execute(REVISION_SQL)
    rows = cursor.fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise DevSeedRefused(f'alembic_version holds {len(rows)} row(s), not exactly one revision: migrate first')
    return rows[0][0]


def _check_revision(cursor: Any, head: str) -> str:
    revision = _database_revision(cursor)
    if revision != head:
        raise DevSeedRefused(
            f'refused: the database is at revision {revision} and the checkout head is {head}; '
            'migrate the dev database to the head first. Nothing was written'
        )
    return revision


def _columns(cursor: Any, table: str) -> list[tuple[str, str, str]]:
    cursor.execute(COLUMNS_SQL, {'table': table})
    columns = [(str(name), str(data_type), str(udt)) for name, data_type, udt in cursor.fetchall()]
    if not columns:
        raise DevSeedError(f'{table} has no columns in information_schema: is the schema migrated?')
    return columns


def _write_table(cursor: Any, directory: Path, table: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    """COPY one table's trimmed rows into its file; returns the manifest entry."""
    statement = cursor.mogrify(TRIM_SQL[table], parameters)
    if isinstance(statement, bytes):
        statement = statement.decode('utf-8')
    path = directory / f'{table}{FILE_SUFFIX}'
    handle = _create_file(path)
    try:
        sink = _Sink(handle)
        cursor.copy_expert(f'COPY ({statement}) TO STDOUT', sink)  # nosec B608 -- constants, values bound by mogrify
        _sync_file(handle)
    finally:
        handle.close()
    columns = _columns(cursor, table)
    return {
        'bytes': sink.size,
        'columns': [name for name, _, _ in columns],
        'file': path.name,
        'fingerprint': column_fingerprint(columns),
        'rows': sink.rows,
        'sha256': sink.hexdigest(),
    }


def _write_manifest(directory: Path, manifest: Mapping[str, Any]) -> None:
    handle = _create_file(directory / MANIFEST_NAME)
    try:
        handle.write((json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8'))
        _sync_file(handle)
    finally:
        handle.close()


def export_seed(
    connection: Any,
    label: str,
    moment: datetime,
    say: Callable[[str], object],
    *,
    root: Path = SEED_ROOT,
    versions_dir: Path = VERSIONS_DIR,
    keep: int = KEEP_SEEDS,
) -> ExportResult:
    """Write the trimmed seed of a dev or test database. The caller has checked the rail and owns the connection.

    The connection is rolled back first (ending the rail check's transaction), used in ONE REPEATABLE READ READ ONLY
    transaction and rolled back again: nothing is ever committed. A failure removes the directory it made.

    Args:
        connection: A DB-API connection that has passed the database rail.
        label: The rail's label ('dev' or 'test'), recorded in the manifest.
        moment: The export instant (aware); every cutoff and the directory stamp come from it.
        say: Where progress lines go (never a row's content).
        root: The seed root; output/dev_seed by default.
        versions_dir: The store's migration versions directory.
        keep: How many stamps of this revision to keep.

    Returns:
        ExportResult: The directory written, the revision, the row counts, the stamps pruned.

    Raises:
        DevSeedError: On a revision that is not the checkout head, a file that cannot be created, or a database failure
            (reported by type name only).
    """
    head = head_revision(versions_dir)
    cutoffs = cutoffs_for(moment)
    try:
        connection.rollback()
        with connection.cursor() as cursor:
            _begin(cursor)
            directory, revision, tables = write_seed(cursor, label, cutoffs, head, root, say)
    except DevSeedError:
        raise
    except Exception as exc:
        raise DevSeedError(f'the export failed and its files were removed ({type(exc).__name__})') from None
    finally:
        connection.rollback()
    pruned = prune_quietly(directory.parent, keep, say)
    say(f'export: wrote {directory}')
    return ExportResult(
        directory=directory,
        revision=revision,
        counts={table: entry['rows'] for table, entry in tables.items()},
        pruned=pruned,
    )


def write_seed(
    cursor: Any, label: str, cutoffs: Cutoffs, head: str, root: Path, say: Callable[[str], object]
) -> tuple[Path, str, dict[str, dict[str, Any]]]:
    """Write the seed files and the manifest from a cursor that is already inside a transaction.

    Used by the export (a READ ONLY transaction) and by the preserving reset (the locked write transaction), so both write
    the same files. A failure removes the directory it made; nothing is pruned (prune_quietly does that, once the seed is
    safe to rely on).

    Returns:
        tuple: The stamp directory, the revision, and the manifest's per-table entries.

    Raises:
        DevSeedRefused: When the database revision is not the checkout head; nothing was written.
        DevSeedError: On a file that cannot be created. Any other exception from the driver propagates after the
            directory is removed.
    """
    parameters = cutoffs.parameters()
    revision = _check_revision(cursor, head)
    say(f'export: revision {revision} (the checkout head); cutoff {cutoffs.cutoff.isoformat()}')
    directory = _make_directory(root, revision, cutoffs.now)
    try:
        tables = {}
        for table in SEED_TABLES:
            tables[table] = _write_table(cursor, directory, table, parameters)
            say(f'  {table}: rows {tables[table]["rows"]} bytes {tables[table]["bytes"]}')
        manifest = {
            'checkout_head': head,
            'corporate_action_cutoff': cutoffs.corporate_cutoff.isoformat(),
            'corporate_action_days': CORPORATE_ACTION_DAYS,
            'created_at': cutoffs.now.isoformat(),
            'cutoff': cutoffs.cutoff.isoformat(),
            'cutoff_days': CUTOFF_DAYS,
            'format': MANIFEST_FORMAT,
            'load_order': list(SEED_TABLES),
            'revision': revision,
            'session_cutoff': cutoffs.session_cutoff.isoformat(),
            'stack': label,
            'tables': tables,
        }
        _write_manifest(directory, manifest)
        _sync_directory(directory)
        _sync_directory(directory.parent)
    except Exception:
        _discard(directory)
        raise
    return directory, revision, tables


def _discard(directory: Path | None) -> None:
    """Remove a half-written export directory (ours: this run created it). Never raises."""
    if directory is None:
        return
    try:
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
    except OSError:
        pass


def prune_quietly(revision_dir: Path, keep: int, say: Callable[[str], object]) -> tuple[str, ...]:
    try:
        pruned = _prune(revision_dir, keep)
    except OSError as exc:
        say(f'export: could not remove an older seed ({type(exc).__name__}); the new one is complete')
        return ()
    if pruned:
        say(f'export: kept the newest {keep}; removed {", ".join(pruned)}')
    return pruned


def estimate_seed(
    connection: Any, moment: datetime, say: Callable[[str], object], *, versions_dir: Path = VERSIONS_DIR
) -> dict[str, tuple[int, int]]:
    """What an export would write, per table: rows and an estimated byte count. Reads only; writes no file.

    Runs the same read-only transaction, revision check and trim statements as export_seed.

    Returns:
        dict[str, tuple[int, int]]: Table to (rows, estimated heap bytes).

    Raises:
        DevSeedError: On a revision that is not the checkout head or a database failure (type name only).
    """
    head = head_revision(versions_dir)
    parameters = cutoffs_for(moment).parameters()
    estimates: dict[str, tuple[int, int]] = {}
    try:
        connection.rollback()
        with connection.cursor() as cursor:
            _begin(cursor)
            revision = _check_revision(cursor, head)
            say(f'export dry run: revision {revision} (the checkout head); nothing is written')
            for table in SEED_TABLES:
                cursor.execute(ESTIMATE_SQL[table], parameters)
                rows, size = cursor.fetchone()
                estimates[table] = (int(rows), int(size))
                say(f'  {table}: rows {estimates[table][0]} approx heap bytes {estimates[table][1]}')
    except DevSeedError:
        raise
    except Exception as exc:
        raise DevSeedError(f'the export estimate failed ({type(exc).__name__})') from None
    finally:
        connection.rollback()
    say(f'export dry run: {sum(r for r, _ in estimates.values())} rows in all')
    return estimates


def _open_regular(path: Path) -> Any:
    """Open an existing file read-only, never through a symbolic link, and only when it is a regular file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise DevSeedError(f'{path.name} cannot be opened (missing, or a symbolic link)') from None
    handle = os.fdopen(descriptor, 'rb')
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise DevSeedError(f'{path.name} is not a regular file')
    return handle


def _is_complete(directory: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(directory / MANIFEST_NAME).st_mode)
    except OSError:
        return False


def resolve_seed(path: Path) -> Path:
    """The stamp directory a load reads: `path` itself when it holds a manifest, else its newest COMPLETE stamp.

    A symbolic link is refused (the seed is never read through one), as is a directory with no complete stamp. A stamp
    without manifest.json is a SIGKILLed export and is skipped.

    Raises:
        DevSeedError: When `path` is not a directory, is a link, or holds no complete seed.
    """
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        raise DevSeedError(f'refused: {path} does not exist') from None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise DevSeedError(f'refused: {path} is not a plain directory (a symbolic link or a file)')
    if _is_complete(path):
        return path
    stamps = sorted(
        (
            entry
            for entry in path.iterdir()
            if _STAMP.match(entry.name) and entry.is_dir() and not entry.is_symlink() and _is_complete(entry)
        ),
        key=lambda entry: entry.name,
    )
    if not stamps:
        raise DevSeedError(f'refused: {path} holds no complete seed (no {MANIFEST_NAME} in it or in a stamp under it)')
    return stamps[-1]


def read_manifest(directory: Path) -> dict[str, Any]:
    """Read and shape-check a seed's manifest.

    Raises:
        DevSeedError: When it cannot be read, is not JSON, or lacks what a load needs: a format this code knows, a
            revision, a stack ('dev' or 'test'), and for exactly the SEED_TABLES a row count, a byte count, a sha256 and a fingerprint. Messages
            name the field, never a value.
    """
    handle = _open_regular(directory / MANIFEST_NAME)
    try:
        raw = handle.read(MANIFEST_LIMIT + 1)
    finally:
        handle.close()
    if len(raw) > MANIFEST_LIMIT:
        raise DevSeedError(f'{MANIFEST_NAME} is larger than {MANIFEST_LIMIT} bytes: not a seed manifest')
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise DevSeedError(f'{MANIFEST_NAME} is not valid JSON') from None
    if not isinstance(manifest, dict) or manifest.get('format') != MANIFEST_FORMAT:
        raise DevSeedError(f'{MANIFEST_NAME} is not a format {MANIFEST_FORMAT} seed manifest')
    revision = manifest.get('revision')
    if not isinstance(revision, str) or not revision:
        raise DevSeedError(f'{MANIFEST_NAME} has no revision')
    if manifest.get('stack') not in SEED_STACKS:
        raise DevSeedError(f"{MANIFEST_NAME} has no stack, or one that is not 'dev' or 'test'")
    tables = manifest.get('tables')
    if not isinstance(tables, dict) or set(tables) != set(SEED_TABLES):
        raise DevSeedError(f'{MANIFEST_NAME} does not list exactly the {len(SEED_TABLES)} seed tables')
    for table in SEED_TABLES:
        entry = tables[table]
        valid = (
            isinstance(entry, dict)
            and all(isinstance(entry.get(key), int) and entry[key] >= 0 for key in ('rows', 'bytes'))
            and isinstance(entry.get('sha256'), str)
            and _SHA256.match(entry['sha256']) is not None
            and isinstance(entry.get('fingerprint'), str)
        )
        if not valid:
            raise DevSeedError(f'{MANIFEST_NAME}: the entry for {table} is malformed')
    return manifest


def verify_files(directory: Path, tables: Mapping[str, Mapping[str, Any]]) -> None:
    """Check every file's size and sha256 against the manifest. Reads only; call it before anything is written.

    Raises:
        DevSeedError: Naming the file and which of size or sha256 differs, never a content.
    """
    for table in SEED_TABLES:
        path = directory / f'{table}{FILE_SUFFIX}'
        handle = _open_regular(path)
        try:
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        finally:
            handle.close()
        if size != tables[table]['bytes']:
            raise DevSeedError(f'{path.name} has {size} bytes and the manifest says {tables[table]["bytes"]}: refused')
        if digest.hexdigest() != tables[table]['sha256']:
            raise DevSeedError(f'{path.name} does not match the manifest sha256: refused')


class _Source:
    """The file a COPY FROM reads: hashes what the server consumes, so a file changed after verify_files is caught."""

    def __init__(self, handle: Any):
        self._handle = handle
        self._digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        self._digest.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def reload_seed(
    cursor: Any, directory: Path, tables: Mapping[str, Mapping[str, Any]], say: Callable[[str], object]
) -> None:
    """COPY every table's file in (parents first), then setval the serial sequences to max(id).

    The caller owns the transaction and the emptiness of the tables, and recounts afterwards. No file is trusted
    blind: the bytes the server reads are hashed and compared to the manifest.

    Raises:
        DevSeedError: When a file changes between verification and its COPY.
    """
    for table in SEED_TABLES:
        handle = _open_regular(directory / f'{table}{FILE_SUFFIX}')
        try:
            source = _Source(handle)
            cursor.copy_expert(LOAD_SQL[table], source)
        finally:
            handle.close()
        if source.hexdigest() != tables[table]['sha256']:
            raise DevSeedError(f'{table}{FILE_SUFFIX} changed while it was loaded: refused')
        say(f'  {table}: loaded')
    for statement in SETVAL_SQL.values():
        cursor.execute(statement)


def _check_fingerprints(cursor: Any, tables: Mapping[str, Mapping[str, Any]]) -> None:
    stale = [
        table for table in SEED_TABLES if column_fingerprint(_columns(cursor, table)) != tables[table]['fingerprint']
    ]
    if stale:
        raise DevSeedRefused(
            f"refused: the columns of {', '.join(stale)} differ from the seed's (name, type or order); the schema "
            'moved without the revision. Nothing was changed'
        )


def _check_serials(cursor: Any) -> None:
    """Prove the live schema has exactly the serial columns SETVAL_SQL resets."""
    cursor.execute(SERIAL_COLUMNS_SQL, {'tables': list(SEED_TABLES)})
    found = {(str(table), str(column)) for table, column in cursor.fetchall()}
    if found != SERIAL_COLUMNS:
        raise DevSeedError(
            'refused: the schema has serial or identity columns this loader does not reset '
            f'({sorted(found ^ SERIAL_COLUMNS)}); update SETVAL_SQL. Nothing was changed'
        )


def load_seed(
    cursor: Any,
    directory: Path,
    manifest: Mapping[str, Any],
    head: str,
    count_rows: Callable[[Any], Mapping[str, int]],
    say: Callable[[str], object],
) -> None:
    """Put a seed into EMPTY tables, from a cursor already inside the locked transaction.

    In order, writing nothing until the last step: seed revision = database alembic_version = checkout head; every
    column fingerprint; the serial columns; every table empty; every file's size and sha256; then the COPYs and the
    setvals. The caller recounts against the manifest and commits (a failure there rolls everything back).

    Args:
        cursor: A cursor inside a transaction that holds the ACCESS EXCLUSIVE lock on the eighteen tables.
        directory: The stamp directory (see resolve_seed).
        manifest: The manifest read_manifest returned.
        head: The checkout head revision.
        count_rows: Returns the row count of each of the eighteen tables.
        say: Where progress lines go.

    Raises:
        DevSeedRefused: A revision or a column layout that does not match (the seed is stale, or the schema moved).
        DevSeedError: A table that holds rows, a file that does not match the manifest, or a changed schema.
    """
    seed_revision = manifest['revision']
    database_revision = _database_revision(cursor)
    if not seed_revision == database_revision == head:
        raise DevSeedRefused(
            f'refused: the seed is at revision {seed_revision}, the database at {database_revision} and the checkout '
            f'head is {head}; a seed loads only when all three are the same. Migrate the dev database to the head, '
            'then reset (it exports a seed of the current revision). Nothing was changed'
        )
    tables = manifest['tables']
    _check_fingerprints(cursor, tables)
    _check_serials(cursor)
    populated = {table: rows for table, rows in count_rows(cursor).items() if rows}
    if populated:
        detail = ', '.join(f'{table} {rows}' for table, rows in populated.items())
        raise DevSeedError(
            f'refused: a load fills empty tables only and these hold rows ({detail}). Nothing was changed'
        )
    verify_files(directory, tables)
    say(f'load: revision {seed_revision} matches the database and the checkout; files verified; loading')
    reload_seed(cursor, directory, tables, say)
