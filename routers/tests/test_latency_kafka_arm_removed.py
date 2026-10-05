"""The latency harness has two arms, and Kafka is not one of them.

tj-3mk3u5.35. The harness's Kafka arm existed to be measured against REST and gRPC. tj-3mk3u5.26 took
that measurement and the numbers are banked on ADR tj-q3zugf and epic tj-3mk3u5; they can never be
retaken, because the arm is now gone. What is pinned here is the removal itself, not the harness's
behaviour -- the harness stays untested by the user's decision on tj-3mk3u5.8, and nothing in this
file times anything.

WHY A TRIPWIRE RATHER THAN A HANDFUL OF ASSERTIONS. The rest of the Kafka removal (tj-3mk3u5.11, .12
and .13) deletes Kafka progressively from other scopes, and each of those beads is a chance for a
reference to come back into this one -- by a merge, by a copied import, by a half-reverted edit. Three
assertions naming the three symbols that went would pass while a fourth crept in. A scan that reds on
ANY Kafka reference in routers/common or schemas/common holds the whole surface, and it keeps holding
as the surrounding deletions land.

IT READS THE AST, NOT THE TEXT. Comments in routers/common/latency.py name Kafka deliberately, to
explain what was removed and which bead removes the shim that is left; a text scan would call those
drift and force them out. Imports, names and attributes are the references that would actually wire
the arm back up, so those are what is checked.
"""

import ast
from pathlib import Path
from typing import Final

import pytest

import routers.common.latency as latency
from common.kafka.topics import ConsumerGroup
from routers.tests.latency_harness import AppStub, RecordingRestClient, rest_endpoint, turn_harness_on
from schemas.common.latency import LatencyRequest


pytestmark = pytest.mark.common

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

# The two scopes tj-3mk3u5.35 cleared. data/ and common/ are NOT here: Kafka stays wired for the
# dataset path until .11/.12, and common/kafka itself is .13's to delete.
CLEARED_TREES: Final = ('routers/common', 'schemas/common')

# Exact identifiers that belong to the Kafka RPC layer and carry no 'kafka' in their own spelling.
# LATENCY_TEST is the topic, and is matched exactly so that the env-var names LATENCY_TEST_ENABLED and
# LATENCY_TEST_TIMEOUT -- which are the harness's own and stay -- are not caught by it.
KAFKA_LAYER_NAMES: Final = frozenset(
    {'RpcEndpointTopic', 'LATENCY_TEST', 'BaseRpcAck', 'RpcEndpoint', 'get_rpc_params'}
)

# THE TWO SURVIVORS, each allowed per file and per token, so that any OTHER Kafka reference in the
# same file still reds. Both are known residue with a named owner, not oversights:
#
# routers/common/latency.py keeps `from common.kafka.topics import ConsumerGroup` only to type the
# client_group/server_group parameters that initialize_latency_client and initialize_latency_server
# still accept and no longer use. Dropping the parameters would change signatures that
# data/store/app/app_depends.py and data/ingest/app/app_depends.py call, which is a cross-scope edit
# tj-3mk3u5.35 deliberately did not make. Those call sites go on tj-3mk3u5.11 and .12.
#
# schemas/common/latency.py keeps BaseRpcAck as the base class of LatencyResponse, which is dead
# production code: it has no caller anywhere, and the only other file naming it is
# schemas/tests/test_schemas_smoke_common.py, whose declared-equals-covered assertion names it ONLY
# because the class exists. So LatencyResponse cannot be deleted by itself -- the class and those two
# test lines go in one change, across two scopes -- and tj-3mk3u5.13 owns it. It is recorded here and
# on tj-3mk3u5.35 because the Kafka-layer deletion breaks this import if it is forgotten.
#
# tj-3mk3u5.13 deletes common/kafka outright. At that point both entries must go, and the staleness
# test below is what refuses to let them linger.
ALLOWED: Final = {
    'routers/common/latency.py': frozenset({'common.kafka.topics'}),
    'schemas/common/latency.py': frozenset({'common.kafka.rpc.kafka_rpc_base', 'BaseRpcAck'}),
}


