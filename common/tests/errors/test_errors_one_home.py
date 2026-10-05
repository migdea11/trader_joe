"""ONE HOME AND ONE JOIN for own_error_id and has_cause_chain (validator, gating tj-zxqn4r; ADR tj-fa1rpu D8).

WHAT WENT WRONG, because it is the whole reason this file exists. The two helpers were COPIED into
common/rpc/errors.py and routers/common/errors.py. Within two days of the second copy being written they had
diverged on their fourth line -- the gRPC copy joined a sequence-valued error_id with ',' and the HTTP copy with
', ' -- while the gRPC copy carried a comment asserting the opposite in so many words: "Read exactly as
routers/common/errors.py reads it, so an error relayed from one transport to the other is judged the same way on
both." Nothing in PR 2 builds a sequence-valued error_id, so no test drove the branch that differed and the
divergence was invisible. The first such id would have made ONE failure name TWO different ids in the two
services' logs, which is precisely the correlation D8 exists to provide.

THIS FILE HOLDS THE TWO HALVES OF THE REPAIR, and they are different claims:

  1. THE VALUES AGREE TODAY -- test_a_sequence_valued_id_is_one_string_at_both_edges. One error with a
     sequence-valued id, driven through both transports, must produce one string on the gRPC wire, in the gRPC
     log line and in the HTTP log line. THIS CASE WOULD HAVE BEEN RED BEFORE tj-zxqn4r: the HTTP line said
     'a1, b2' where the wire said 'a1,b2'. It is the test the divergence never had.

  2. THERE IS ONE HOME -- test_neither_edge_reimplements_the_shared_judgements. And this is the half that
     needs saying carefully, because ADDENDUM 3 on tj-tkm4tn was written about exactly this mistake: A GUARD
     AGAINST DUPLICATION MUST BE ABLE TO FAIL ON DUPLICATION, and an equality of resolved values never can,
     because equal-but-distinct is precisely what duplication produces. Claim 1 CANNOT see a re-duplication:
     paste a verbatim copy of own_error_id back into either edge and claim 1 stays green, because a faithful
     copy agrees. That is not a hypothetical -- the FetchEvent mirror guard sat green for days over the
     duplication it was written to forbid, and the architect found it by producing the state rather than
     reasoning about it. So claim 2 is STRUCTURAL: it reads each edge's source and asserts that the file
     contains no function making either judgement for itself, and that each edge imports and uses the shared
     one. Demonstrated by producing the forbidden state -- see the verdict note on tj-zxqn4r for the paste-back
     run, where this case reds alone while every behavioural suite stays green.

BOTH ARE NEEDED AND NEITHER SUBSTITUTES. Claim 1 catches a shared helper whose behaviour drifts from what the
wire does. Claim 2 catches a second home appearing. The mutation that reds both -- changing the shared
separator -- proves the single home is REACHED, not that a second one cannot be added.
"""

import ast
import logging
from pathlib import Path

import pytest
from google.rpc import error_details_pb2, status_pb2

from common.errors.vocabulary import METADATA_SEQUENCE_SEPARATOR, InvalidRequestError, Reason, own_error_id
from common.rpc.errors import abort_with_error
from routers.tests.problem_app import answer_to


pytestmark = pytest.mark.common

# A reason both transports render: it is on the InvalidRequestError branch and its row names a gRPC code.
SHARED_REASON = Reason.NOT_FOUND

GRPC_LOGGER = 'common.rpc.errors'
HTTP_LOGGER = 'routers.common.errors'

# The two edge modules. Both are read as SOURCE for the structural claim, so the paths are resolved from the
# repository root rather than from an imported module's __file__ -- a module that failed to import would
# otherwise make the guard silently unrunnable rather than red.
REPO_ROOT = Path(__file__).resolve().parents[3]
EDGE_SOURCES = {
    'common/rpc/errors.py': REPO_ROOT / 'common' / 'rpc' / 'errors.py',
    'routers/common/errors.py': REPO_ROOT / 'routers' / 'common' / 'errors.py',
}

