"""OpaqueStr: layer M3's str subclass (tj-7zjov1.70; design tj-7zjov1.67 ARCHITECT DESIGN, rulings tj-7zjov1.69).

The contract, per the design's measured table:
- MARKER: every rendering -- str, repr, format (with any spec), f-strings with any conversion,
  str.format, percent-format, string.Template, print, logging arguments (also inside containers)
  and an exception raised with the value as its argument.
- VALUE: reveal(), encode, equality, hash, len, json.dumps, pickle at protocol 2 and up, copy and
  deepcopy (R4 A: left as str's).
- PLAIN STR OUT: concatenation, slicing, join and str methods return an exact str carrying the
  value. That is the documented hazard, pinned so a change is noticed, not a guarantee.
- SILENT CORRUPTION: str(x) as a conversion, RedactedStr(x) and pickle at protocol 0 or 1 give the
  marker (documented hazard; the pickle case per ADR tj-vhboky.41 ADDENDUM 3).
- Not a RedactedStr, and RedactedStr unchanged.

Every "absent" assertion checks the raw value; the fixture first proves the value and the marker
are disjoint, or those assertions would be vacuous. Made-up values only.
The bind guard in SensitiveString (R3 A) is pinned in database/test_sql_alchemy_sensitive_string.py.
"""

import copy
import json
import logging
import operator
import pickle
import string
import traceback

import pydantic
import pytest

from common.sensitive import REDACTED, OpaqueStr, RedactedStr


pytestmark = pytest.mark.common

PLAIN = 'key-4e81-made-up-not-the-marker'
LOGGER = 'test.common.opaque_str'


@pytest.fixture
def opaque() -> OpaqueStr:
    value = OpaqueStr(PLAIN)
    assert PLAIN not in REDACTED and REDACTED not in PLAIN
    return value


def _marker_only(rendered: str) -> None:
    assert REDACTED in rendered, rendered
    assert PLAIN not in rendered, rendered


# --- shape ----------------------------------------------------------------------------------


def test_it_is_a_direct_str_subclass_with_no_instance_dict(opaque: OpaqueStr) -> None:
    assert isinstance(opaque, str)
    assert OpaqueStr.__mro__ == (OpaqueStr, str, object)
    with pytest.raises(AttributeError):
        opaque.extra = 'x'  # type: ignore[attr-defined]


def test_it_is_not_a_redacted_str(opaque: OpaqueStr) -> None:
    assert not isinstance(opaque, RedactedStr)
    assert not issubclass(OpaqueStr, RedactedStr)
    assert not issubclass(RedactedStr, OpaqueStr)


def test_redacted_str_is_unchanged() -> None:
    """RedactedStr's contract is that only repr() is the marker; OpaqueStr must not have changed that."""
    redacted = RedactedStr(PLAIN)
    assert str(redacted) == PLAIN
    assert f'{redacted}' == PLAIN
    assert format(redacted, '') == PLAIN
    assert '%s' % (redacted,) == PLAIN  # noqa: UP031
    assert repr(redacted) == REDACTED
    assert RedactedStr.__str__ is str.__str__
    assert RedactedStr.__format__ is str.__format__
    assert RedactedStr.__new__ is str.__new__


# --- MARKER: every rendering ----------------------------------------------------------------


@pytest.mark.parametrize(
    'render',
    [
        pytest.param(str, id='str'),
        pytest.param(repr, id='repr'),
        pytest.param(ascii, id='ascii'),
        pytest.param(format, id='format-no-spec'),
        pytest.param(lambda v: format(v, ''), id='format-empty-spec'),
        pytest.param(lambda v: f'{v}', id='f-string'),
        pytest.param(lambda v: f'{v!s}', id='f-string-s'),
        pytest.param(lambda v: f'{v!r}', id='f-string-r'),
        pytest.param(lambda v: f'{v!a}', id='f-string-a'),
        pytest.param(lambda v: f'{v=}', id='f-string-debug'),
        pytest.param(lambda v: '{}'.format(v), id='str.format'),  # noqa: UP032
        pytest.param(lambda v: '{0!r} {0!s} {0!a}'.format(v), id='str.format-conversions'),  # noqa: UP032
        pytest.param(lambda v: '{k}'.format_map({'k': v}), id='format_map'),
        pytest.param(lambda v: '%s' % v, id='percent-s-bare'),  # noqa: UP031
        pytest.param(lambda v: '%s' % (v,), id='percent-s'),  # noqa: UP031
        pytest.param(lambda v: '%r' % (v,), id='percent-r'),  # noqa: UP031
        pytest.param(lambda v: '%a' % (v,), id='percent-a'),  # noqa: UP031
        pytest.param(lambda v: '%(k)s' % {'k': v}, id='percent-mapping'),  # noqa: UP031
        pytest.param(lambda v: string.Template('$v').substitute(v=v), id='template'),
        pytest.param(lambda v: string.Template('$v').safe_substitute(v=v), id='template-safe'),
        pytest.param(lambda v: str({'k': v}), id='inside-dict'),
        pytest.param(lambda v: str([v]), id='inside-list'),
        pytest.param(lambda v: str((v, 'neighbour')), id='inside-tuple'),
        pytest.param(lambda v: str({v}), id='inside-set'),
        pytest.param(lambda v: str(RuntimeError(v)), id='exception-str'),
        pytest.param(lambda v: repr(RuntimeError(v)), id='exception-repr'),
        pytest.param(lambda v: str(ValueError(v, 1)), id='exception-two-args'),
        pytest.param(lambda v: str(KeyError(v)), id='keyerror'),
    ],
)
def test_every_rendering_is_the_marker(opaque: OpaqueStr, render) -> None:
    _marker_only(render(opaque))


