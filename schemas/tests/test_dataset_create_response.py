"""The declared dataset-create response, and the served_range the user ruled is exposed in PR 2.

WHY THIS FILE EXISTS (validator, gating tj-3mk3u5.37.12). POST
/store/{asset_type}/{data_type}/{asset_symbol} returned an UNDECLARED dict, so the interface
manifest recorded its response as '-' and a member added to that dict would be absent from OpenAPI
and from every client generated out of it. The user ruled on 2026-10-02 that served_range is
exposed there in PR 2, so a SERVED-BUT-EMPTY answer -- a misspelled symbol among them (tj-lldllr)
-- reaches the caller rather than only an INFO line in our logs. ADR tj-fa1rpu D2 and its
2026-10-02 addendum are the design; this file is what makes the declaration a tested claim.

WHAT A SCHEMA TEST CAN AND CANNOT SAY, stated because the easy version of this file proves nothing.
These models have NO CALLER YET: TE-6 (tj-3mk3u5.37.8) makes the route return the model, and until
it lands the route still returns the dict. So nothing here renders the live FastAPI document, and
no assertion below should be read as having done so -- what is checked is the JSON Schema the model
itself produces, which is the input FastAPI composes that document from. The route actually
answering with this shape, and the manifest's response field ceasing to be '-', belong to TE-6's
gate.

EVERY CASE HERE WAS MUTATED, and the mutation log is on the bead. A required-bound test that would
pass against a model with both bounds optional, or a date-time test that would pass against a plain
``datetime``, is the specific way a schema test comes to assert nothing.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from schemas.data_ingest import fetch_dataset
from schemas.data_store.asset_dataset_store import ServedRange, StoreAssetDatasetResponse


pytestmark = pytest.mark.data_store

# The served window, as a vendor would have answered for it. Deliberately NARROWER than a plausible
# request: the whole point of D2 is that the answer carries its own range, so a fixture whose bounds
# matched a request range would make "served" and "requested" indistinguishable in every failure
# message here.
SERVED_START = datetime(2026, 1, 5, tzinfo=UTC)
SERVED_END = datetime(2026, 1, 9, tzinfo=UTC)

# The two keys the route answers with today (routers/data_store/asset_dataset_store.py:66 returns
# {'message': 'Data stored', 'data_points': n}). Written out rather than imported: importing the
# route would make this schema test depend on routers/, and the claim is precisely that the declared
# model reproduces those keys without having been derived from them.
TODAYS_KEYS = {'message': 'Data stored', 'data_points': 0}


def _response_payload() -> dict:
    """A minimal valid payload for the response model.

    Returns:
        dict: Every required field and nothing else.
    """
    return TODAYS_KEYS | {'served_range': {'start': SERVED_START, 'end': SERVED_END}}


# ---------------------------------------------------------------------------------------------
# Both bounds required, and served_range required on the response
# ---------------------------------------------------------------------------------------------


def test_served_range_requires_both_of_its_bounds():
    """BOTH REQUIRED, asserted as the exact missing set rather than as "it raised".

    THE MUTATION THIS EXISTS FOR is ``end: AwareDatetime | None = None``, which is the plausible
    one: ``StoreAssetDatasetBody.end`` IS optional, and a reader copying the request's shape would
    make the answer's optional to match. It must not be. An open request end means "up to whatever
    is current" and the fetch CLAMPS it -- end = min(requested end, as_of), an open end served as
    as_of -- so "open" is a property of the REQUEST only and the answer always names an instant. A
    nullable end would let a client receive ``null`` and have nothing to compare its request
    against, which is the one thing served_range was exposed to prevent.

    Asserting the set, not the count: a model that required only ``start`` raises one ``missing``
    too.
    """
    with pytest.raises(ValidationError) as excinfo:
        ServedRange()
    missing = {str(error['loc'][0]) for error in excinfo.value.errors() if error['type'] == 'missing'}
    assert missing == {'start', 'end'}, (
        f'ServedRange reports {missing} as missing from an empty payload. Both bounds are required: '
        f'the served window always names two instants, however open the request was.'
    )


def test_the_response_requires_all_three_members_including_served_range():
    """served_range is not an optional extra, and that is the whole deliverable.

    An optional served_range would satisfy "the member exists in OpenAPI" while letting the route
    omit it -- so a served-but-empty answer would again be invisible to the client, which is exactly
    the gap tj-lldllr named and the 2026-10-02 ruling closed. The two older keys are asserted
    required in the same breath because the response is additive: today's callers already rely on
    both, and making either optional would be a silent narrowing of a contract this bead is only
    supposed to extend.
    """
    with pytest.raises(ValidationError) as excinfo:
        StoreAssetDatasetResponse()
    missing = {str(error['loc'][0]) for error in excinfo.value.errors() if error['type'] == 'missing'}
    assert missing == {'message', 'data_points', 'served_range'}, (
        f'StoreAssetDatasetResponse reports {missing} as missing. All three are required: an '
        f'optional served_range leaves a served-but-empty answer invisible to the client again.'
    )


def test_the_response_constructs_and_carries_todays_two_keys_unchanged():
    """The additive half: one member added, nothing renamed and nothing dropped.

    The serialised key set is asserted EXACTLY. ``data_points`` renamed to ``dataPoints`` or
    ``count`` -- the kind of tidy-up a new model invites, since nothing else in the package uses
    snake_case on the wire by accident -- is a breaking change to every existing caller, and no
    other assertion in this file or in the smoke file would see it.
    """
    response = StoreAssetDatasetResponse(**_response_payload())

    assert set(response.model_dump().keys()) == {'message', 'data_points', 'served_range'}, (
        'the response no longer carries exactly the two keys the route answers with today plus '
        'served_range; the only wire change this bead authorises is ONE ADDED MEMBER'
    )
    assert response.message == TODAYS_KEYS['message']
    assert response.data_points == TODAYS_KEYS['data_points']
    assert response.served_range.start == SERVED_START
    assert response.served_range.end == SERVED_END


def test_no_as_of_is_exposed():
    """USER RULING, 2026-10-02 04:54 UTC: served_range only; a client queries the data for the time.

    Pinned by name rather than left to the exact-key-set assertion above, because the two fail for
    different reasons and a reader of a failure deserves the ruling rather than a set difference.
    ``as_of`` is on the INTERNAL FetchDone and is the obvious thing to forward when wiring TE-6;
    this is what says it was considered and declined.
    """
    assert 'as_of' not in StoreAssetDatasetResponse.model_fields, (
        'as_of is exposed on the public response. The user ruled on 2026-10-02 at 04:54 UTC that it '
        'is not: served_range only, and a client that wants the vendor answer time queries the data.'
    )
    assert 'as_of' not in ServedRange.model_fields, 'as_of was added to the served range rather than to the response'


# ---------------------------------------------------------------------------------------------
# The bounds are instants, not strings with a zone attached by guesswork
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('bound', ['start', 'end'])
def test_a_naive_bound_is_refused_rather_than_assumed_utc(bound: str):
    """The tj-1bl90i rule, on the response's bounds: REFUSE, never convert.

    THE MUTATION THIS EXISTS FOR is ``AwareDatetime`` -> ``datetime``, which is invisible to every
    other case in this file: a naive value constructs, serialises and round-trips perfectly well,
    and only loses when someone compares it with the aware bound of their own request and gets a
    TypeError -- or worse, compares it against a naive one of their own and gets an answer that
    depends on where the two processes were running. The error list is asserted exactly so that a
    CONVERT implementation (an after-validator attaching UTC) reds this rather than slipping through
    as "no error here, but one somewhere".

    Args:
        bound: The bound sent without a zone.
    """
    payload = {'start': SERVED_START, 'end': SERVED_END} | {bound: datetime(2026, 1, 7)}
    with pytest.raises(ValidationError) as excinfo:
        ServedRange(**payload)
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((bound,), 'timezone_aware')]


@pytest.mark.parametrize('bound', ['start', 'end'])
def test_an_offset_less_json_string_bound_is_refused(bound: str):
    """The wire form of the case above, which is the form a bound actually arrives in.

    A client never hands this model a Python datetime; something parses JSON. A string-side
    coercion could refuse the object form above while accepting this one, so both are pinned.

    Args:
        bound: The bound sent as offset-less text.
    """
    text = '2026-01-07T00:00:00'
    assert '+' not in text and not text.endswith('Z'), 'the fixture must carry no offset'
    body = {'start': SERVED_START.isoformat(), 'end': SERVED_END.isoformat()} | {bound: text}

    with pytest.raises(ValidationError) as excinfo:
        ServedRange.model_validate_json(json.dumps(body))
    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((bound,), 'timezone_aware')]


@pytest.mark.parametrize(
    ('suffix', 'zone'),
    [('Z', UTC), ('+00:00', UTC), ('-05:00', timezone(timedelta(hours=-5)))],
    ids=['zulu', 'utc-offset', 'nonzero-offset'],
)
def test_an_offset_bearing_bound_round_trips_as_the_instant_it_names(suffix: str, zone: timezone):
    """The success half, and the instant rather than the text.

    A refusal test alone is satisfied by a field that refuses everything, so the accepting case is
    what keeps the two honest together. The non-zero offset is the one that separates HONOURING the
    offset from dropping it: were ``-05:00`` read as UTC the value would compare five hours early,
    and the model's own docstring tells a client to "COMPARE THESE AS INSTANTS, NEVER AS STRINGS" --
    which is only safe advice if the parse is right.

    The round trip is through JSON in both directions, because that is the only path a real client
    takes and it is where a tzinfo is most easily lost.

    Args:
        suffix: The offset designator appended to the ISO text.
        zone: The zone that designator names.
    """
    text = '2026-01-07T09:30:00' + suffix
    expected = datetime(2026, 1, 7, 9, 30, tzinfo=zone)

    served = ServedRange.model_validate_json(json.dumps({'start': text, 'end': SERVED_END.isoformat()}))
    assert served.start == expected, f'{text} was not read as the instant it names'

    reparsed = ServedRange.model_validate_json(served.model_dump_json())
    assert reparsed.start == expected, 'the instant did not survive a JSON round trip'
    assert reparsed.start.utcoffset() is not None, 'the round trip dropped the offset and left a naive value'


# ---------------------------------------------------------------------------------------------
# What a generated client is actually built from
# ---------------------------------------------------------------------------------------------


def test_the_json_schema_documents_both_bounds_as_required_date_times():
    """The OpenAPI half: this is the input FastAPI composes the document from.

    IT IS NOT THE LIVE DOCUMENT, and must not be read as one -- the route still returns an
    undeclared dict until TE-6 (tj-3mk3u5.37.8) lands, so there is no document to render in which
    this model appears. What is proved here is that WHEN the route declares it, a client generated
    from the result gets served_range as a required object with two required date-time bounds. The
    route actually declaring it is TE-6's gate, not this one.

    ``format: date-time`` is asserted because it is what makes a generated client parse the value
    into its language's instant type instead of leaving it a string -- the difference between a
    caller comparing served_range with its request and a caller doing string arithmetic.
    """
    schema = StoreAssetDatasetResponse.model_json_schema()

    assert 'served_range' in schema['required'], (
        f'served_range is not required on the response schema, so a generated client would make it '
        f'optional and a caller could not rely on it: required={schema["required"]}'
    )

    nested = schema['$defs']['ServedRange']
    assert sorted(nested['required']) == ['end', 'start'], (
        f'the served range schema does not require both bounds: required={nested["required"]}'
    )
    for bound in ('start', 'end'):
        assert nested['properties'][bound]['format'] == 'date-time', (
            f'served_range.{bound} is documented as {nested["properties"][bound]} rather than as a '
            f'date-time, so a generated client would hand its caller a bare string'
        )


# ---------------------------------------------------------------------------------------------
# The duplicated ServedRange: two declarations, one meaning
# ---------------------------------------------------------------------------------------------


def test_the_public_and_internal_served_range_agree_field_for_field():
    """TWO DECLARATIONS OF ServedRange EXIST, AND NOTHING ELSE NOTICES IF THEY DIVERGE.

    schemas/data_ingest/fetch_dataset.py declares one for the INTERNAL gRPC hop (tj-3mk3u5.27) and
    schemas/data_store/asset_dataset_store.py declares another for the PUBLIC HTTP response. The
    separation is deliberate and correct: a re-export would let a change to the internal hop move
    the public contract silently, which is what the bead's item 3 rules out in so many words.

    BUT THE SAME BEAD SAYS "Read .27's model anyway, so the bound semantics agree" -- so they are
    MEANT to agree, and until this case existed that agreement rested on a sentence in a commit
    message. data_store copies the bounds UNCHANGED from FetchDone, so a divergence is not cosmetic:
    an internal bound that became optional, gained a sibling, or was renamed would break the copy or
    silently stop being represented in the public answer, and the suite would say nothing. That is
    the shape the architect found with FetchEvent earlier in this epic, where "these two mirror each
    other" turned out to rest on nothing.

    WHAT IS COMPARED IS NAME, ANNOTATION AND REQUIRED-NESS -- not the class, and not the config. The
    base class difference is the INTENDED one: the internal model is an InboundContract because it
    arrives from another process, and the public one is what we send. Comparing configs here would
    pin the very thing the separation exists to allow.

    IF THIS REDS, the answer is a decision and not an edit: either the public model follows the
    internal one, or the two have genuinely parted and this case should be replaced by something
    recording why.
    """
    internal = fetch_dataset.ServedRange
    public = ServedRange

    def shape(model: type[BaseModel]) -> dict:
        return {name: (field.annotation, field.is_required()) for name, field in model.model_fields.items()}

    assert shape(public) == shape(internal), (
        f'the public and internal ServedRange have diverged.\n'
        f'  public   (schemas/data_store/asset_dataset_store.py): {shape(public)}\n'
        f'  internal (schemas/data_ingest/fetch_dataset.py):      {shape(internal)}\n'
        f'data_store copies these bounds unchanged off FetchDone, so a divergence either breaks that '
        f'copy or silently drops a member from the public answer. Decide which model is right rather '
        f'than editing this assertion.'
    )
