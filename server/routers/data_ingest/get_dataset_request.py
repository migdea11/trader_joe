"""data_ingest's HTTP router, now empty: the dataset request is served over gRPC, not here.

Until tj-3mk3u5.11 this module also built a KafkaRpcFactory at import and decorated store_data with
@rpc.add_server(InterfaceRpc.INGEST_DATASET), which dispatched into data/ingest/app/ingest_control.py.
Both are gone with the Kafka transport. Dataset requests arrive over gRPC Submit (routers/data_ingest/submit_handler.py), registered in
data/ingest/app/grpc_host.py::registered_services; the FetchDataset pull stream is gone (owner ruling 2026-11-02).

The router itself stays because main.create_app mounts it, and because an HTTP route added to
routers/data_ingest later belongs here -- which is also what the `none` declaration in
routers/tests/interface_manifest/data_ingest.manifest keeps guarded (decision tj-3wgh03).
"""

from fastapi import APIRouter


router = APIRouter()
