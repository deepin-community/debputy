from .exceptions import InvalidControl as InvalidControl
from .library import Library as Library
from .utilities import Utilities as Utilities
from _typeshed import Incomplete

class Controls:
    def __init__(self) -> None: ...
    def nav(self, name: str, row: int, column: Incomplete | None = None) -> str: ...
