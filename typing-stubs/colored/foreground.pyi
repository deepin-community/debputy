from .exceptions import InvalidColor as InvalidColor
from .library import Library as Library
from .utilities import Utilities as Utilities

class MetaFore(type):
    def __getattr__(cls, color: str): ...

class Fore(metaclass=MetaFore):
    @classmethod
    def rgb(cls, r: int | str, g: int | str, b: int | str) -> str: ...
    @classmethod
    def RGB(cls, r: int | str, g: int | str, b: int | str) -> str: ...

class fore(Fore): ...
