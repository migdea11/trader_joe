from enum import StrEnum


class RunMode(StrEnum):
    """Run mode."""
    DEV = "dev"
    PROD = "prod"