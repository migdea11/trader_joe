from unittest.mock import patch
from typing import Dict


def mock_get_env_var(env_values: Dict[str, str | int]):
    patcher = patch(
        'common.environment.get_env_var',
        side_effect=lambda key, default=None, is_num=False: (
            env_values.get(key, default)
        )
    )
    mock = patcher.start()
    return mock, patcher