def _is_kafka(name: str) -> bool:
    """Whether an identifier belongs to the Kafka layer.

    Args:
        name (str): A module path, bare name or attribute.

    Returns:
        bool: True if it names Kafka, or a symbol of the Kafka RPC layer.
    """
    return 'kafka' in name.lower() or name in KAFKA_LAYER_NAMES


def _kafka_references(source: Path) -> list[tuple[str, int, str]]:
    """Every Kafka import, name and attribute in one module, before any allowance is applied.

    Args:
        source (Path): The module to scan.

    Returns:
        list[tuple[str, int, str]]: One (token, line number, rendered detail) per reference.
    """
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_kafka(alias.name):
                    found.append((alias.name, node.lineno, f'import {alias.name}'))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if _is_kafka(module):
                found.append((module, node.lineno, f'from {module} import ...'))
            for alias in node.names:
                if _is_kafka(alias.name):
                    found.append((alias.name, node.lineno, f'from {module} import {alias.name}'))
        elif isinstance(node, ast.Name) and _is_kafka(node.id):
            found.append((node.id, node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and _is_kafka(node.attr):
            found.append((node.attr, node.lineno, f'.{node.attr}'))
    return found


def _scan() -> dict[str, list[tuple[str, int, str]]]:
    """Scan both cleared trees.

    Returns:
        dict[str, list[tuple[str, int, str]]]: Kafka references per repository-relative path.
    """
    references = {}
    for tree in CLEARED_TREES:
        for source in sorted((REPO_ROOT / tree).rglob('*.py')):
            references[source.relative_to(REPO_ROOT).as_posix()] = _kafka_references(source)
    return references


def test_no_kafka_reference_survives_in_routers_common_or_schemas_common():
    """The arm is gone from both scopes tj-3mk3u5.35 cleared, and stays gone while .11/.12/.13 land."""
    references = _scan()

    offenders = [
        f'{relative}:{line}: {detail}'
        for relative, found in sorted(references.items())
        for token, line, detail in found
        if token not in ALLOWED.get(relative, frozenset())
    ]

    assert references, f'the scan found no modules at all under {CLEARED_TREES}; it is proving nothing'
    assert offenders == [], (
        'the latency harness lost its Kafka arm on tj-3mk3u5.35 and these scopes were cleared with '
        'it. A reference is back:\n' + '\n'.join(offenders)
    )


def test_every_allowed_kafka_reference_is_still_excusing_something():
    """The allowances above are load-bearing, so they are checked rather than trusted.

    An allowlist entry that stops matching anything is the failure mode of every allowlist: it goes on
    excusing a file long after the thing it excused has gone, and the next real reference to use that
    name walks straight through. When tj-3mk3u5.11/.12 remove the parameters and .13 deletes
    common/kafka and LatencyResponse, this test is what says each entry may now go.
    """
    references = _scan()
    stale = []
    for relative, tokens in sorted(ALLOWED.items()):
        if relative not in references:
            stale.append(f'{relative} is allowed Kafka references but is not in the scanned trees')
            continue
        present = {token for token, _, _ in references[relative]}
        for token in sorted(tokens - present):
            stale.append(f'{relative} no longer references {token}')

    assert stale == [], (
        'an allowance in ALLOWED no longer excuses anything. Delete the entry; the reference it '
        'covered is gone:\n' + '\n'.join(stale)
    )


def test_the_latency_harness_offers_exactly_rest_and_grpc():
    """Two arms, named. A third is then a deliberate act rather than something that reappears.

    The members are spelled out rather than compared against a count: RPC_KAFKA coming back under
    another name is the regression this is here for, and a length check would not see it.
    """
    members = {member.name: member.value for member in LatencyRequest.LatencyType}

    assert members == {'REST': 'rest', 'GRPC': 'grpc'}, (
        "LatencyRequest.LatencyType is the harness's whole menu of transports. The Kafka member went "
        'on tj-3mk3u5.35 after the tj-3mk3u5.26 measurement; adding an arm means adding a dispatch '
        'branch in routers/common/latency.py to match.'
    )


def test_the_topics_shim_is_importable_and_empty():
    """get_latency_topics survives the arm it served, and must now create nothing.

    It is kept only so the two lifespans keep calling it unchanged until tj-3mk3u5.11 and .12 remove
    the call sites and .13 deletes it. Both halves matter and neither is obvious from the other: if it
    stopped being importable both services would fail to start, and if it ever returned a topic again
    the lifespans would go back to provisioning Kafka topics for an arm that no longer exists.
    """
    assert latency.get_latency_topics() == (), (
        'the latency topics shim must stay empty: its callers hand the result straight to topic '
        'creation, and the arm that consumed those topics went on tj-3mk3u5.35.'
    )


# ---------------------------------------------------------------------------------------------------
# THE TWO ARMS THAT SURVIVED
#
# Removing the Kafka arm meant editing the handler's dispatch chain, and the Kafka branch was the
# FIRST of the three. A wrong edit there does not raise -- it falls through to the `else`, which
# returns {'success': False} -- so the two surviving branches are pinned by dispatching down each one
# and checking the transport underneath actually saw the traffic. Nothing here times anything: the
# harness stays untested as to speed by the user's decision on tj-3mk3u5.8.


@pytest.fixture
def harness_on(monkeypatch: pytest.MonkeyPatch):
    """Turn the harness on with only the transports stubbed. See latency_harness.turn_harness_on."""
    turn_harness_on(monkeypatch)


@pytest.mark.asyncio
async def test_the_rest_arm_still_dispatches_to_the_rest_client(harness_on, monkeypatch: pytest.MonkeyPatch):
    """REST was the second branch and is now the first; it must still reach the REST client."""
    app = AppStub()
    latency.initialize_latency_client(app, 'probe', 1, ConsumerGroup.COMMON_GROUP)

    recorder = RecordingRestClient()
    monkeypatch.setattr(latency, '__REST_CLIENT', recorder)

    result = await rest_endpoint(app)(
        LatencyRequest(latency_type=LatencyRequest.LatencyType.REST, iterations=2, payload_size=1)
    )

    assert result['success'] is True, result
    assert result['samples'] == 2, result
    assert len(recorder.posts) == 2
    assert getattr(latency, '__GRPC_CLIENT').probes == [], 'a REST sample reached the gRPC arm'


@pytest.mark.asyncio
async def test_the_grpc_arm_still_dispatches_to_the_probe_client(harness_on, monkeypatch: pytest.MonkeyPatch):
    """The gRPC branch was the last before the Kafka one went, and is the arm most easily orphaned.

    The probe payload is checked for presence rather than content: the handler builds it from
    os.urandom, so its value is not a property of the dispatch. What is a property of the dispatch is
    that one probe happened per iteration and the REST client was never touched.
    """
    app = AppStub()
    latency.initialize_latency_client(app, 'probe', 1, ConsumerGroup.COMMON_GROUP)

    recorder = RecordingRestClient()
    monkeypatch.setattr(latency, '__REST_CLIENT', recorder)

    result = await rest_endpoint(app)(
        LatencyRequest(latency_type=LatencyRequest.LatencyType.GRPC, iterations=3, payload_size=1)
    )

    assert result['success'] is True, result
    assert result['samples'] == 3, result
    probes = getattr(latency, '__GRPC_CLIENT').probes
    assert len(probes) == 3, probes
    assert all(payload for payload in probes), 'the probe was handed an empty payload'
    assert recorder.posts == [], 'a gRPC sample went out over REST'