# The two judgements that must have exactly one home, and the shared name each edge must reach them by.
SHARED_NAMES = ('own_error_id', 'has_cause_chain')
SHARED_MODULE = 'common.errors.vocabulary'


def sequence_error(parts: tuple[str, ...]) -> InvalidRequestError:
    """An error carrying a sequence-valued error_id, which is the shape the two copies disagreed about."""
    return InvalidRequestError(SHARED_REASON, 'the same failure, seen at two edges', metadata={'error_id': parts})


class _RecordingContext:
    """The least a grpc.aio.ServicerContext needs to be for abort_with_error to run and log.

    abort_with_error renders, logs one line, then awaits context.abort. A real context's abort raises to end
    the RPC; recording and returning is enough here, because the line this case reads has already been written
    by the time abort is reached. Driving the whole interceptor stack would exercise the boundary, which is
    common/tests/rpc/test_rpc_errors_boundary.py's subject and not this file's.
    """

    def __init__(self):
        self.aborted_with: tuple | None = None

    async def abort(self, code, details, trailing_metadata):
        self.aborted_with = (code, details, trailing_metadata)

    def sent_status(self) -> status_pb2.Status:
        """The rich status the abort actually carried, parsed back off the trailing metadata.

        READ FROM THE ABORT AND NOT FROM A SECOND render() CALL, which is a trap worth naming: render() is
        pure and MINTS an id when the error carries none, so rendering the same error twice legitimately
        produces two different ids. A case that rendered once for the wire value and called abort_with_error
        once for the log line would be comparing two unrelated failures and would red against correct code --
        it did, before this helper existed. One call, one id, read from what that call sent.
        """
        assert self.aborted_with is not None, 'abort_with_error did not abort'
        for key, value in self.aborted_with[2]:
            if key == 'grpc-status-details-bin':
                sent = status_pb2.Status()
                sent.ParseFromString(value)
                return sent
        raise AssertionError('the abort carried no rich status')


def _error_info(status: status_pb2.Status) -> error_details_pb2.ErrorInfo:
    """The one ErrorInfo a typed status carries."""
    for packed in status.details:
        if packed.Is(error_details_pb2.ErrorInfo.DESCRIPTOR):
            info = error_details_pb2.ErrorInfo()
            packed.Unpack(info)
            return info
    raise AssertionError('every typed status carries exactly one ErrorInfo')


def _named_id(record: logging.LogRecord) -> str:
    """The id a log line ends by naming, as both edges write it: '...; error_id <id>'."""
    message = record.getMessage()
    marker = '; error_id '
    assert marker in message, f'the line does not name an error_id at all: {message!r}'
    return message.rsplit(marker, 1)[1]


def _one_line(caplog: pytest.LogCaptureFixture, logger: str) -> logging.LogRecord:
    """The single record one edge logged for one failure."""
    records = [record for record in caplog.records if record.name == logger]
    assert len(records) == 1, f'{logger} logged {len(records)} lines, not one: {[r.getMessage() for r in records]}'
    return records[0]


