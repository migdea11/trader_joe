import os
from typing import TypeVar

from common.enums.config_enum import RunMode
from common.logging import get_logger


log = get_logger(__name__)
T = TypeVar('T')


def get_env_var(var_name: str, default: type[T] | None = None, cast_type: type[T] = str) -> T:
    """Get the environment variable.

    Args:
        var_name (str): Environment variable name.
        default (Type[T], optional): Default value for the environment variable. Defaults to None.
        cast_type (Type[T], optional): Type to cast the value. Defaults to str.

    Returns:
        T: Environment variable value.
    """
    var = os.getenv(var_name, default)
    if cast_type is not str and var is not None:
        if cast_type is bool and isinstance(var, str):
            var = var.lower() in ['true', '1']
        else:
            try:
                var = cast_type(var)
            except ValueError as e:
                log.error(f'Failed to cast {var_name} to {cast_type}: {e}')
                if default is not None:
                    log.info(f'Using default value: {default}')
                    var = default
                else:
                    raise
    return var


def get_run_mode() -> str:
    """Get the run mode (dev or prod).

    Defaults to PROD. DEV is the mode that opens a debugpy socket (see
    common.app_lifecycle.init_debugger), so an unset RUN_MODE must not select it --
    anything running outside the dev image (tests, scripts, a bare uvicorn) inherits
    no RUN_MODE at all. The dev image sets RUN_MODE=dev explicitly (Dockerfile), which
    is the only thing that should. An unrecognised value falls back here too, because
    get_env_var() returns the default when the cast fails.

    Returns:
        str: Run mode.
    """
    return get_env_var('RUN_MODE', default=RunMode.PROD, cast_type=RunMode)
