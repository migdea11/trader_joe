from unittest.mock import patch
from typing import Any, Dict


def mock_get_env_var(env_values: Dict[str, Any]):
    def side_effect(key, default=None, cast_type=str):
        env_value = env_values.get(key)
        if isinstance(env_value, tuple):
            return env_value[0]
        else:
            return env_value

    patcher = patch(
        'common.environment.get_env_var',
        side_effect=side_effect
    )
    mock = patcher.start()
    return mock, patcher
