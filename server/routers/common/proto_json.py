"""Protobuf canonical JSON at the HTTP and WebSocket edge, for the /ui/v1 routes (ADR tj-grna9p.4 section 4).

The UI's payloads are the protobuf canonical JSON of generated messages (ADR tj-4k5s35): lowerCamelCase names,
enums by name, int64 as a string, Timestamp as RFC 3339 'Z'. google.protobuf.json_format renders and parses it,
so no second representation of a message exists in Python. This module is the one place that calls json_format
for the wire.

    ProtoJSONResponse    a handler returns one: the message rendered byte-for-byte as MessageToJson, served as
                         application/json.
    ProtoBody            a dependency: the request body parsed into a message of the type given. A body that is
                         not canonical JSON of that type, an unknown field included, is a 422 problem+json.
    proto_route          the route keywords that put ProtoJSONResponse and x-proto-message on a route.
    send_proto_json      the same rendering, sent as one WebSocket text frame.

OpenAPI carries no field schema for these routes: the .proto is the schema. What the document records is the
message's full name under the x-proto-message extension, so the interface manifest pins the contract.

Nothing here imports trader_joe.proto (banned outside common/rpc). The helpers take any protobuf Message, and a
route names its message type from wherever the mapping code in common/rpc hands it out.

A parse failure never echoes the body or the parser's text, which can quote a value (D8): the detail names the
message type only, and the unknown or malformed part is for the client to find in the .proto.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any, Final, TypeVar

from fastapi import Request, WebSocket
from fastapi.responses import Response
from google.protobuf import json_format
from google.protobuf.message import Message

from common.errors.vocabulary import InvalidRequestError, Reason


MessageT = TypeVar('MessageT', bound=Message)

# The OpenAPI extension that names the message a route serves, by its full proto name.
X_PROTO_MESSAGE: Final = 'x-proto-message'


def to_proto_json(message: Message) -> str:
    """Render a message as protobuf canonical JSON, exactly as json_format.MessageToJson does.

    Args:
        message: The message to render.

    Returns:
        str: The canonical JSON text.
    """
    return json_format.MessageToJson(message)


class ProtoJSONResponse(Response):
    """A response whose content is a protobuf message, rendered as canonical JSON (application/json).

    The body is byte-for-byte MessageToJson of the message, encoded as UTF-8. A handler returns one directly:
    FastAPI sends a Response as it is, so nothing converts the message to a Pydantic model first. A handler
    that returns the bare message instead of ProtoJSONResponse(msg) is a 500: FastAPI runs jsonable_encoder
    on it, which cannot encode a protobuf message.
    """

    media_type = 'application/json'

    def render(self, content: Any) -> bytes:
        """Render the message as UTF-8 canonical JSON.

        Args:
            content: The message to render.

        Returns:
            bytes: The canonical JSON, encoded.

        Raises:
            TypeError: If content is not a protobuf message.
        """
        if not isinstance(content, Message):
            raise TypeError(f'ProtoJSONResponse renders a protobuf Message, not {type(content).__name__}')
        return to_proto_json(content).encode('utf-8')


def ProtoBody(message_type: type[MessageT]) -> Callable[[Request], Awaitable[MessageT]]:
    """Build a FastAPI dependency that parses the request body into a message of message_type.

    Use it as Annotated[Msg, Depends(ProtoBody(Msg))]. The body is read as UTF-8 and parsed with
    json_format.Parse, unknown fields refused. The body must be a JSON object. A body that is not, or does not
    parse, raises InvalidRequestError
    (INVALID_REQUEST), which the app's problem+json handlers render as a 422. The detail names the message
    type only, never the body.

    Args:
        message_type: The generated message class the body must be canonical JSON of.

    Returns:
        Callable[[Request], Awaitable[MessageT]]: The dependency.
    """
    full_name = message_type.DESCRIPTOR.full_name

    async def parse_body(request: Request) -> MessageT:
        raw = await request.body()
        try:
            text = raw.decode('utf-8')
            # Parse does not require an object: an empty array or empty string reads as an empty message (a 200
            # on an empty body), and a non-empty array or string is refused only by ignore_unknown_fields=False.
            # So the object check is ours, and every non-object body takes the same refusal (protobuf 6.33.6).
            if not isinstance(json.loads(text), dict):
                raise ValueError('not a JSON object')
            return json_format.Parse(text, message_type(), ignore_unknown_fields=False)
        except (json_format.ParseError, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            # from None: neither exception's text is allowed to reach a log line or a wire (D8).
            raise InvalidRequestError(
                Reason.INVALID_REQUEST, f'The request body is not valid protobuf JSON for {full_name}.'
            ) from None

    return parse_body


def proto_route(message: type[Message]) -> dict[str, Any]:
    """Return the route keywords for a route that serves message: its response class and x-proto-message.

    Use it as @router.get(path, **proto_route(Msg)) and return ProtoJSONResponse(msg) from the handler. Returning
    the bare message is a 500 (FastAPI runs jsonable_encoder on it), so always wrap it.

    Args:
        message: The generated message class the route answers with.

    Returns:
        dict[str, Any]: response_class and openapi_extra, for the route decorator.
    """
    return {'response_class': ProtoJSONResponse, 'openapi_extra': {X_PROTO_MESSAGE: message.DESCRIPTOR.full_name}}


async def send_proto_json(websocket: WebSocket, message: Message) -> None:
    """Send a message as one WebSocket text frame of canonical JSON, the text ProtoJSONResponse would carry.

    Args:
        websocket: The accepted socket to send on.
        message: The message to render and send.
    """
    await websocket.send_text(to_proto_json(message))
