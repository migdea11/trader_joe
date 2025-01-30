from enum import IntEnum
from typing import Self


class NamedIntEnum(IntEnum):
    """Base class for creating named integer enums."""
    @classmethod
    def validate(cls, value: int) -> Self:
        """Validate and parse the enum value.

        Args:
            value (int): Value to validate.

        Raises:
            ValueError: Invalid enum value.
            ValueError: Invalid type for enum_field.

        Returns:
            Self: Enum instance.
        """
        if isinstance(value, str):  # Parse from lowercase string
            try:
                return cls._decode(value)
            except KeyError:
                raise ValueError(f"Invalid enum value: {value}. Must be one of {[cls.encoder(e) for e in cls]}")
        elif isinstance(value, int):  # Parse from integer
            return cls(value)
        elif isinstance(value, cls):  # Already a valid enum instance
            return value
        raise ValueError(f"Invalid type for enum_field: {type(value)}")

    @classmethod
    def _decode(cls, value: str) -> Self:
        """Decode the enum value from a string

        Args:
            value (str): Value to decode.

        Returns:
            Self: Enum instance.
        """
        return cls[value.upper()]

    @classmethod
    def encoder(cls, value: Self) -> int:
        """Encode the enum value to an integer.

        Args:
            value (Self): Enum instance.

        Returns:
            int: Encoded value.
        """
        return value.name

    def __str__(self) -> str:
        return f"{type(self).__name__}.{self.name}[{self.value}]"

    def __repr__(self) -> str:
        return str(self)
