import importlib
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from common.database.sql_alchemy_table import AppBase
from common.environment import get_env_var
from common.logging import get_logger
from data.store.app.database.uri_display import is_ambiguous_database_uri, mask_database_uri


log = get_logger(__name__)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Assuming your .env file is in the same directory as your Alembic directory or specify the path
load_dotenv('.env')
database_uri = get_env_var('DATABASE_URI')
# tj-zb1di4: a raw, unescaped '@' in the password (docker-compose.yaml interpolates
# POSTGRES_PASS unencoded, so this is reachable) makes the DSN's userinfo/host split
# ambiguous. Masking the log line below isn't enough on its own -- the SAME ambiguous DSN
# still goes to psycopg2 in run_migrations_online(), and psycopg2's own connection-failure
# message names whatever it mis-parsed as the host (e.g. `could not translate host name
# "TAILpart@db-nx.invalid"`), which reaches make migrate/CI output exactly like the log line
# used to. So refuse before anything downstream gets a chance to leak part of the password,
# with a message that names the problem but never echoes the URI.
if database_uri and is_ambiguous_database_uri(database_uri):
    raise RuntimeError('DATABASE_URI is ambiguous (unescaped @ in the password?); percent-encode reserved characters.')
# DATABASE_URI carries the postgres password. Never log it verbatim -- this line used to, and
# the password showed up in plain text in `make migrate` output and in this public repo's CI
# logs. mask_database_uri() is the only thing allowed to sit inside this f-string.
log.debug(f'Setting up postgres URL: {mask_database_uri(database_uri)}')
# config.set_main_option() writes through configparser, which treats '%' as its own
# interpolation escape. A percent-encoded password (the correct way to put a reserved
# character like '@' in a URL, e.g. '%40') then raises ValueError with the FULL RAW URI --
# password included -- in the traceback, which reaches make migrate and CI output same as the
# log line above did. '%' -> '%%' is alembic's documented escape and round-trips losslessly:
# configparser un-doubles it back to a single '%' when the value is read back out.
config.set_main_option('sqlalchemy.url', database_uri.replace('%', '%%') if database_uri else database_uri)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

ALLOWED_MODELS = {'base_market_activity', 'stock_market_activity', 'store_dataset_entry'}
for model_name in ALLOWED_MODELS:
    try:
        importlib.import_module(f'data.store.app.database.models.{model_name}')  # nosem
        print(f'Successfully imported {model_name}')
    except ImportError as e:
        print(f'Failed to import {model_name}: {e}')

target_metadata = AppBase.DATA_STORE_BASE.metadata
print(f'Registered tables: {AppBase.DATA_STORE_BASE.metadata.tables.keys()}')


# Custom renderer for IntEnum
# def render_int_enum(type_: str, object_: Any, autogen_context: AutogenContext):
#     if type_ == 'type':
#         if isinstance(object_, OrderedEnum):
#             enum_object: OrderedEnum = object_
#             if hasattr(enum_object, "enum_class"):
#                 enum_class = enum_object.enum_class
#                 autogen_context.imports.add(f"from common.enums.data_stock import {enum_class.__name__}")
#                 autogen_context.imports.add("from common.database.sql_alchemy_types import IntEnum")
#                 return f"IntEnum({enum_class.__name__})"
#             else:
#                 raise ValueError(f"IntEnum type {type_} is missing the 'enum_class' attribute.")
#         elif isinstance(object_, NullableDateTime):
#             autogen_context.imports.add("from common.database.sql_alchemy_types import NullableDateTime")
#             return "NullableDateTime"

#     return False

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option('sqlalchemy.url')
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        # render_item=render_int_enum
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix='sqlalchemy.', poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # render_item=render_int_enum
        )

        with context.begin_transaction():
            # Register custom render function
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
