from typing import Any, Callable, Coroutine, Dict, List
from common.kafka.kafka_config import RpcParams
from common.kafka.messaging.kafka_consumer import KafkaConsumerFactory
from common.kafka.rpc.kafka_rpc_base import RpcEndpoint, RpcRequest
from common.kafka.rpc.kafka_rpc_client import KafkaRpcClient
from common.kafka.rpc.kafka_rpc_server import KafkaRpcServer, Res, Req


class KafkaRpcFactory:
    def __init__(self, rpc_params: RpcParams):
        self.rpc_params = rpc_params

        self._rpc_clients: List[KafkaRpcClient] = []
        self._rpc_servers: List[KafkaRpcServer] = []

        self._clients_running = False
        self._servers_running = False

    def add_client(self, endpoint: RpcEndpoint, timeout: int = 5):
        self._rpc_clients.append(KafkaRpcClient(self.rpc_params, endpoint, timeout))

    def add_server(
        self,
        endpoint: RpcEndpoint,
        timeout: int = 5,
    ) -> Callable[[Callable[[RpcRequest], Coroutine[Any, Any, Res]]], Callable[[RpcRequest], Coroutine[Any, Any, Res]]]:
        """
        Decorator to register an RPC server with the provided endpoint and function.
        """
        def decorator(
            rpc_function: Callable[[RpcRequest], Coroutine[Any, Any, Res]]
        ) -> Callable[[RpcRequest], Coroutine[Any, Any, Res]]:
            self._rpc_servers.append(KafkaRpcServer(self.rpc_params, endpoint, rpc_function, timeout))
            return rpc_function  # Return the original function unchanged

        return decorator

    class RpcClients:
        def __init__(self):
            self._consumer_factory = KafkaConsumerFactory()
            self._rpc_clients: Dict[int, KafkaRpcClient] = {}

        @staticmethod
        def _client_key(endpoint: RpcEndpoint) -> int:
            return hash(endpoint.topic.request)

        def get_client(self, endpoint: RpcEndpoint[Req, Res]) -> KafkaRpcClient[Req, Res]:
            client_key = self._client_key(endpoint)
            if client_key not in self._rpc_clients:
                raise Exception(f"Client for {endpoint} not found")
            return self._rpc_clients[client_key]

        def shutdown(self):
            self._consumer_factory.shutdown()

    def init_clients(self) -> RpcClients:
        if self._clients_running is True:
            raise Exception("Already initialized")

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

    def init_servers(self) -> RpcServers:
        if self._servers_running is True:
            raise Exception("Already initialized")

        rpc_servers = self.RpcServers()
        rpc_servers._rpc_servers = self._rpc_servers
        for rpc_server in self._rpc_servers:
            rpc_server.initialize(rpc_servers._consumer_factory)

        self._servers_running = True
        return rpc_servers

