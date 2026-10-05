"""data_ingest's HTTP router, now empty: the dataset request is served over gRPC, not here.

Until tj-3mk3u5.11 this module also built a KafkaRpcFactory at import and decorated store_data with
@rpc.add_server(InterfaceRpc.INGEST_DATASET), which dispatched into data/ingest/app/ingest_control.py.
Both are gone with the Kafka transport. The dataset request is answered by the gRPC IngestService,
whose domain handler is routers/data_ingest/fetch_dataset_handler.py::IngestFetchHandler and whose
registration is data/ingest/app/grpc_host.py::registered_services.

The router itself stays because main.create_app mounts it, and because an HTTP route added to
routers/data_ingest later belongs here -- which is also what the `none` declaration in
routers/tests/interface_manifest/data_ingest.manifest keeps guarded (decision tj-3wgh03).
"""

from fastapi import APIRouter


router = APIRouter()
