import dataclasses
from typing import (
    Iterator,
    Union,
    Optional,
    List,
    Tuple,
    Mapping,
    get_origin,
    get_args,
    Any,
    Type,
    TypeVar,
    TYPE_CHECKING,
    Iterable,
    Container,
    Literal,
    FrozenSet,
    cast,
)

from debputy.yaml.compat import CommentedBase

from debputy.manifest_parser.exceptions import ManifestParseException

if TYPE_CHECKING:
    from debputy.manifest_parser.parser_data import ParserContextData
    from debputy.plugin.api.spec import DebputyIntegrationMode, PackageTypeSelector


MP = TypeVar("MP", bound="DebputyParseHint")
StrOrInt = Union[str, int]
AttributePathAliasMapping = Mapping[
    StrOrInt, Tuple[StrOrInt, Optional["AttributePathAliasMapping"]]
]
LineReportKind = Literal["key", "value", "container"]


_PACKAGE_TYPE_DEB_ONLY = frozenset(["deb"])
_ALL_PACKAGE_TYPES = frozenset(["deb", "udeb"])


def resolve_package_type_selectors(
    package_type: "PackageTypeSelector",
) -> FrozenSet[str]:
    if package_type is _ALL_PACKAGE_TYPES or package_type is _PACKAGE_TYPE_DEB_ONLY:
        return cast("FrozenSet[str]", package_type)
    if isinstance(package_type, str):
        return (
            _PACKAGE_TYPE_DEB_ONLY
            if package_type == "deb"
            else frozenset([package_type])
        )
    else:
        return frozenset(package_type)


class AttributePath:
    __slots__ = ("parent", "container", "name", "alias_mapping", "path_hint")

    def __init__(
        self,
        parent: Optional["AttributePath"],
        key: Optional[Union[str, int]],
        *,
        container: Optional[Any] = None,
        alias_mapping: Optional[AttributePathAliasMapping] = None,
    ) -> None:
        self.parent = parent
        self.container = container
        self.name = key
        self.path_hint: Optional[str] = None
        self.alias_mapping = alias_mapping

    @classmethod
    def root_path(cls, container: Optional[Any]) -> "AttributePath":
        return AttributePath(None, None, container=container)

    @classmethod
    def builtin_path(cls) -> "AttributePath":
        return AttributePath(None, "$builtin$")

    @classmethod
    def test_path(cls) -> "AttributePath":
        return AttributePath(None, "$test$")

    def __bool__(self) -> bool:
        return self.name is not None or self.parent is not None

    def copy_with_path_hint(self, path_hint: str) -> "AttributePath":
        p = self.__class__(self.parent, self.name, alias_mapping=self.alias_mapping)
        p.path_hint = path_hint
        return p

    def path_segments(self) -> Iterable[Union[str, int]]:
        segments = list(self._iter_path())
        segments.reverse()
        yield from (s.name for s in segments)

    def _resolve_path(self, report_kind: LineReportKind) -> str:
        parent = self.parent
        key = self.name
        if report_kind == "container":
            key = parent.name if parent else None
            parent = parent.parent if parent else None
        container = parent.container if parent is not None else None

        if isinstance(container, CommentedBase):
            lc = container.lc
            try:
                if isinstance(key, str):
                    if report_kind == "key":
                        lc_data = lc.key(key)
                    else:
                        lc_data = lc.value(key)
                else:
                    lc_data = lc.item(key)
            except (AttributeError, RuntimeError, LookupError, TypeError):
                lc_data = None
        else:
            lc_data = None

        segments = list(self._iter_path())
        segments.reverse()
        parts: List[str] = []
        path_hint = None

        for s in segments:
            k = s.name
            s_path_hint = s.path_hint
            if s_path_hint is not None:
                path_hint = s_path_hint
            if isinstance(k, int):
                parts.append(f"[{k}]")
            elif k is not None:
                if parts:
                    parts.append(".")
                parts.append(k)

        if lc_data is not None:
            line_pos, col = lc_data
            # Translate 0-based (index) to 1-based (line number)
            line_pos += 1
            parts.append(f" [Line {line_pos} column {col}]")

        elif path_hint:
            parts.append(f" <Search for: {path_hint}>")
        if not parts:
            return "document root"
        return "".join(parts)

    @property
    def path_container_lc(self) -> str:
        return self._resolve_path("container")

    @property
    def path_key_lc(self) -> str:
        return self._resolve_path("key")

    @property
    def path(self) -> str:
        return self._resolve_path("value")

    def __str__(self) -> str:
        return self.path

    def __getitem__(self, item: Union[str, int]) -> "AttributePath":
        alias_mapping = None
        if self.alias_mapping:
            match = self.alias_mapping.get(item)
            if match:
                item, alias_mapping = match
                if item == "":
                    # Support `sources[0]` mapping to `source` by `sources -> source` and `0 -> ""`.
                    return AttributePath(
                        self.parent,
                        self.name,
                        alias_mapping=alias_mapping,
                        container=self.container,
                    )
        container = self.container
        if container is not None:
            try:
                child_container = self.container[item]
            except (AttributeError, RuntimeError, LookupError, TypeError):
                child_container = None
        else:
            child_container = None
        return AttributePath(
            self,
            item,
            alias_mapping=alias_mapping,
            container=child_container,
        )

    def _iter_path(self) -> Iterator["AttributePath"]:
        current = self
        yield current
        while True:
            parent = current.parent
            if not parent:
                break
            current = parent
            yield current


