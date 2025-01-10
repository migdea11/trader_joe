import pytest
from dotenv import load_dotenv
from pathlib import Path


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Load the .env file
    env_file = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=str(env_file))
