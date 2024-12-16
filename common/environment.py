import os


def get_env_var(var_name: str, default: str = None, is_num: bool = False) -> str | int:
    var = os.getenv(var_name, default)
    if is_num:
        return int(var)
    return var