def check_integration_mode(
    path: AttributePath,
    parser_context: Optional["ParserContextData"] = None,
    expected_debputy_integration_mode: Optional[
        Container["DebputyIntegrationMode"]
    ] = None,
) -> None:
    if expected_debputy_integration_mode is None:
        return
    if parser_context is None:
        raise AssertionError(
            f"Cannot use integration mode restriction when parsing {path.path} since it is not parsed in the manifest context"
        )
    if parser_context.debputy_integration_mode not in expected_debputy_integration_mode:
        raise ManifestParseException(
            f"The attribute {path.path} cannot be used as it is not allowed for"
            f" the current debputy integration mode ({parser_context.debputy_integration_mode})."
            f" Please remove the manifest definition or change the integration mode"
        )


@dataclasses.dataclass(slots=True, frozen=True)
class _SymbolicModeSegment:
    base_mode: int
    base_mask: int
    cap_x_mode: int
    cap_x_mask: int

    def apply(self, current_mode: int, is_dir: bool) -> int:
        if current_mode & 0o111 or is_dir:
            chosen_mode = self.cap_x_mode
            mode_mask = self.cap_x_mask
        else:
            chosen_mode = self.base_mode
            mode_mask = self.base_mask
        # set ("="): mode mask clears relevant segment and current_mode are the desired bits
        # add ("+"): mode mask keeps everything and current_mode are the desired bits
        # remove ("-"): mode mask clears relevant bits and current_mode are 0
        return (current_mode & mode_mask) | chosen_mode


def _symbolic_mode_bit_inverse(v: int) -> int:
    # The & part is necessary because otherwise python narrows the inversion to the minimum number of bits
    # required, which is not what we want.
    return ~v & 0o7777