# ---------------------------------------------------------------------------------------------------------------
# CLAIM 1: the values agree. This is the test the divergence never had.
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'parts',
    [
        pytest.param(('a1', 'b2'), id='two-parts'),
        pytest.param(('a1', 'b2', 'c3'), id='three-parts'),
        # A part that names nothing is KEPT inside a sequence that names something, so the two edges must keep
        # it identically too -- a filter on one side only is a divergence the two-part case cannot see.
        pytest.param(('', 'b2'), id='a-leading-empty-part'),
    ],
)
@pytest.mark.asyncio
async def test_a_sequence_valued_id_is_one_string_at_both_edges(parts: tuple[str, ...], caplog):
    """ONE failure, THREE renderings, ONE id string. Red before tj-zxqn4r, by ', ' against ','.

    THE THREE PLACES AN OPERATOR MEETS THE ID, and all three have to agree or the correlation is a lie:
      * the gRPC ErrorInfo metadata value, which is what the peer receives and may log on its own side;
      * the line ingest's edge writes as it aborts;
      * the line data_store's edge writes as it answers.
    Before this bead the third was joined with ', ' and the first two with ','. An operator grepping the id
    out of data_store's log would have found nothing in ingest's, for the same failure.

    WHAT THIS CASE CANNOT DO is notice a re-duplication, and that is not a defect in it -- it is the reason
    the structural case below exists. Two faithful copies produce equal strings, so this stays green over
    exactly the state tj-zxqn4r removed (ADDENDUM 3 on tj-tkm4tn).

    Args:
        parts: A sequence-valued error_id.
        caplog: Captures one line from each edge.
    """
    expected = METADATA_SEQUENCE_SEPARATOR.join(parts)

    # ONE abort, and the wire value read from what that one abort sent -- see _RecordingContext.sent_status
    # for why a second render() call would be comparing two different failures.
    context = _RecordingContext()
    with caplog.at_level(logging.DEBUG, logger=GRPC_LOGGER):
        await abort_with_error(context, sequence_error(parts), method='/Svc/Method')
        grpc_line_id = _named_id(_one_line(caplog, GRPC_LOGGER))
    wire = _error_info(context.sent_status()).metadata['error_id']

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=HTTP_LOGGER):
        body = answer_to(sequence_error(parts)).json()
        http_line_id = _named_id(_one_line(caplog, HTTP_LOGGER))

    assert {wire, grpc_line_id, http_line_id} == {expected}, (
        f'one failure named itself three different ways: the gRPC wire says {wire!r}, the gRPC log line says '
        f'{grpc_line_id!r}, the HTTP log line says {http_line_id!r}, and the shared join gives {expected!r}. '
        f'An operator cannot follow one failure across two services unless all three are the same string, '
        f'which is the correlation D8 exists to provide.'
    )
    # The HTTP BODY keeps the list rather than the join: the join is for a log line and for the gRPC wire,
    # where a metadata value must be one string. problem+json can carry an array, so it does.
    assert body['error_id'] == list(parts), (
        f'the problem+json body carries {body["error_id"]!r}; the join is for the gRPC wire and the log lines, '
        f'and must not have reached the HTTP body, which can carry the array as it stands'
    )


@pytest.mark.parametrize(
    'names_nothing',
    [
        pytest.param('', id='the-empty-string'),
        pytest.param((), id='an-empty-sequence'),
        pytest.param(('', ''), id='a-sequence-of-nothing-but-empty-strings'),
    ],
)
@pytest.mark.asyncio
async def test_an_id_that_names_nothing_is_no_id_at_both_edges(names_nothing, caplog):
    """The bead's three "names nothing" shapes, agreed on by both transports rather than by one.

    Each edge MINTS an id when the error carries none, so the observable consequence of disagreeing here is
    worse than a different spelling: one edge would send the caller the empty value it was given and the other
    a fresh uuid. A single `is None` in one place is what makes that impossible, and these are the three values
    TraderJoeError accepts that must all collapse to it.

    Args:
        names_nothing: A metadata value that names nothing.
        caplog: Captures one line from each edge.
    """
    error = InvalidRequestError(SHARED_REASON, 'no id of its own', metadata={'error_id': names_nothing})
    assert own_error_id(error) is None, 'the shared reader does not treat this shape as "no id"'

    context = _RecordingContext()
    with caplog.at_level(logging.DEBUG, logger=GRPC_LOGGER):
        await abort_with_error(context, error, method='/Svc/Method')
        grpc_line_id = _named_id(_one_line(caplog, GRPC_LOGGER))
    wire = _error_info(context.sent_status()).metadata['error_id']

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=HTTP_LOGGER):
        http_id = answer_to(error).json()['error_id']
        http_line_id = _named_id(_one_line(caplog, HTTP_LOGGER))

    for edge, sent, named in (('gRPC', wire, grpc_line_id), ('HTTP', http_id, http_line_id)):
        assert sent and sent != names_nothing, (
            f'the {edge} edge sent {sent!r} rather than minting an id, so a value that names nothing was '
            f'passed off as a correlation id'
        )
        assert named == sent, f'the {edge} edge sent {sent!r} and named {named!r}; they must be one id'


