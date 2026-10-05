"""Keep the lifespan's Kafka startup off the network, for as long as there still is one.

TRANSITIONAL, AND DELIBERATELY TOLERANT (tj-3mk3u5.32). Kafka is still wired into data_ingest's
lifespan at this commit: app_depends waits for a broker and starts the Kafka RPC servers. A test that
enters the real lifespan must therefore still keep both off the network -- but it must NOT go red
when tj-3mk3u5.11 unwires them and tj-3mk3u5.14 deletes common/kafka, because a builder deleting
production code never edits a test (ADR tj-8fxxfb, and the reason this bead runs ahead of the
deletions).

A hard ``patch.object(KafkaConsumerFactory, ...)`` cannot satisfy both: the import dies at .14 and
the ``get_dataset_request.rpc`` attribute dies at .11. So each stub below is applied only if its
target is still there, and the whole module becomes a no-op -- then dead code to delete -- once the
wiring is gone.

WHAT THIS IS NOT. It stubs; it asserts nothing. No test's expectations rest on anything here, which
is the point: the ASSERTIONS about the Kafka transport were retired on tj-3mk3u5.32, and what is left
behind is only the muzzle that stops a still-wired broker client reaching the network. When this
module stops stubbing anything, the tests that call it must still pass unchanged -- that is the
property that lets the deletions be self-consistent, and it is what tj-iwiq23 verifies before
deleting it.
"""

from contextlib import ExitStack
from unittest.mock import Mock, patch

from routers.data_ingest import get_dataset_request


def stub_kafka_startup(stack: ExitStack) -> None:
    """Stub whatever of data_ingest's Kafka startup still exists, under the given stack.

    Two entry points, each skipped once it is gone:

    * ``KafkaConsumerFactory.wait_for_kafka`` -- the startup block's wait for a live broker, which
      would otherwise retry against a broker no test environment has (tj-3mk3u5.11 removes the call,
      tj-3mk3u5.14 the class);
    * the ingest RPC factory's ``init_servers`` -- which would otherwise start real consumer threads
      (tj-3mk3u5.11 removes the factory from the router).

    The ``init_servers`` stand-in returns a Mock, so the lifespan's ``rpc_servers.shutdown()`` in its
    finally is satisfied without the test knowing or caring that it was called. Nothing records it:
    the Kafka RPC server's lifecycle is no longer pinned anywhere (tj-3mk3u5.32).

    Args:
        stack (ExitStack): The stack the stubs are entered on, and unwound with.
    """
    try:
        from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
    except ModuleNotFoundError:
        pass
    else:
        stack.enter_context(patch.object(KafkaConsumerFactory, 'wait_for_kafka', return_value=True))

    rpc = getattr(get_dataset_request, 'rpc', None)
    if rpc is not None:
        stack.enter_context(patch.object(rpc, 'init_servers', return_value=Mock()))
