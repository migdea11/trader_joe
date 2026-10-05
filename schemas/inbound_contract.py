"""The base every contract a service RECEIVES is built on.

UNKNOWN FIELDS ARE REJECTED, NOT IGNORED (tj-vhboky.1, user ruling of 2026-09-25). Pydantic's
default is to drop a field it does not recognise and carry on, which makes a removed field
indistinguishable from a field that was never sent.

THE REASON IS NOT TIDINESS, and the user was explicit that the internal case is the worse one:
"an external caller sending junk is sloppy, but an internal service still sending a field we
deleted means we removed something and nothing told us the sender had not noticed. That is a bug
shipped to ourselves." The live instance when this landed: the Alpaca adapter was still sending
split_factor and dividends_factor on every bar after the contract dropped them, and every call
succeeded because extra=ignore swallowed both.

>>> THE CONSEQUENCE, AND THE ONE THING TO REMEMBER BEFORE A RELEASE <<<
A strict receiver creates a DEPLOY-ORDERING CONSTRAINT. Once a receiver rejects unknown fields, a
sender that starts sending a NEW field before the receiver knows about it is rejected outright.
So for any change that adds a field to a contract, THE RECEIVING SIDE DEPLOYS FIRST. The order is
the price of the guarantee, not a defect in it: the same strictness that catches a field we
deleted also catches a field we have not added yet.

Removing a field is the mirror image and does not reverse the rule: the SENDER stops sending it
first, then the receiver drops it.
"""

from pydantic import BaseModel, ConfigDict


class InboundContract(BaseModel):
    """A model a service accepts from somewhere else, over HTTP or over gRPC.

    Inheriting this is the statement "this shape arrives from another process". Response and
    read models do not need it -- they are what we send, and we are already the authority on
    their shape -- though they inherit it harmlessly where they extend a request model.
    """

    model_config = ConfigDict(extra='forbid')