# ---------------------------------------------------------------------------------------------------------------
# CLAIM 2: there is one home. A guard that can fail on duplication (ADDENDUM 3 on tj-tkm4tn).
# ---------------------------------------------------------------------------------------------------------------


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]


def _reads_suppress_context(function: ast.AST) -> bool:
    """Whether a function reads __suppress_context__, which is has_cause_chain's judgement and nothing else's.

    The sharpest possible marker for that helper: __suppress_context__ exists to distinguish a context the
    raise site suppressed from one it did not, and no other question in either edge needs to ask it. A copy
    that omitted it would not be a copy of has_cause_chain -- it would be the bug has_cause_chain avoids.
    """
    return any(isinstance(node, ast.Attribute) and node.attr == '__suppress_context__' for node in ast.walk(function))


def _joins_a_metadata_value(function: ast.AST) -> bool:
    """Whether a function both reads a `.metadata` attribute and joins something: own_error_id's shape.

    TWO MARKERS AND NOT ONE, because each alone has a legitimate second user in these files.
    common/rpc/errors.py's _wire_value joins, and must -- it is the wire encoding for every sequence-valued
    metadata item, which tj-zxqn4r explicitly keeps -- but it takes a bare value and reads no `.metadata`.
    render() and _describe() read `.metadata` and must, to copy it onto the wire, but neither joins. Only a
    re-implementation of own_error_id does both in one function, which is what makes the conjunction a
    precise rule rather than a heuristic that would have to be suppressed somewhere.
    """
    nodes = list(ast.walk(function))
    reads_metadata = any(isinstance(node, ast.Attribute) and node.attr == 'metadata' for node in nodes)
    joins = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'join'
        for node in nodes
    )
    return reads_metadata and joins


@pytest.mark.parametrize('relative_path', sorted(EDGE_SOURCES), ids=sorted(EDGE_SOURCES))
def test_neither_edge_reimplements_the_shared_judgements(relative_path: str):
    """No function in either edge makes either judgement for itself: the only path to them is the import.

    THIS IS THE GUARD THAT CAN FAIL ON DUPLICATION, which claim 1 above cannot. ADDENDUM 3 on tj-tkm4tn is
    explicit about why the distinction matters: the FetchEvent mirror guard compared two resolved unions for
    equality, stayed green for the entire time the duplication it forbade was live, and was credited with
    preventing it. Equal-but-distinct is what duplication produces, so equality can never be the guard.

    SOURCE, NOT IMPORTS. A test asserting `routers.common.errors.own_error_id is
    common.errors.vocabulary.own_error_id` would be the identity version, and ADDENDUM 3 records that identity
    alone is not sufficient either: a duplicate defined BELOW the import leaves the imported object bound to
    the name while everything defined after it uses the local one. Reading the source and asking whether any
    function in the file makes the judgement at all is immune to ordering.

    THE MARKERS ARE DELIBERATELY NARROW. __suppress_context__ is has_cause_chain's question and nobody else's.
    Reading `.metadata` and joining in ONE function is own_error_id's shape; each half alone is legitimate here
    and is used (see _joins_a_metadata_value). A guard that forbade joining outright would have to carve out
    _wire_value, and a carve-out is where the next copy would be put.

    Args:
        relative_path: The edge module to read, named as it appears in the repository.
    """
    source_path = EDGE_SOURCES[relative_path]
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    reimplemented = sorted(
        function.name
        for function in _functions(tree)
        if _reads_suppress_context(function) or _joins_a_metadata_value(function)
    )
    assert reimplemented == [], (
        f'{relative_path} defines {reimplemented}, which make for themselves a judgement that has one home in '
        f'{SHARED_MODULE}. That is how own_error_id and has_cause_chain came to be two copies that disagreed '
        f'about the separator within two days, under a comment claiming they agreed (tj-zxqn4r). Import '
        f'{" and ".join(SHARED_NAMES)} instead.'
    )


