import os
import pytest
from unittest.mock import patch

from common.environment import get_env_var


def test_get_env_var_with_existing_env_var():
    with patch.dict(os.environ, {"TEST_VAR": "123"}):
        result = get_env_var("TEST_VAR")
        assert result == "123"


def test_get_env_var_with_default_value():
    with patch.dict(os.environ, {}, clear=True):  # Clear environment variables
        result = get_env_var("NON_EXISTENT_VAR", default="default_value")
        assert result == "default_value"


def test_get_env_var_with_cast_type():
    with patch.dict(os.environ, {"TEST_VAR": "123"}):
        result = get_env_var("TEST_VAR", cast_type=int)
        assert result == 123
        assert isinstance(result, int)


def test_get_env_var_with_missing_env_var_and_no_default():
    with patch.dict(os.environ, {}, clear=True):  # Clear environment variables
        result = get_env_var("NON_EXISTENT_VAR")
        assert result is None


def test_get_env_var_with_cast_type_and_default_value():
    with patch.dict(os.environ, {}, clear=True):  # Clear environment variables
        result = get_env_var("NON_EXISTENT_VAR", default="456", cast_type=int)
        assert result == 456
        assert isinstance(result, int)


def test_get_env_var_with_invalid_cast_type():
    with patch.dict(os.environ, {"TEST_VAR": "not_an_int"}):
        with pytest.raises(ValueError):
            get_env_var("TEST_VAR", cast_type=int)