def parse_symbolic_mode(
    symbolic_mode: str,
    attribute_path: Optional[AttributePath],
) -> Iterator[_SymbolicModeSegment]:
    sticky_bit = 0o01000
    setuid_bit = 0o04000
    setgid_bit = 0o02000
    mode_group_flag = 0o7
    subject_mask_and_shift = {
        "u": (mode_group_flag << 6, 6),
        "g": (mode_group_flag << 3, 3),
        "o": (mode_group_flag << 0, 0),
    }
    bits = {
        "r": (0o4, 0o4),
        "w": (0o2, 0o2),
        "x": (0o1, 0o1),
        "X": (0o0, 0o1),
        "s": (0o0, 0o0),  # Special-cased below (it depends on the subject)
        "t": (0o0, 0o0),  # Special-cased below
    }
    modifiers = {
        "+",
        "-",
        "=",
    }
    in_path = f" in {attribute_path.path}" if attribute_path is not None else ""
    for orig_part in symbolic_mode.split(","):
        base_mode = 0
        cap_x_mode = 0
        part = orig_part
        subjects = set()
        while part and part[0] in ("u", "g", "o", "a"):
            subject = part[0]
            if subject == "a":
                subjects = {"u", "g", "o"}
            else:
                subjects.add(subject)
            part = part[1:]
        if not subjects:
            subjects = {"u", "g", "o"}

        if part and part[0] in modifiers:
            modifier = part[0]
        elif not part:
            raise ValueError(
                f'Invalid symbolic mode{in_path}: expected [+-=] to be present (from "{orig_part}")'
            )
        else:
            raise ValueError(
                f'Invalid symbolic mode{in_path}: Expected "{part[0]}" to be one of [+-=]'
                f' (from "{orig_part}")'
            )
        part = part[1:]
        s_bit_seen = False
        t_bit_seen = False
        while part and part[0] in bits:
            if part == "s":
                s_bit_seen = True
            elif part == "t":
                t_bit_seen = True
            elif part in ("u", "g", "o"):
                raise NotImplementedError(
                    f"Cannot parse symbolic mode{in_path}: Sorry, we do not support referencing an"
                    " existing subject's permissions (a=u) in symbolic modes."
                )
            else:
                matched_bits = bits.get(part[0])
                if matched_bits is None:
                    valid_bits = "".join(bits)
                    raise ValueError(
                        f'Invalid symbolic mode{in_path}: Expected "{part[0]}" to be one of the letters'
                        f' in "{valid_bits}" (from "{orig_part}")'
                    )
                base_mode_bits, cap_x_mode_bits = bits[part[0]]
                base_mode |= base_mode_bits
                cap_x_mode |= cap_x_mode_bits
            part = part[1:]

        if part:
            raise ValueError(
                f'Invalid symbolic mode{in_path}: Could not parse "{part[0]}" from "{orig_part}"'
            )

        final_base_mode = 0
        final_cap_x_mode = 0
        segment_mask = 0
        for subject in subjects:
            mask, shift = subject_mask_and_shift[subject]
            segment_mask |= mask
            final_base_mode |= base_mode << shift
            final_cap_x_mode |= cap_x_mode << shift
        if modifier == "=":
            segment_mask |= setuid_bit if "u" in subjects else 0
            segment_mask |= setgid_bit if "g" in subjects else 0
            segment_mask |= sticky_bit if "o" in subjects else 0
        if s_bit_seen:
            if "u" in subjects:
                final_base_mode |= setuid_bit
                final_cap_x_mode |= setuid_bit
            if "g" in subjects:
                final_base_mode |= setgid_bit
                final_cap_x_mode |= setgid_bit
        if t_bit_seen:
            final_base_mode |= sticky_bit
            final_cap_x_mode |= sticky_bit
        if modifier == "+":
            final_base_mask = ~0
            final_cap_x_mask = ~0
        elif modifier == "-":
            final_base_mask = _symbolic_mode_bit_inverse(final_base_mode)
            final_cap_x_mask = _symbolic_mode_bit_inverse(final_cap_x_mode)
            final_base_mode = 0
            final_cap_x_mode = 0
        elif modifier == "=":
            # FIXME: Handle "unmentioned directory's setgid/setuid bits"
            inverted_mask = _symbolic_mode_bit_inverse(segment_mask)
            final_base_mask = inverted_mask
            final_cap_x_mask = inverted_mask
        else:
            raise AssertionError(
                f"Unknown modifier in symbolic mode: {modifier} - should not have happened"
            )
        yield _SymbolicModeSegment(
            base_mode=final_base_mode,
            base_mask=final_base_mask,
            cap_x_mode=final_cap_x_mode,
            cap_x_mask=final_cap_x_mask,
        )


def unpack_type(
    orig_type: Any,
    parsing_typed_dict_attribute: bool,
) -> Tuple[Any, Optional[Any], Tuple[Any, ...]]:
    raw_type = orig_type
    origin = get_origin(raw_type)
    args = get_args(raw_type)
    if not parsing_typed_dict_attribute and repr(origin) in (
        "typing.NotRequired",
        "typing.Required",
    ):
        raise ValueError(
            f"The Required/NotRequired attributes cannot be used outside typed dicts,"
            f" the type that triggered the error: {orig_type}"
        )

    while repr(origin) in ("typing.NotRequired", "typing.Required"):
        if len(args) != 1:
            raise ValueError(
                f"The type {raw_type} should have exactly one type parameter"
            )
        raw_type = args[0]
        origin = get_origin(raw_type)
        args = get_args(raw_type)

    assert not isinstance(raw_type, tuple)

    return raw_type, origin, args


def find_annotation(
    annotations: Tuple[Any, ...],
    anno_class: Type[MP],
) -> Optional[MP]:
    m = None
    for anno in annotations:
        if isinstance(anno, anno_class):
            if m is not None:
                raise ValueError(
                    f"The annotation {anno_class.__name__} was used more than once"
                )
            m = anno
    return m
