import dataclasses
from typing import (
    TypedDict,
    TYPE_CHECKING,
    Generic,
    Type,
    Callable,
    Optional,
)

from debputy.plugin.plugin_state import current_debputy_plugin_required
from debputy.types import S
from debputy.util import T

if TYPE_CHECKING:
    from debputy.manifest_parser.parser_data import ParserContextData

    from debputy.manifest_parser.util import AttributePath


class DebputyParsedContent(TypedDict):
    pass


class DebputyDispatchableType:
    __slots__ = ("_debputy_plugin",)

    def __init__(self) -> None:
        self._debputy_plugin = current_debputy_plugin_required()


@dataclasses.dataclass
class TypeMapping(Generic[S, T]):
    target_type: Type[T]
    source_type: Type[S]
    mapper: Callable[[S, "AttributePath", Optional["ParserContextData"]], T]
