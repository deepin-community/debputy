from .exceptions import InvalidColor as InvalidColor
from .library import Library as Library
from .utilities import Utilities as Utilities

class MetaBack(type):
    def __getattr__(cls, color: str): ...

class Back(metaclass=MetaBack):
    @classmethod
    def rgb(cls, r: int | str, g: int | str, b: int | str) -> str: ...
    @classmethod
    def RGB(cls, r: int | str, g: int | str, b: int | str) -> str: ...

class back(Back): ...
