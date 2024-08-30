from .exceptions import InvalidStyle as InvalidStyle
from .library import Library as Library
from .utilities import Utilities as Utilities

class MetaStyle(type):
    def __getattr__(cls, color: str): ...

class Style(metaclass=MetaStyle):
    @classmethod
    def underline_color(cls, color: str | int) -> str: ...
    @classmethod
    def UNDERLINE_COLOR(cls, color: str | int) -> str: ...

class style(Style): ...
