from schemas.store_broker_data import DataRequest

# TODO send request to data_ingest -> Kafka/data preprocessing -> data_store -> Redis (status)
def store_data_request(request: DataRequest):
    pass