"""The seed producer: a deterministic, synthetic-only seed of the market tables at the head revision.

Decision tj-vhboky.55 (SEED FORMAT, SEED PRODUCTION) and ruling tj-vhboky.56: a seed is
<revision>.sql plus <revision>.json, kept as committed files under tests/system/seeds/ and loaded
by the downgrade-with-data check. It runs in test_client, over the network, and prints one bundle
line on stdout:
    python -m data.store.seeds [--date YYYY-MM-DD] | python -m data.store.seeds.bundle --out DIR
See producer.py for the run, stack.py for the transport, scenario.py for what is seeded, dump.py for
the rendering (the seed format), bundle.py for the bundle contract, and manifest.py for the digests.

WHY THIS DIRECTORY AND NOT data/store/app. The image copies data/store/app and nothing else of the
service, so the producer never ships. It imports the fake's scenario prefixes from tests.fakes (R4
permits a non-shipped directory to import the test tree; nothing under data/store/app may).
"""
