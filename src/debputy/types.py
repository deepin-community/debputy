from typing import TypeVar, TYPE_CHECKING

if TYPE_CHECKING:
    from debputy.plugin.api import VirtualPath
    from debputy.filesystem_scan import FSPath


VP = TypeVar("VP", "VirtualPath", "FSPath")
S = TypeVar("S", str, bytes)
