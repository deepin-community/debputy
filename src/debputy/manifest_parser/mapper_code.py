from typing import (
    TypeVar,
    Optional,
    Union,
    List,
    Callable,
)

from debputy.manifest_parser.exceptions import ManifestTypeException
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage
from debputy.util import assume_not_none

S = TypeVar("S")
T = TypeVar("T")


def type_mapper_str2package(
    raw_package_name: str,
    ap: AttributePath,
    opc: Optional[ParserContextData],
) -> BinaryPackage:
    pc = assume_not_none(opc)
    if "{{" in raw_package_name:
        resolved_package_name = pc.substitution.substitute(raw_package_name, ap.path)
    else:
        resolved_package_name = raw_package_name

    package_name_in_message = raw_package_name
    if resolved_package_name != raw_package_name:
        package_name_in_message = f'"{resolved_package_name}" ["{raw_package_name}"]'

    if not pc.is_known_package(resolved_package_name):
        package_names = ", ".join(pc.binary_packages)
        raise ManifestTypeException(
            f'The value {package_name_in_message} (from "{ap.path}") does not reference a package declared in'
            f" debian/control. Valid options are: {package_names}"
        )
    package_data = pc.binary_package_data(resolved_package_name)
    if package_data.is_auto_generated_package:
        package_names = ", ".join(pc.binary_packages)
        raise ManifestTypeException(
            f'The package name {package_name_in_message} (from "{ap.path}") references an auto-generated package.'
            " However, auto-generated packages are now permitted here. Valid options are:"
            f" {package_names}"
        )
    return package_data.binary_package


def wrap_into_list(
    x: T,
    _ap: AttributePath,
    _pc: Optional["ParserContextData"],
) -> List[T]:
    return [x]


def normalize_into_list(
    x: Union[T, List[T]],
    _ap: AttributePath,
    _pc: Optional["ParserContextData"],
) -> List[T]:
    return x if isinstance(x, list) else [x]


def map_each_element(
    mapper: Callable[[S, AttributePath, Optional["ParserContextData"]], T],
) -> Callable[[List[S], AttributePath, Optional["ParserContextData"]], List[T]]:
    def _generated_mapper(
        xs: List[S],
        ap: AttributePath,
        pc: Optional["ParserContextData"],
    ) -> List[T]:
        return [mapper(s, ap[i], pc) for i, s in enumerate(xs)]

    return _generated_mapper
