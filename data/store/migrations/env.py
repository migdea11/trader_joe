import importlib
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from alembic.autogenerate.api import AutogenContext

from common.environment import get_env_var
from common.logging import get_logger
from common.database.sql_alchemy_types import IntEnum
from data.store.app.db.models import alembic_base

log = get_logger(__name__)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Assuming your .env file is in the same directory as your Alembic directory or specify the path
load_dotenv(".env")
database_uri = get_env_var("DATABASE_URI")
log.debug(f"Setting up postgres URL: {database_uri}")
config.set_main_option('sqlalchemy.url', database_uri)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

models_dir = Path('/code/data/store/app/db/models')
print(f"Scanning models directory: {models_dir}")
for filename in models_dir.iterdir():
    if filename.suffix == ".py" and filename.stem not in ("__init__", "alembic_base"):
        module_name = f"data.store.app.db.models.{filename.stem}"
        print(f"found: {module_name}")
        importlib.import_module(module_name)
target_metadata = alembic_base.Base.metadata
print(f"Registered tables: {alembic_base.Base.metadata.tables.keys()}")


# Custom renderer for IntEnum
def render_int_enum(type_: str, object_: Any, autogen_context: AutogenContext):
    if type_ == 'type' and isinstance(object_, IntEnum):
        # Ensure enum_class is present
        enum_object: IntEnum = object_
        if hasattr(enum_object, "enum_class"):
            enum_class = enum_object.enum_class
            autogen_context.imports.add(f"from common.enums.data_stock import {enum_class.__name__}")
            autogen_context.imports.add("from common.database.sql_alchemy_types import IntEnum")
            return f"IntEnum({enum_class.__name__})"
        else:
            raise ValueError(f"IntEnum type {type_} is missing the 'enum_class' attribute.")

    return False

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
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_int_enum
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            render_item=render_int_enum
        )

        with context.begin_transaction():
            # Register custom render function
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