def test_the_grpc_wire_separator_is_the_shared_constant_and_not_a_second_spelling_of_it():
    """``_SEQUENCE_SEPARATOR = METADATA_SEQUENCE_SEPARATOR``, asserted on the SOURCE because identity cannot.

    WHAT THIS GUARDS. tj-zxqn4r's WORK item 2 asks that the gRPC hop's wire separator and the shared join
    "cannot be changed apart". The builder read that as a structural requirement rather than a documentation
    one and made _SEQUENCE_SEPARATOR the shared constant itself, flagging the choice for this gate. It is the
    right reading: the bead exists because a COMMENT asserted two things agreed and was false, so answering it
    with another comment would reproduce the defect under repair. An alias enforces what a comment only claims.

    WHY NOT `_SEQUENCE_SEPARATOR is METADATA_SEQUENCE_SEPARATOR`, which is the obvious test and is WORTHLESS
    here. CPython interns short string literals, so a module that went back to ``_SEQUENCE_SEPARATOR = ','``
    would still satisfy `is` -- measured, not assumed: `',' is METADATA_SEQUENCE_SEPARATOR` is True. An
    identity assertion would therefore be green over exactly the state it was written to forbid, which is the
    hollow guard ADDENDUM 3 on tj-tkm4tn is about. MEASURED HERE TOO: before this case existed, reverting the
    alias to a bare ',' reddened NOTHING in common/tests or routers/tests -- 1649 passed. The constant was
    reached only as its value and never as itself.

    So the claim is made where it is actually visible: in the source, the right-hand side of that assignment
    must be the NAME of the shared constant.
    """
    source_path = EDGE_SOURCES['common/rpc/errors.py']
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and target.id == '_SEQUENCE_SEPARATOR'
    ]
    (assignment,) = assignments
    value = assignment.value
    assert isinstance(value, ast.Name) and value.id == 'METADATA_SEQUENCE_SEPARATOR', (
        f'common/rpc/errors.py assigns _SEQUENCE_SEPARATOR from {ast.dump(value) if value else None}, not from '
        f'the name METADATA_SEQUENCE_SEPARATOR. The wire encoding and the log-line join must be one constant so '
        f'they cannot be changed apart (tj-zxqn4r WORK item 2); a second spelling that happens to be equal '
        f'today is how the two helpers diverged in the first place.'
    )


@pytest.mark.parametrize('relative_path', sorted(EDGE_SOURCES), ids=sorted(EDGE_SOURCES))
def test_each_edge_imports_the_shared_judgements_and_uses_them(relative_path: str):
    """The positive half: the import is present AND the name is actually called in the file.

    WHY BOTH. The case above forbids a re-implementation; on its own it is satisfied by an edge that makes
    neither judgement at all -- deleted the call, dropped the error_id, stopped logging the chain. An unused
    import satisfies a presence check just as hollowly. So the name must be imported from the one home and
    must appear as a call, which together mean the edge reaches the shared judgement and reaches it by name.

    Args:
        relative_path: The edge module to read.
    """
    source_path = EDGE_SOURCES[relative_path]
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == SHARED_MODULE
        for alias in node.names
    }
    missing = sorted(name for name in SHARED_NAMES if name not in imported)
    assert missing == [], f'{relative_path} does not import {missing} from {SHARED_MODULE}'

    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    unused = sorted(name for name in SHARED_NAMES if name not in called)
    assert unused == [], (
        f'{relative_path} imports {unused} but never calls them, so the import proves nothing about how this '
        f'edge actually decides. An unused import is not a shared judgement.'
    )
