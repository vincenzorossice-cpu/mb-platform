from typing import TypeVar, Generic, Optional
from dataclasses import dataclass

T = TypeVar('T')

@dataclass
class Result(Generic[T]):
    success: bool
    data: Optional[T] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

    @classmethod
    def ok(cls, data: T = None) -> 'Result[T]':
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str, code: str) -> 'Result[T]':
        return cls(success=False, error=error, error_code=code)
