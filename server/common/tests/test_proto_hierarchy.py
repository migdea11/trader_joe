"""THE HIERARCHY RULES OF proto/ (decision tj-3mk3u5.42 F1 rule 8; ADR tj-r6vcgv B1).

``common/tests/test_make_proto.py`` pins what ``make proto`` DOES -- the include root, the clearing,
the namespace. This pins what the SOURCE TREE may contain, which is a different thing and has no
guard at all otherwise:

* a package's directory and its proto package name agree, so a canonical import path is derivable
  from either one (buf's STANDARD lint says the same; this says it without buf on PATH);
* NO CONTRACT IMPORTS ANOTHER CONTRACT. A contract may import shared vocabulary; two contracts that
  import each other are one contract with a seam drawn through it;
* AN EXTERNAL CONTRACT NEVER IMPORTS AN INTERNAL ONE. ``internal`` marks a contract whose two ends
  always deploy together and whose shape may change freely -- buf's breaking checks ignore it. An
  external contract that imported one would publish that freedom to the private repo and the SDK,
  and the next free change would be a breaking release;
* NO FIELD IS CALLED BARE ``timestamp``. ADR tj-r6vcgv B1: an unqualified name is what invites the
  DATA time and the SERVER'S RESPONSE time to collapse into one, which silently turns a 1000x replay
  into a 1x one. B1 prescribes no replacement scheme, so this enforces only the prohibition.

These are structural, cheap to break and invisible in generated code. Every one of them would still
compile, still generate and still pass the rest of the suite.

The marker is ``build_infra``: this checks the repository's own layout, not any component's
interface.
"""

import re

import pytest

from common.tests.proto_descriptors import FIRST_PARTY_PREFIX, PROTO_ROOT, first_party_files


pytestmark = pytest.mark.build_infra

# The reserved root under proto/, as the segments a canonical file name starts with: proto files live
# at trader_joe/proto/<domain...>/v<n>/<file>.proto.
VERSION = re.compile(r'^v[0-9]+$')

# A contract is a package holding a service; shared vocabulary is a package that holds none. The two
# kinds are derived from the descriptors below rather than listed, so a new package is classified
# automatically and cannot be forgotten.
INTERNAL_SEGMENT = 'internal'


def _packages() -> dict[str, list[str]]:
    """Proto package name to the canonical file names declaring it."""
    packages: dict[str, list[str]] = {}
    for name, file in sorted(first_party_files().items()):
        packages.setdefault(file.package, []).append(name)
    return packages


def _services_by_package() -> dict[str, list[str]]:
    """Proto package name to the service names it declares, for every first-party package."""
    services: dict[str, list[str]] = {package: [] for package in _packages()}
    for file in first_party_files().values():
        services[file.package].extend(service.name for service in file.service)
    return services


def _contract_packages() -> set[str]:
    """Packages that declare a service. Those are the contracts; the rest is shared vocabulary."""
    return {package for package, names in _services_by_package().items() if names}


def test_the_tree_is_not_empty_and_every_file_lies_under_the_reserved_root():
    """The sweep's own guard: a glob that found nothing would make every test below vacuously true.

    F1 rule 4 reserves ``proto/trader_joe/proto/``; ``make proto`` fails on a .proto outside it
    (pinned in test_make_proto.py). This asserts the committed tree actually satisfies it, which is
    the other half -- the target refusing a stray file says nothing about whether one is there.
    """
    on_disk = sorted(str(path.relative_to(PROTO_ROOT)) for path in PROTO_ROOT.rglob('*.proto'))
    assert on_disk, f'no .proto under {PROTO_ROOT}, so this file is asserting nothing'
    assert sorted(first_party_files()) == on_disk
    for name in on_disk:
        assert name.startswith(FIRST_PARTY_PREFIX), f'{name} is outside the reserved root'


