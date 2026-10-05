from enum import Enum


class InterfaceRest(str, Enum):
    PING = '/ping'
    LATENCY = '/latency/{latency_type}'
    INTERNAL_LATENCY = '/latency_internal'
