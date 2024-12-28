from enum import IntEnum
from typing import Self


class NamedIntEnum(IntEnum):
    """
    Base class for creating named integer enums.
    """
    @classmethod
    def validate(cls, value: int) -> Self:
        """
        Validate that the value is a valid enum member.
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
        """
        Convert the integer to an enum.
        """
        return cls[value]

    @classmethod
    def encoder(cls, value: Self) -> int:
        """
        Convert the enum to an integer.
        """
        return value.name

    def __str__(self) -> str:
        return f"{type(self).__name__}.{self.name}[{self.value}]"

    def __repr__(self) -> str:
        return str(self)
