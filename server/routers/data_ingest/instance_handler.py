"""THE DOMAIN SIDE of the Instance probe (tj-7zjov1.21): which ingest process is answering.

Conforms STRUCTURALLY to common.rpc.ingest.InstanceHandler, by shape. The identity it reports is the one the lifespan
minted once at start, the same value every Submit answer and attempt report carries.
"""

from data.ingest.app.instance import InstanceIdentity
from schemas.data_ingest.submit import InstanceResponse


class IngestInstanceHandler:
    """Serves the Instance probe from this process's identity."""

    def __init__(self, identity: InstanceIdentity) -> None:
        """Bind the handler to the identity this process minted at start.

        Args:
            identity (InstanceIdentity): The id and the start time.
        """
        self._identity = identity

    async def instance(self) -> InstanceResponse:
        """Report this ingest process.

        Returns:
            InstanceResponse: The instance id and when it started.
        """
        return InstanceResponse(instance_id=self._identity.instance_id, started_at=self._identity.started_at)
