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
explain what was removed; a text scan would call those drift and force them out. Imports, names and
attributes are the references that would actually wire the arm back up, so those are what is checked.

WHAT THE SCAN IS STILL THE GUARD FOR, NARROWED HONESTLY ON tj-iwiq23, because common/kafka no longer
exists and that changes what can reach this test at all. Measured, not reasoned about:

* a Kafka IMPORT in a module some test imports      -> ModuleNotFoundError at collection. The
                                                       interpreter gets there first; this scan never
                                                       runs. That is a stronger guard, not a weaker
                                                       one, and it needs no test to hold it.
* a bare Kafka NAME in such a module                -> NameError at import. Same story.
* a Kafka import in a module NO test here imports   -> THIS SCAN, and nothing else in this scope.
                                                       routers/common/ping.py is exactly that module
                                                       today: the routers suite never imports it, so
                                                       a Kafka import there reaches collection
                                                       intact. Verified by mutation.

So the scan has gone from holding the whole surface to holding the part the interpreter cannot, and
that part is real but small. Do not read a green run here as proof the trees are Kafka-free; read it
together with the fact that the suite imports six of the seven modules in them.

ONE KNOWN BLIND SPOT, recorded rather than fixed: _kafka_references walks Import, ImportFrom, Name
and Attribute, so a Kafka-layer name RE-DECLARED locally -- `def get_rpc_params():` -- is matched by
none of them and passes. Fixing it means walking FunctionDef and ClassDef too. It is left because
the shape it would catch is someone reimplementing the Kafka layer under its old names, which the
deletion of the package makes a deliberate act rather than an accident.
"""

import ast
from pathlib import Path
from typing import Final

import pytest

import routers.common.latency as latency
from common.tests.roots import SERVER_ROOT
from routers.tests.latency_harness import AppStub, RecordingRestClient, rest_endpoint, turn_harness_on
from schemas.common.latency import LatencyRequest


pytestmark = pytest.mark.common

# THE SERVER ROOT (tj-iontkq.2): CLEARED_TREES below names routers/common and schemas/common, both
# trees that travel with the services, and the scan reports its hits relative to the same root.
# SERVER_ROOT, never REPO_ROOT -- which would make both rglob walks find no files at all and leave
# this guard green having scanned nothing.

# The two scopes tj-3mk3u5.35 cleared. data/ and common/ are NOT here, and the reason outlived the
# deletions: these two trees were cleared of Kafka FIRST, while the rest of the repo still ran on
# it, so a reference reappearing here was the regression worth tripping on. common/kafka is gone
# outright since tj-3mk3u5.14, which makes a stray import there an ImportError rather than a
# silent rewiring -- so the narrow scan is still the right shape and still the only thing that
# would catch a reference re-entering these two trees from a merge or a revert.
CLEARED_TREES: Final = ('routers/common', 'schemas/common')

# Exact identifiers that belong to the Kafka RPC layer and carry no 'kafka' in their own spelling.
# LATENCY_TEST is the topic, and is matched exactly so that the env-var names LATENCY_TEST_ENABLED and
# LATENCY_TEST_TIMEOUT -- which are the harness's own and stay -- are not caught by it.
KAFKA_LAYER_NAMES: Final = frozenset(
    {'RpcEndpointTopic', 'LATENCY_TEST', 'BaseRpcAck', 'RpcEndpoint', 'get_rpc_params'}
)


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
        for source in sorted((SERVER_ROOT / tree).rglob('*.py')):
            references[source.relative_to(SERVER_ROOT).as_posix()] = _kafka_references(source)
    return references


def test_no_kafka_reference_survives_in_routers_common_or_schemas_common():
    """The arm is gone from both scopes tj-3mk3u5.35 cleared, and stayed gone as .11 through .14 landed.

    NO ALLOWANCES, since tj-iwiq23. Two entries excused known residue while the deletions were in
    flight -- a ConsumerGroup import typing two dead parameters, and BaseRpcAck as LatencyResponse's
    base. tj-3mk3u5.13 and .14 removed both, so the scan now reds on ANY Kafka reference in these
    trees with nothing to argue about. That is the strongest form this test has ever had.
    """
    references = _scan()

    offenders = [
        f'{relative}:{line}: {detail}'
        for relative, found in sorted(references.items())
        for _token, line, detail in found
    ]

    assert references, f'the scan found no modules at all under {CLEARED_TREES}; it is proving nothing'
    assert offenders == [], (
        'the latency harness lost its Kafka arm on tj-3mk3u5.35 and these scopes were cleared with '
        'it. A reference is back:\n' + '\n'.join(offenders)
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
    latency.initialize_latency_client(app, 'probe', 1)

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
    latency.initialize_latency_client(app, 'probe', 1)

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
