from typing import Any, Callable, Coroutine, Dict, List, Self
from common.kafka.kafka_config import RpcParams
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.rpc.kafka_rpc_base import RpcEndpoint, RpcRequest
from common.kafka.rpc.kafka_rpc_client import KafkaRpcClient
from common.kafka.rpc.kafka_rpc_server import KafkaRpcServer, Res, Req


class KafkaRpcFactory:
    """Factory that manages the creation and lifecycle of Kafka RPC clients and servers."""
    def __init__(self, rpc_params: RpcParams):
        """Create a new KafkaRpcFactory.

        Args:
            rpc_params (RpcParams): RPC parameters.
        """
        self.rpc_params = rpc_params

        self._rpc_clients: List[KafkaRpcClient] = []
        self._rpc_servers: List[KafkaRpcServer] = []

        self._clients_running = False
        self._servers_running = False

    def add_client(self, endpoint: RpcEndpoint, timeout: int = 5):
        """Add an RPC client to the provided endpoint definition.

        Args:
            endpoint (RpcEndpoint): RPC endpoint definition.
            timeout (int, optional): RPC request and Kakfa producer/consumer timeout. Defaults to 5.
        """
        self._rpc_clients.append(KafkaRpcClient(self.rpc_params, endpoint, timeout))

    def add_server(
        self,
        endpoint: RpcEndpoint,
        timeout: int = 5,
    ) -> Callable[[Callable[[RpcRequest], Coroutine[Any, Any, Res]]], Callable[[RpcRequest], Coroutine[Any, Any, Res]]]:
        """Add an RPC server to the provided endpoint definition.

        Args:
            endpoint (RpcEndpoint): RPC endpoint definition.
            timeout (int, optional): Kakfa producer/consumer timeout. Defaults to 5.

        Returns:
            Callable[[Callable[[RpcRequest], Coroutine[Any, Any, Res]]], Callable[[RpcRequest], Coroutine[Any, Any, Res]]]: Decorator function.
        """  # noqa: E501
        def decorator(
            rpc_function: Callable[[RpcRequest], Coroutine[Any, Any, Res]]
        ) -> Callable[[RpcRequest], Coroutine[Any, Any, Res]]:
            self._rpc_servers.append(KafkaRpcServer(self.rpc_params, endpoint, rpc_function, timeout))
            return rpc_function  # Return the original function unchanged

        return decorator

    class RpcClients:
        """Handle for Kafka RPC clients."""
        def __init__(self):
            self._consumer_factory = KafkaConsumerFactory()
            self._rpc_clients: Dict[int, KafkaRpcClient] = {}

        @staticmethod
        def _client_key(endpoint: RpcEndpoint) -> int:
            """Generate a unique key for the RPC client.

            Args:
                endpoint (RpcEndpoint): RPC endpoint definition.

            Returns:
                int: Unique key.
            """
            return hash(endpoint.topic.request)

        def get_client(self, endpoint: RpcEndpoint[Req, Res]) -> KafkaRpcClient[Req, Res]:
            """Get the RPC client for the provided endpoint.

            Args:
                endpoint (RpcEndpoint[Req, Res]): RPC endpoint definition.

            Raises:
                ValueError: Client for the provided endpoint not found.

            Returns:
                KafkaRpcClient[Req, Res]: RPC client.
            """
            client_key = self._client_key(endpoint)
            if client_key not in self._rpc_clients:
                raise ValueError(f"Client for {endpoint} not found")
            return self._rpc_clients[client_key]

        def shutdown(self):
            """Shutdown all RPC clients."""
            self._consumer_factory.shutdown()

    def init_clients(self) -> Self:
        """Initialize all RPC clients.

        Raises:
            RuntimeError: Already initialized.

        Returns:
            Self: RPC clients Handle.
        """
        if self._clients_running is True:
            raise RuntimeError("Already initialized")

        clients = self.RpcClients()
        for rpc_client in self._rpc_clients:
            clients._rpc_clients[clients._client_key(rpc_client.endpoint)] = rpc_client
            rpc_client.initialize(clients._consumer_factory)

        self._clients_running = True
        return clients

    class RpcServers:
        def __init__(self):
            self._consumer_factory = KafkaConsumerFactory()
            self._rpc_servers: List[KafkaRpcServer] = []

        def shutdown(self):
            self._consumer_factory.shutdown()

    def init_servers(self) -> Self:
        """Initialize all RPC servers.

        Raises:
            RuntimeError: Already initialized.

        Returns:
            Self: RPC servers Handle.
        """
        if self._servers_running is True:
            raise RuntimeError("Already initialized")

        rpc_servers = self.RpcServers()
        rpc_servers._rpc_servers = self._rpc_servers
        for rpc_server in self._rpc_servers:
            rpc_server.initialize(rpc_servers._consumer_factory)

        self._servers_running = True
        return rpc_servers