def test_str_and_repr_are_exactly_the_marker(opaque: OpaqueStr) -> None:
    assert str(opaque) == REDACTED
    assert type(str(opaque)) is str
    assert repr(opaque) == REDACTED
    assert format(opaque) == REDACTED


@pytest.mark.parametrize('spec', ['>40', '<40', '^40', '*^41', '40', '.3', '>40.3', '!<12'])
def test_a_format_spec_applies_to_the_marker(opaque: OpaqueStr, spec: str) -> None:
    expected = format(REDACTED, spec)
    assert format(opaque, spec) == expected
    assert f'{opaque:{spec}}' == expected
    assert ('{:' + spec + '}').format(opaque) == expected
    assert PLAIN not in expected and PLAIN[:3] not in expected


@pytest.mark.parametrize('spec', ['d', 'x', '.2f'])
def test_an_invalid_spec_raises_without_the_value_in_the_message(opaque: OpaqueStr, spec: str) -> None:
    with pytest.raises(ValueError) as raised:
        format(opaque, spec)
    message = str(raised.value)
    assert PLAIN not in message
    assert PLAIN[:3] not in message


def test_a_spec_with_a_conversion_formats_the_marker(opaque: OpaqueStr) -> None:
    assert f'{opaque!s:>30}' == format(REDACTED, '>30')
    assert f'{opaque!r:^30}' == format(REDACTED, '^30')
    assert '%-30s|' % (opaque,) == f'{REDACTED:<30}|'  # noqa: UP031
    assert '%30r|' % (opaque,) == f'{REDACTED:>30}|'  # noqa: UP031


def test_print_writes_the_marker(opaque: OpaqueStr, capsys: pytest.CaptureFixture[str]) -> None:
    print(opaque)
    print('value:', opaque, [opaque], {'k': opaque})
    _marker_only(capsys.readouterr().out)


@pytest.mark.parametrize(
    ('message', 'args'),
    [
        pytest.param('value %s', 'bare', id='percent-s'),
        pytest.param('value %r', 'bare', id='percent-r'),
        pytest.param('value %s', 'dict', id='inside-dict'),
        pytest.param('value %s', 'list', id='inside-list'),
        pytest.param('value %s', 'tuple', id='inside-tuple'),
        pytest.param('value %(k)s', 'mapping', id='mapping-argument'),
    ],
)
def test_a_logging_argument_renders_the_marker(
    opaque: OpaqueStr, caplog: pytest.LogCaptureFixture, message: str, args: str
) -> None:
    arg = {
        'bare': opaque,
        'dict': {'key': opaque},
        'list': [opaque],
        'tuple': (opaque, 'neighbour'),
        'mapping': {'k': opaque},
    }[args]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        logging.getLogger(LOGGER).info(message, arg)

    assert len(caplog.records) == 1
    _marker_only(caplog.records[0].getMessage())
    _marker_only(caplog.text)


