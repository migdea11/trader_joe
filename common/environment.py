import os
from typing import Type, TypeVar

T = TypeVar('T')


def get_env_var(var_name: str, default: Type[T] = None, cast_type: Type[T] = str) -> T:
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
            var = cast_type(var)
    return var


def get_run_mode() -> str:
    """Get the run mode (dev or prod).

    Returns:
        str: Run mode.
    """
    return get_env_var("RUN_MODE", default="not found")