@pytest.mark.parametrize('package', sorted(_packages()))
def test_a_packages_name_and_its_directory_agree(package: str):
    """The property a canonical import path depends on, and the one buf's STANDARD lint names first.

    Every consumer of proto/ -- the server, protoc-gen-es for the UI, the client SDK -- derives file
    names from the directory and symbol names from the package. If they disagree, each consumer picks
    a different one and the descriptor file names stop matching across consumers, which is the
    duplicate-symbol collision decision tj-3mk3u5.42 was written to avoid.

    Args:
        package: A first-party proto package name.
    """
    for file_name in _packages()[package]:
        directory = file_name.rsplit('/', 1)[0]
        assert directory.replace('/', '.') == package, f'{file_name} declares package {package}'
        assert VERSION.match(package.rsplit('.', 1)[-1]), f'{package} is not a versioned package'


@pytest.mark.parametrize('package', sorted(_contract_packages()))
def test_no_contract_imports_another_contract(package: str):
    """F1 rule 8. A contract imports shared vocabulary, never a second service's schema.

    The failure it prevents is not a compile error -- it compiles fine. It is that two contracts
    importing each other are ONE contract with a seam drawn through it: neither can version, deprecate
    or be deleted on its own, and a consumer that wanted one is handed both.

    Args:
        package: A first-party package declaring at least one service.
    """
    contracts = _contract_packages()
    by_file = first_party_files()

    offenders = [
        (file_name, dependency)
        for file_name in _packages()[package]
        for dependency in by_file[file_name].dependency
        if dependency in by_file and by_file[dependency].package in contracts - {package}
    ]
    assert not offenders, f'{package} imports another contract: {offenders}'


def test_an_external_contract_never_imports_an_internal_one():
    """``internal`` means both ends ship together, so its shape may change freely -- and must not leak.

    buf's breaking checks ignore ``trader_joe.proto.internal`` (F1 R3). An external contract that
    imported one would inherit a schema nothing guards, and the first free change to it would be a
    breaking change published to the private repo and the typed SDK. The direction is the whole point:
    internal importing external would be fine, and is not what this forbids.
    """
    by_file = first_party_files()

    offenders = [
        (file_name, dependency)
        for file_name, file in sorted(by_file.items())
        if INTERNAL_SEGMENT not in file.package.split('.')
        for dependency in file.dependency
        if dependency in by_file and INTERNAL_SEGMENT in by_file[dependency].package.split('.')
    ]
    assert not offenders, f'an external contract imports an internal one: {offenders}'


def test_shared_vocabulary_imports_no_contract():
    """The other half of rule 8, and the one that keeps market/v1 reusable.

    A vocabulary package that imported a contract could not be taken on its own: the UI and the SDK
    would pull a service schema in to get a Bar. Vocabulary depends on google/protobuf and on itself.
    """
    by_file = first_party_files()
    contracts = _contract_packages()

    offenders = [
        (file_name, dependency)
        for file_name, file in sorted(by_file.items())
        if file.package not in contracts
        for dependency in file.dependency
        if dependency in by_file and by_file[dependency].package in contracts
    ]
    assert not offenders, f'shared vocabulary imports a contract: {offenders}'


def test_no_field_anywhere_is_called_bare_timestamp():
    """ADR tj-r6vcgv B1, swept over every message in proto/ rather than over this one contract.

    B1 is a prohibition, not a naming scheme: it forbids the unqualified name and prescribes no
    replacement. The fetch contract answers it with ``bar_start`` for the data time and a single
    ``FetchDone.as_of`` for the server's response time, which is the two-timestamp distinction B1 is
    about. This guards the prohibition for every contract that comes after, which is where the rule
    will actually be forgotten.
    """
    offenders = [
        f'{file_name} {message.name}.{field.name}'
        for file_name, file in sorted(first_party_files().items())
        for message in file.message_type
        for field in message.field
        if field.name == 'timestamp'
    ]
    assert not offenders, f'ADR tj-r6vcgv B1 forbids a bare "timestamp" field: {offenders}'