def test_a_logged_message_that_is_the_value_renders_the_marker(
    opaque: OpaqueStr, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        logging.getLogger(LOGGER).warning(opaque)
    _marker_only(caplog.records[0].getMessage())


def test_a_logged_traceback_of_an_exception_raised_with_the_value_shows_the_marker(
    opaque: OpaqueStr, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        try:
            raise RuntimeError(opaque)
        except RuntimeError:
            logging.getLogger(LOGGER).exception('failed')

    text = caplog.text
    assert 'Traceback' in text, 'the traceback must be rendered, or the check below is vacuous'
    assert text.rstrip().splitlines()[-1] == f'RuntimeError: {REDACTED}'
    assert PLAIN not in text


def test_traceback_format_exception_shows_the_marker(opaque: OpaqueStr) -> None:
    try:
        raise ValueError(opaque)
    except ValueError as error:
        lines = traceback.format_exception(error)
    assert lines[-1] == f'ValueError: {REDACTED}\n'
    assert PLAIN not in ''.join(lines)


# --- reveal() -------------------------------------------------------------------------------


def test_reveal_returns_an_exact_str_holding_the_value(opaque: OpaqueStr) -> None:
    revealed = opaque.reveal()
    assert type(revealed) is str
    assert revealed == PLAIN
    assert str(revealed) == PLAIN
    assert repr(revealed) == repr(PLAIN)
    assert f'{revealed}' == PLAIN


def test_reveal_of_a_non_ascii_value() -> None:
    plain = 'clé-秘密-made-up'
    revealed = OpaqueStr(plain).reveal()
    assert type(revealed) is str
    assert revealed == plain
    assert revealed.encode() == plain.encode()


def test_reveal_of_the_empty_value() -> None:
    revealed = OpaqueStr('').reveal()
    assert type(revealed) is str
    assert revealed == ''
    assert str(OpaqueStr('')) == REDACTED


# --- constructor ----------------------------------------------------------------------------


def test_wrapping_an_existing_instance_returns_it_unchanged(opaque: OpaqueStr) -> None:
    assert OpaqueStr(opaque) is opaque
    assert OpaqueStr(OpaqueStr(OpaqueStr(PLAIN))).reveal() == PLAIN


def test_double_wrapping_keeps_the_value_not_the_marker(opaque: OpaqueStr) -> None:
    rewrapped = OpaqueStr(opaque)
    assert rewrapped.reveal() == PLAIN
    assert rewrapped == PLAIN
    assert rewrapped != REDACTED


def test_wrapping_a_redacted_str_keeps_the_value() -> None:
    wrapped = OpaqueStr(RedactedStr(PLAIN))
    assert type(wrapped) is OpaqueStr
    assert wrapped.reveal() == PLAIN
    assert type(wrapped.reveal()) is str


def test_wrapping_a_str_subclass_that_overrides_str_keeps_the_raw_characters() -> None:
    """The constructor reads the characters, not str() of its argument."""

    class Lying(str):
        def __str__(self) -> str:
            return 'something-else'

    assert OpaqueStr(Lying(PLAIN)).reveal() == PLAIN


@pytest.mark.parametrize(
    'value',
    [
        pytest.param(PLAIN.encode(), id='bytes'),
        pytest.param(bytearray(PLAIN.encode()), id='bytearray'),
        pytest.param(41813, id='int'),
        pytest.param(None, id='none'),
        pytest.param([PLAIN], id='list-holding-the-value'),
    ],
)
def test_a_non_str_is_refused_without_its_value_in_the_message(value: object) -> None:
    with pytest.raises(TypeError) as raised:
        OpaqueStr(value)  # type: ignore[arg-type]

    message = str(raised.value)
    assert type(value).__name__ in message
    assert PLAIN not in message
    assert str(41813) not in message
    if value is not None:
        assert repr(value) not in message


# --- VALUE: kept where it must be -----------------------------------------------------------


def test_equality_and_hash_match_the_plain_value(opaque: OpaqueStr) -> None:
    assert opaque == PLAIN
    assert operator.eq(PLAIN, opaque), 'equality must hold with the plain value on the left too'
    assert operator.ne(opaque, PLAIN) is False
    assert opaque != REDACTED
    assert hash(opaque) == hash(PLAIN)
    assert {PLAIN: 'found'}[opaque] == 'found'
    assert {opaque: 'found'}[PLAIN] == 'found'
    assert opaque in {PLAIN}
    assert len(opaque) == len(PLAIN)


def test_encode_and_bytes_carry_the_value(opaque: OpaqueStr) -> None:
    assert opaque.encode() == PLAIN.encode()
    assert opaque.encode('ascii') == PLAIN.encode('ascii')
    assert bytes(opaque, 'utf-8') == PLAIN.encode()
    assert list(opaque) == list(PLAIN)
    assert str.__str__(opaque) == PLAIN


@pytest.mark.parametrize('indent', [None, 2], ids=['c-encoder', 'python-encoder'])
def test_json_dumps_carries_the_value(opaque: OpaqueStr, indent: int | None) -> None:
    assert json.dumps(opaque, indent=indent) == json.dumps(PLAIN, indent=indent)
    assert json.dumps({'key': opaque}, indent=indent) == json.dumps({'key': PLAIN}, indent=indent)


@pytest.mark.parametrize('protocol', range(2, pickle.HIGHEST_PROTOCOL + 1))
def test_pickle_round_trips_type_and_value(opaque: OpaqueStr, protocol: int) -> None:
    """R4 A: pickle is left as str's. The value is in the pickle bytes and survives the trip."""
    data = pickle.dumps(opaque, protocol)
    assert PLAIN.encode() in data
    loaded = pickle.loads(data)
    assert type(loaded) is OpaqueStr
    assert loaded.reveal() == PLAIN


@pytest.mark.parametrize('protocol', [0, 1])
def test_pickle_under_the_old_protocols_serialises_only_the_marker(opaque: OpaqueStr, protocol: int) -> None:
    """Protocols 0 and 1 hold the marker: a documented hazard that fails closed, not a defect.

    copyreg._reduce_ex takes str() of the instance, so the pickle bytes carry the marker and never
    the value, and the round trip yields an OpaqueStr holding the marker. Ruled on tj-7zjov1.70
    (F1: documented hazard, no reduce override), reasoning in ADR tj-vhboky.41 ADDENDUM 3. A reduce
    override would put the value into these bytes and turn this red.
    """
    data = pickle.dumps(opaque, protocol)
    assert PLAIN.encode() not in data
    assert REDACTED.encode() in data, 'the marker must be what was serialised, or the check above is vacuous'

    loaded = pickle.loads(data)
    assert type(loaded) is OpaqueStr
    assert str.__str__(loaded) == REDACTED
    assert loaded.reveal() != PLAIN


@pytest.mark.parametrize('copier', [copy.copy, copy.deepcopy], ids=['copy', 'deepcopy'])
def test_copy_keeps_type_and_value(opaque: OpaqueStr, copier) -> None:
    copied = copier(opaque)
    assert type(copied) is OpaqueStr
    assert copied.reveal() == PLAIN
    assert str(copied) == REDACTED


def test_deepcopy_of_a_container_keeps_the_value(opaque: OpaqueStr) -> None:
    copied = copy.deepcopy({'credentials': [opaque]})
    assert type(copied['credentials'][0]) is OpaqueStr
    assert copied['credentials'][0].reveal() == PLAIN


# --- PLAIN STR OUT: the documented hazard, pinned -------------------------------------------


@pytest.mark.parametrize(
    'derive',
    [
        pytest.param(lambda v: v + '', id='concat-right'),
        pytest.param(lambda v: '' + v, id='concat-left'),
        pytest.param(lambda v: v[:], id='full-slice'),
        pytest.param(lambda v: v[0:], id='slice'),
        pytest.param(lambda v: ''.join([v]), id='join'),
        pytest.param(lambda v: v.strip(), id='strip'),
        pytest.param(lambda v: v.lower(), id='lower'),
        pytest.param(lambda v: v.replace('', ''), id='replace'),
        pytest.param(lambda v: v * 1, id='repeat'),
    ],
)
def test_str_operations_return_an_exact_str_carrying_the_value(opaque: OpaqueStr, derive) -> None:
    derived = derive(opaque)
    assert type(derived) is str
    assert derived == PLAIN
    assert str(derived) == PLAIN


def test_concatenation_into_an_exception_shows_the_value(opaque: OpaqueStr) -> None:
    """The hazard the docstring names: build text with + and the protection is gone."""
    assert PLAIN in str(RuntimeError('bad key ' + opaque))


def test_a_pydantic_str_field_strips_the_subclass(opaque: OpaqueStr) -> None:
    class Model(pydantic.BaseModel):
        key: str

    model = Model(key=opaque)
    assert type(model.key) is str
    assert model.key == PLAIN


# --- SILENT CORRUPTION: the documented hazard, pinned ---------------------------------------


def test_str_as_a_conversion_yields_the_marker(opaque: OpaqueStr) -> None:
    converted = str(opaque)
    assert converted == REDACTED
    assert converted != PLAIN


def test_redacted_str_of_an_opaque_str_holds_the_marker(opaque: OpaqueStr) -> None:
    rewrapped = RedactedStr(opaque)
    assert str.__str__(rewrapped) == REDACTED
    assert rewrapped != PLAIN
