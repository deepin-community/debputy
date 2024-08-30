import collections
import dataclasses
import typing
from typing import (
    Any,
    Callable,
    Tuple,
    TypedDict,
    Dict,
    get_type_hints,
    Annotated,
    get_args,
    get_origin,
    TypeVar,
    Generic,
    FrozenSet,
    Mapping,
    Optional,
    cast,
    Type,
    Union,
    List,
    Collection,
    NotRequired,
    Iterable,
    Literal,
    Sequence,
    Container,
)

from debputy.manifest_parser.base_types import FileSystemMatchRule
from debputy.manifest_parser.exceptions import (
    ManifestParseException,
)
from debputy.manifest_parser.mapper_code import (
    normalize_into_list,
    wrap_into_list,
    map_each_element,
)
from debputy.manifest_parser.parse_hints import (
    ConditionalRequired,
    DebputyParseHint,
    TargetAttribute,
    ManifestAttribute,
    ConflictWithSourceAttribute,
    NotPathHint,
)
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.tagging_types import (
    DebputyParsedContent,
    DebputyDispatchableType,
    TypeMapping,
)
from debputy.manifest_parser.util import (
    AttributePath,
    unpack_type,
    find_annotation,
    check_integration_mode,
)
from debputy.plugin.api.impl_types import (
    DeclarativeInputParser,
    TD,
    ListWrappedDeclarativeInputParser,
    DispatchingObjectParser,
    DispatchingTableParser,
    TTP,
    TP,
    InPackageContextParser,
)
from debputy.plugin.api.spec import (
    ParserDocumentation,
    DebputyIntegrationMode,
    StandardParserAttributeDocumentation,
    undocumented_attr,
    ParserAttributeDocumentation,
    reference_documentation,
)
from debputy.util import _info, _warn, assume_not_none

try:
    from Levenshtein import distance
except ImportError:
    _WARN_ONCE = False

    def _detect_possible_typo(
        _key: str,
        _value: object,
        _manifest_attributes: Mapping[str, "AttributeDescription"],
        _path: "AttributePath",
    ) -> None:
        global _WARN_ONCE
        if not _WARN_ONCE:
            _WARN_ONCE = True
            _info(
                "Install python3-levenshtein to have debputy try to detect typos in the manifest."
            )

else:

    def _detect_possible_typo(
        key: str,
        value: object,
        manifest_attributes: Mapping[str, "AttributeDescription"],
        path: "AttributePath",
    ) -> None:
        k_len = len(key)
        key_path = path[key]
        matches: List[str] = []
        current_match_strength = 0
        for acceptable_key, attr in manifest_attributes.items():
            if abs(k_len - len(acceptable_key)) > 2:
                continue
            d = distance(key, acceptable_key)
            if d > 2:
                continue
            try:
                attr.type_validator.ensure_type(value, key_path)
            except ManifestParseException:
                if attr.type_validator.base_type_match(value):
                    match_strength = 1
                else:
                    match_strength = 0
            else:
                match_strength = 2

            if match_strength < current_match_strength:
                continue
            if match_strength > current_match_strength:
                current_match_strength = match_strength
                matches.clear()
            matches.append(acceptable_key)

        if not matches:
            return
        ref = f'at "{path.path}"' if path else "at the manifest root level"
        if len(matches) == 1:
            possible_match = repr(matches[0])
            _warn(
                f'Possible typo: The key "{key}" {ref} should probably have been {possible_match}'
            )
        else:
            matches.sort()
            possible_matches = ", ".join(repr(a) for a in matches)
            _warn(
                f'Possible typo: The key "{key}" {ref} should probably have been one of {possible_matches}'
            )


SF = TypeVar("SF")
T = TypeVar("T")
S = TypeVar("S")


_NONE_TYPE = type(None)


# These must be able to appear in an "isinstance" check and must be builtin types.
BASIC_SIMPLE_TYPES = {
    str: "string",
    int: "integer",
    bool: "boolean",
}


class AttributeTypeHandler:
    __slots__ = ("_description", "_ensure_type", "base_type", "mapper")

    def __init__(
        self,
        description: str,
        ensure_type: Callable[[Any, AttributePath], None],
        *,
        base_type: Optional[Type[Any]] = None,
        mapper: Optional[
            Callable[[Any, AttributePath, Optional["ParserContextData"]], Any]
        ] = None,
    ) -> None:
        self._description = description
        self._ensure_type = ensure_type
        self.base_type = base_type
        self.mapper = mapper

    def describe_type(self) -> str:
        return self._description

    def ensure_type(self, obj: object, path: AttributePath) -> None:
        self._ensure_type(obj, path)

    def base_type_match(self, obj: object) -> bool:
        base_type = self.base_type
        return base_type is not None and isinstance(obj, base_type)

    def map_type(
        self,
        value: Any,
        path: AttributePath,
        parser_context: Optional["ParserContextData"],
    ) -> Any:
        mapper = self.mapper
        if mapper is not None:
            return mapper(value, path, parser_context)
        return value

    def combine_mapper(
        self,
        mapper: Optional[
            Callable[[Any, AttributePath, Optional["ParserContextData"]], Any]
        ],
    ) -> "AttributeTypeHandler":
        if mapper is None:
            return self
        if self.mapper is not None:
            m = self.mapper

            def _combined_mapper(
                value: Any,
                path: AttributePath,
                parser_context: Optional["ParserContextData"],
            ) -> Any:
                return mapper(m(value, path, parser_context), path, parser_context)

        else:
            _combined_mapper = mapper

        return AttributeTypeHandler(
            self._description,
            self._ensure_type,
            base_type=self.base_type,
            mapper=_combined_mapper,
        )


@dataclasses.dataclass(slots=True)
class AttributeDescription:
    source_attribute_name: str
    target_attribute: str
    attribute_type: Any
    type_validator: AttributeTypeHandler
    annotations: Tuple[Any, ...]
    conflicting_attributes: FrozenSet[str]
    conditional_required: Optional["ConditionalRequired"]
    parse_hints: Optional["DetectedDebputyParseHint"] = None
    is_optional: bool = False


def _extract_path_hint(v: Any, attribute_path: AttributePath) -> bool:
    if attribute_path.path_hint is not None:
        return True
    if isinstance(v, str):
        attribute_path.path_hint = v
        return True
    elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], str):
        attribute_path.path_hint = v[0]
        return True
    return False


@dataclasses.dataclass(slots=True, frozen=True)
class DeclarativeNonMappingInputParser(DeclarativeInputParser[TD], Generic[TD, SF]):
    alt_form_parser: AttributeDescription
    inline_reference_documentation: Optional[ParserDocumentation] = None
    expected_debputy_integration_mode: Optional[Container[DebputyIntegrationMode]] = (
        None
    )

    def parse_input(
        self,
        value: object,
        path: AttributePath,
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        check_integration_mode(
            path,
            parser_context,
            self.expected_debputy_integration_mode,
        )
        if self.reference_documentation_url is not None:
            doc_ref = f" (Documentation: {self.reference_documentation_url})"
        else:
            doc_ref = ""

        alt_form_parser = self.alt_form_parser
        if value is None:
            form_note = f" The value must have type: {alt_form_parser.type_validator.describe_type()}"
            if self.reference_documentation_url is not None:
                doc_ref = f" Please see {self.reference_documentation_url} for the documentation."
            raise ManifestParseException(
                f"The attribute {path.path} was missing a value. {form_note}{doc_ref}"
            )
        _extract_path_hint(value, path)
        alt_form_parser.type_validator.ensure_type(value, path)
        attribute = alt_form_parser.target_attribute
        alias_mapping = {
            attribute: ("", None),
        }
        v = alt_form_parser.type_validator.map_type(value, path, parser_context)
        path.alias_mapping = alias_mapping
        return cast("TD", {attribute: v})


@dataclasses.dataclass(slots=True)
class DeclarativeMappingInputParser(DeclarativeInputParser[TD], Generic[TD, SF]):
    input_time_required_parameters: FrozenSet[str]
    all_parameters: FrozenSet[str]
    manifest_attributes: Mapping[str, "AttributeDescription"]
    source_attributes: Mapping[str, "AttributeDescription"]
    at_least_one_of: FrozenSet[FrozenSet[str]]
    alt_form_parser: Optional[AttributeDescription]
    mutually_exclusive_attributes: FrozenSet[FrozenSet[str]] = frozenset()
    _per_attribute_conflicts_cache: Optional[Mapping[str, FrozenSet[str]]] = None
    inline_reference_documentation: Optional[ParserDocumentation] = None
    path_hint_source_attributes: Sequence[str] = tuple()
    expected_debputy_integration_mode: Optional[Container[DebputyIntegrationMode]] = (
        None
    )

    def _parse_alt_form(
        self,
        value: object,
        path: AttributePath,
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        alt_form_parser = self.alt_form_parser
        if alt_form_parser is None:
            raise ManifestParseException(
                f"The attribute {path.path} must be a mapping.{self._doc_url_error_suffix()}"
            )
        _extract_path_hint(value, path)
        alt_form_parser.type_validator.ensure_type(value, path)
        assert (
            value is not None
        ), "The alternative form was None, but the parser should have rejected None earlier."
        attribute = alt_form_parser.target_attribute
        alias_mapping = {
            attribute: ("", None),
        }
        v = alt_form_parser.type_validator.map_type(value, path, parser_context)
        path.alias_mapping = alias_mapping
        return cast("TD", {attribute: v})

    def _validate_expected_keys(
        self,
        value: Dict[Any, Any],
        path: AttributePath,
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> None:
        unknown_keys = value.keys() - self.all_parameters
        doc_ref = self._doc_url_error_suffix()
        if unknown_keys:
            for k in unknown_keys:
                if isinstance(k, str):
                    _detect_possible_typo(k, value[k], self.manifest_attributes, path)
            unused_keys = self.all_parameters - value.keys()
            if unused_keys:
                k = ", ".join(unused_keys)
                raise ManifestParseException(
                    f'Unknown keys "{unknown_keys}" at {path.path_container_lc}".  Keys that could be used here are: {k}.{doc_ref}'
                )
            raise ManifestParseException(
                f'Unknown keys "{unknown_keys}" at {path.path_container_lc}".  Please remove them.{doc_ref}'
            )
        missing_keys = self.input_time_required_parameters - value.keys()
        if missing_keys:
            required = ", ".join(repr(k) for k in sorted(missing_keys))
            raise ManifestParseException(
                f"The following keys were required but not present at {path.path_container_lc}: {required}{doc_ref}"
            )
        for maybe_required in self.all_parameters - value.keys():
            attr = self.manifest_attributes[maybe_required]
            assert attr.conditional_required is None or parser_context is not None
            if (
                attr.conditional_required is not None
                and attr.conditional_required.condition_applies(
                    assume_not_none(parser_context)
                )
            ):
                reason = attr.conditional_required.reason
                raise ManifestParseException(
                    f'Missing the *conditionally* required attribute "{maybe_required}" at {path.path_container_lc}. {reason}{doc_ref}'
                )
        for keyset in self.at_least_one_of:
            matched_keys = value.keys() & keyset
            if not matched_keys:
                conditionally_required = ", ".join(repr(k) for k in sorted(keyset))
                raise ManifestParseException(
                    f"At least one of the following keys must be present at {path.path_container_lc}:"
                    f" {conditionally_required}{doc_ref}"
                )
        for group in self.mutually_exclusive_attributes:
            matched = value.keys() & group
            if len(matched) > 1:
                ck = ", ".join(repr(k) for k in sorted(matched))
                raise ManifestParseException(
                    f"Could not parse {path.path_container_lc}: The following attributes are"
                    f" mutually exclusive: {ck}{doc_ref}"
                )

    def _parse_typed_dict_form(
        self,
        value: Dict[Any, Any],
        path: AttributePath,
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        self._validate_expected_keys(value, path, parser_context=parser_context)
        result = {}
        per_attribute_conflicts = self._per_attribute_conflicts()
        alias_mapping = {}
        for path_hint_source_attributes in self.path_hint_source_attributes:
            v = value.get(path_hint_source_attributes)
            if v is not None and _extract_path_hint(v, path):
                break
        for k, v in value.items():
            attr = self.manifest_attributes[k]
            matched = value.keys() & per_attribute_conflicts[k]
            if matched:
                ck = ", ".join(repr(k) for k in sorted(matched))
                raise ManifestParseException(
                    f'The attribute "{k}" at {path.path} cannot be used with the following'
                    f" attributes: {ck}{self._doc_url_error_suffix()}"
                )
            nk = attr.target_attribute
            key_path = path[k]
            attr.type_validator.ensure_type(v, key_path)
            if v is None:
                continue
            if k != nk:
                alias_mapping[nk] = k, None
            v = attr.type_validator.map_type(v, key_path, parser_context)
            result[nk] = v
        if alias_mapping:
            path.alias_mapping = alias_mapping
        return cast("TD", result)

    def _doc_url_error_suffix(self, *, see_url_version: bool = False) -> str:
        doc_url = self.reference_documentation_url
        if doc_url is not None:
            if see_url_version:
                return f" Please see {doc_url} for the documentation."
            return f" (Documentation: {doc_url})"
        return ""

    def parse_input(
        self,
        value: object,
        path: AttributePath,
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        check_integration_mode(
            path,
            parser_context,
            self.expected_debputy_integration_mode,
        )
        if value is None:
            form_note = " The attribute must be a mapping."
            if self.alt_form_parser is not None:
                form_note = (
                    " The attribute can be a mapping or a non-mapping format"
                    ' (usually, "non-mapping format" means a string or a list of strings).'
                )
            doc_ref = self._doc_url_error_suffix(see_url_version=True)
            raise ManifestParseException(
                f"The attribute {path.path} was missing a value. {form_note}{doc_ref}"
            )

        if not isinstance(value, dict):
            return self._parse_alt_form(value, path, parser_context=parser_context)
        return self._parse_typed_dict_form(value, path, parser_context=parser_context)

    def _per_attribute_conflicts(self) -> Mapping[str, FrozenSet[str]]:
        conflicts = self._per_attribute_conflicts_cache
        if conflicts is not None:
            return conflicts
        attrs = self.source_attributes
        conflicts = {
            a.source_attribute_name: frozenset(
                attrs[ca].source_attribute_name for ca in a.conflicting_attributes
            )
            for a in attrs.values()
        }
        self._per_attribute_conflicts_cache = conflicts
        return self._per_attribute_conflicts_cache


def _is_path_attribute_candidate(
    source_attribute: AttributeDescription, target_attribute: AttributeDescription
) -> bool:
    if (
        source_attribute.parse_hints
        and not source_attribute.parse_hints.applicable_as_path_hint
    ):
        return False
    target_type = target_attribute.attribute_type
    _, origin, args = unpack_type(target_type, False)
    match_type = target_type
    if origin == list:
        match_type = args[0]
    return isinstance(match_type, type) and issubclass(match_type, FileSystemMatchRule)


if typing.is_typeddict(DebputyParsedContent):
    is_typeddict = typing.is_typeddict
else:

    def is_typeddict(t: Any) -> bool:
        if typing.is_typeddict(t):
            return True
        return isinstance(t, type) and issubclass(t, DebputyParsedContent)


class ParserGenerator:
    def __init__(self) -> None:
        self._registered_types: Dict[Any, TypeMapping[Any, Any]] = {}
        self._object_parsers: Dict[str, DispatchingObjectParser] = {}
        self._table_parsers: Dict[
            Type[DebputyDispatchableType], DispatchingTableParser[Any]
        ] = {}
        self._in_package_context_parser: Dict[str, Any] = {}

    def register_mapped_type(self, mapped_type: TypeMapping[Any, Any]) -> None:
        existing = self._registered_types.get(mapped_type.target_type)
        if existing is not None:
            raise ValueError(f"The type {existing} is already registered")
        self._registered_types[mapped_type.target_type] = mapped_type

    def get_mapped_type_from_target_type(
        self,
        mapped_type: Type[T],
    ) -> Optional[TypeMapping[Any, T]]:
        return self._registered_types.get(mapped_type)

    def discard_mapped_type(self, mapped_type: Type[T]) -> None:
        del self._registered_types[mapped_type]

    def add_table_parser(self, rt: Type[DebputyDispatchableType], path: str) -> None:
        assert rt not in self._table_parsers
        self._table_parsers[rt] = DispatchingTableParser(rt, path)

    def add_object_parser(
        self,
        path: str,
        *,
        parser_documentation: Optional[ParserDocumentation] = None,
        expected_debputy_integration_mode: Optional[
            Container[DebputyIntegrationMode]
        ] = None,
    ) -> None:
        assert path not in self._in_package_context_parser
        assert path not in self._object_parsers
        self._object_parsers[path] = DispatchingObjectParser(
            path,
            parser_documentation=parser_documentation,
            expected_debputy_integration_mode=expected_debputy_integration_mode,
        )

    def add_in_package_context_parser(
        self,
        path: str,
        delegate: DeclarativeInputParser[Any],
    ) -> None:
        assert path not in self._in_package_context_parser
        assert path not in self._object_parsers
        self._in_package_context_parser[path] = InPackageContextParser(path, delegate)

    @property
    def dispatchable_table_parsers(
        self,
    ) -> Mapping[Type[DebputyDispatchableType], DispatchingTableParser[Any]]:
        return self._table_parsers

    @property
    def dispatchable_object_parsers(self) -> Mapping[str, DispatchingObjectParser]:
        return self._object_parsers

    def dispatch_parser_table_for(
        self, rule_type: TTP
    ) -> Optional[DispatchingTableParser[TP]]:
        return cast(
            "Optional[DispatchingTableParser[TP]]", self._table_parsers.get(rule_type)
        )

    def generate_parser(
        self,
        parsed_content: Type[TD],
        *,
        source_content: Optional[SF] = None,
        allow_optional: bool = False,
        inline_reference_documentation: Optional[ParserDocumentation] = None,
        expected_debputy_integration_mode: Optional[
            Container[DebputyIntegrationMode]
        ] = None,
        automatic_docs: Optional[
            Mapping[Type[Any], Sequence[StandardParserAttributeDocumentation]]
        ] = None,
    ) -> DeclarativeInputParser[TD]:
        """Derive a parser from a TypedDict

        Generates a parser for a segment of the manifest (think the `install-docs` snippet) from a TypedDict
        or two that are used as a description.

        In its most simple use-case, the caller provides a TypedDict of the expected attributed along with
        their types. As an example:

          >>> class InstallDocsRule(DebputyParsedContent):
          ...     sources: List[str]
          ...     into: List[str]
          >>> pg = ParserGenerator()
          >>> simple_parser = pg.generate_parser(InstallDocsRule)

        This will create a parser that would be able to interpret something like:

        ```yaml
           install-docs:
             sources: ["docs/*"]
             into: ["my-pkg"]
        ```

        While this is sufficient for programmers, it is a bit rigid for the packager writing the manifest.  Therefore,
        you can also provide a TypedDict describing the input, enabling more flexibility:

          >>> class InstallDocsRule(DebputyParsedContent):
          ...     sources: List[str]
          ...     into: List[str]
          >>> class InputDocsRuleInputFormat(TypedDict):
          ...     source: NotRequired[Annotated[str, DebputyParseHint.target_attribute("sources")]]
          ...     sources: NotRequired[List[str]]
          ...     into: Union[str, List[str]]
          >>> pg = ParserGenerator()
          >>> flexible_parser = pg.generate_parser(
          ...    InstallDocsRule,
          ...    source_content=InputDocsRuleInputFormat,
          ... )

        In this case, the `sources` field can either come from a single `source` in the manifest (which must be a string)
        or `sources` (which must be a list of strings). The parser also ensures that only one of `source` or `sources`
        is used to ensure the input is not ambiguous. For the `into` parameter, the parser will accept it being a str
        or a list of strings.  Regardless of how the input was provided, the parser will normalize the input such that
        both `sources` and `into` in the result is a list of strings.  As an example, this parser can accept
        both the previous input but also the following input:

        ```yaml
           install-docs:
             source: "docs/*"
             into: "my-pkg"
        ```

        The `source` and `into` attributes are then normalized to lists as if the user had written them as lists
        with a single string in them. As noted above, the name of the `source` attribute will also be normalized
        while parsing.

        In the cases where only one field is required by the user, it can sometimes make sense to allow a non-dict
        as part of the input.  Example:

          >>> class DiscardRule(DebputyParsedContent):
          ...     paths: List[str]
          >>> class DiscardRuleInputDictFormat(TypedDict):
          ...     path: NotRequired[Annotated[str, DebputyParseHint.target_attribute("paths")]]
          ...     paths: NotRequired[List[str]]
          >>> # This format relies on DiscardRule having exactly one Required attribute
          >>> DiscardRuleInputWithAltFormat = Union[
          ...    DiscardRuleInputDictFormat,
          ...    str,
          ...    List[str],
          ... ]
          >>> pg = ParserGenerator()
          >>> flexible_parser = pg.generate_parser(
          ...    DiscardRule,
          ...    source_content=DiscardRuleInputWithAltFormat,
          ... )


        Supported types:
          * `List` - must have a fixed type argument (such as `List[str]`)
          * `str`
          * `int`
          * `BinaryPackage` - When provided (or required), the user must provide a package name listed
                              in the debian/control file. The code receives the BinaryPackage instance
                              matching that input.
          * `FileSystemMode` - When provided (or required), the user must provide a file system mode in any
                               format that `debputy' provides (such as `0644` or `a=rw,go=rw`).
          * `FileSystemOwner` - When provided (or required), the user must a file system owner that is
                                available statically on all Debian systems (must be in `base-passwd`).
                                The user has multiple options for how to specify it (either via name or id).
          * `FileSystemGroup` - When provided (or required), the user must a file system group that is
                                available statically on all Debian systems (must be in `base-passwd`).
                                The user has multiple options for how to specify it (either via name or id).
          * `ManifestCondition` - When provided (or required), the user must specify a conditional rule to apply.
                                  Usually, it is better to extend `DebputyParsedContentStandardConditional`, which
                                  provides the `debputy' default `when` parameter for conditionals.

        Supported special type-like parameters:

          * `Required` / `NotRequired` to mark a field as `Required` or `NotRequired`. Must be provided at the
             outermost level.  Cannot vary between `parsed_content` and `source_content`.
          * `Annotated`. Accepted at the outermost level (inside Required/NotRequired) but ignored at the moment.
          * `Union`. Must be the outermost level (inside `Annotated` or/and `Required`/`NotRequired` if these are present).
            Automapping (see below) is restricted to two members in the Union.

        Notable non-supported types:
          * `Mapping` and all variants therefore (such as `dict`). In the future, nested `TypedDict`s may be allowed.
          * `Optional` (or `Union[..., None]`): Use `NotRequired` for optional fields.

        Automatic mapping rules from `source_content` to `parsed_content`:
          - `Union[T, List[T]]` can be narrowed automatically to `List[T]`.  Transformation is basically:
            `lambda value: value if isinstance(value, list) else [value]`
          - `T` can be mapped automatically to `List[T]`, Transformation being: `lambda value: [value]`

        Additionally, types can be annotated (`Annotated[str, ...]`) with `DebputyParseHint`s.  Check its classmethod
        for concrete features that may be useful to you.

        :param parsed_content: A DebputyParsedContent / TypedDict describing the desired model of the input once parsed.
          (DebputyParsedContent is a TypedDict subclass that work around some inadequate type checkers).
          It can also be a `List[DebputyParsedContent]`. In that case, `source_content` must be a
          `List[TypedDict[...]]`.
        :param source_content: Optionally, a TypedDict describing the input allowed by the user.  This can be useful
          to describe more variations than in `parsed_content` that the parser will normalize for you. If omitted,
          the parsed_content is also considered the source_content (which affects what annotations are allowed in it).
          Note you should never pass the parsed_content as source_content directly.
        :param allow_optional: In rare cases, you want to support explicitly provided vs. optional.  In this case, you
          should set this to True.  Though, in 99.9% of all cases, you want `NotRequired` rather than `Optional` (and
          can keep this False).
        :param inline_reference_documentation: Optionally, programmatic documentation
        :param expected_debputy_integration_mode: If provided, this declares the integration modes where the
          result of the parser can be used. This is primarily useful for "fail-fast" on incorrect usage.
          When the restriction is not satisfiable, the generated parser will trigger a parse error immediately
          (resulting in a "compile time" failure rather than a "runtime" failure).
        :return: An input parser capable of reading input matching the TypedDict(s) used as reference.
        """
        orig_parsed_content = parsed_content
        if source_content is parsed_content:
            raise ValueError(
                "Do not provide source_content if it is the same as parsed_content"
            )
        is_list_wrapped = False
        if get_origin(orig_parsed_content) == list:
            parsed_content = get_args(orig_parsed_content)[0]
            is_list_wrapped = True

        if isinstance(parsed_content, type) and issubclass(
            parsed_content, DebputyDispatchableType
        ):
            parser = self.dispatch_parser_table_for(parsed_content)
            if parser is None:
                raise ValueError(
                    f"Unsupported parsed_content descriptor: {parsed_content.__qualname__}."
                    f" The class {parsed_content.__qualname__} is not a pre-registered type."
                )
            # FIXME: Only the list wrapped version has documentation.
            if is_list_wrapped:
                parser = ListWrappedDeclarativeInputParser(
                    parser,
                    inline_reference_documentation=inline_reference_documentation,
                    expected_debputy_integration_mode=expected_debputy_integration_mode,
                )
            return parser

        if not is_typeddict(parsed_content):
            raise ValueError(
                f"Unsupported parsed_content descriptor: {parsed_content.__qualname__}."
                ' Only "TypedDict"-based types and a subset of "DebputyDispatchableType" are supported.'
            )
        if is_list_wrapped and source_content is not None:
            if get_origin(source_content) != list:
                raise ValueError(
                    "If the parsed_content is a List type, then source_format must be a List type as well."
                )
            source_content = get_args(source_content)[0]

        target_attributes = self._parse_types(
            parsed_content,
            allow_source_attribute_annotations=source_content is None,
            forbid_optional=not allow_optional,
        )
        required_target_parameters = frozenset(parsed_content.__required_keys__)
        parsed_alt_form = None
        non_mapping_source_only = False

        if source_content is not None:
            default_target_attribute = None
            if len(required_target_parameters) == 1:
                default_target_attribute = next(iter(required_target_parameters))

            source_typed_dict, alt_source_forms = _extract_typed_dict(
                source_content,
                default_target_attribute,
            )
            if alt_source_forms:
                parsed_alt_form = self._parse_alt_form(
                    alt_source_forms,
                    default_target_attribute,
                )
            if source_typed_dict is not None:
                source_content_attributes = self._parse_types(
                    source_typed_dict,
                    allow_target_attribute_annotation=True,
                    allow_source_attribute_annotations=True,
                    forbid_optional=not allow_optional,
                )
                source_content_parameter = "source_content"
                source_and_parsed_differs = True
            else:
                source_typed_dict = parsed_content
                source_content_attributes = target_attributes
                source_content_parameter = "parsed_content"
                source_and_parsed_differs = True
                non_mapping_source_only = True
        else:
            source_typed_dict = parsed_content
            source_content_attributes = target_attributes
            source_content_parameter = "parsed_content"
            source_and_parsed_differs = False

        sources = collections.defaultdict(set)
        seen_targets = set()
        seen_source_names: Dict[str, str] = {}
        source_attributes: Dict[str, AttributeDescription] = {}
        path_hint_source_attributes = []

        for k in source_content_attributes:
            ia = source_content_attributes[k]

            ta = (
                target_attributes.get(ia.target_attribute)
                if source_and_parsed_differs
                else ia
            )
            if ta is None:
                # Error message would be wrong if this assertion is false.
                assert source_and_parsed_differs
                raise ValueError(
                    f'The attribute "{k}" from the "source_content" parameter should have mapped'
                    f' to "{ia.target_attribute}", but that parameter does not exist in "parsed_content"'
                )
            if _is_path_attribute_candidate(ia, ta):
                path_hint_source_attributes.append(ia.source_attribute_name)
            existing_source_name = seen_source_names.get(ia.source_attribute_name)
            if existing_source_name:
                raise ValueError(
                    f'The attribute "{k}" and "{existing_source_name}" both share the source name'
                    f' "{ia.source_attribute_name}". Please change the {source_content_parameter} parameter,'
                    f' so only one attribute use "{ia.source_attribute_name}".'
                )
            seen_source_names[ia.source_attribute_name] = k
            seen_targets.add(ta.target_attribute)
            sources[ia.target_attribute].add(k)
            if source_and_parsed_differs:
                bridge_mapper = self._type_normalize(
                    k, ia.attribute_type, ta.attribute_type, False
                )
                ia.type_validator = ia.type_validator.combine_mapper(bridge_mapper)
            source_attributes[k] = ia

        def _as_attr_names(td_name: Iterable[str]) -> FrozenSet[str]:
            return frozenset(
                source_content_attributes[a].source_attribute_name for a in td_name
            )

        _check_attributes(
            parsed_content,
            source_typed_dict,
            source_content_attributes,
            sources,
        )

        at_least_one_of = frozenset(
            _as_attr_names(g)
            for k, g in sources.items()
            if len(g) > 1 and k in required_target_parameters
        )

        if source_and_parsed_differs and seen_targets != target_attributes.keys():
            missing = ", ".join(
                repr(k) for k in (target_attributes.keys() - seen_targets)
            )
            raise ValueError(
                'The following attributes in "parsed_content" did not have a source field in "source_content":'
                f" {missing}"
            )
        all_mutually_exclusive_fields = frozenset(
            _as_attr_names(g) for g in sources.values() if len(g) > 1
        )

        all_parameters = (
            source_typed_dict.__required_keys__ | source_typed_dict.__optional_keys__
        )
        _check_conflicts(
            source_content_attributes,
            source_typed_dict.__required_keys__,
            all_parameters,
        )

        manifest_attributes = {
            a.source_attribute_name: a for a in source_content_attributes.values()
        }

        if parsed_alt_form is not None:
            target_attribute = parsed_alt_form.target_attribute
            if (
                target_attribute not in required_target_parameters
                and required_target_parameters
                or len(required_target_parameters) > 1
            ):
                raise NotImplementedError(
                    "When using alternative source formats (Union[TypedDict, ...]), then the"
                    " target must have at most one require parameter"
                )
            bridge_mapper = self._type_normalize(
                target_attribute,
                parsed_alt_form.attribute_type,
                target_attributes[target_attribute].attribute_type,
                False,
            )
            parsed_alt_form.type_validator = (
                parsed_alt_form.type_validator.combine_mapper(bridge_mapper)
            )

        inline_reference_documentation = (
            _verify_and_auto_correct_inline_reference_documentation(
                parsed_content,
                source_typed_dict,
                source_content_attributes,
                inline_reference_documentation,
                parsed_alt_form is not None,
                automatic_docs,
            )
        )
        if non_mapping_source_only:
            parser = DeclarativeNonMappingInputParser(
                assume_not_none(parsed_alt_form),
                inline_reference_documentation=inline_reference_documentation,
                expected_debputy_integration_mode=expected_debputy_integration_mode,
            )
        else:
            parser = DeclarativeMappingInputParser(
                _as_attr_names(source_typed_dict.__required_keys__),
                _as_attr_names(all_parameters),
                manifest_attributes,
                source_attributes,
                mutually_exclusive_attributes=all_mutually_exclusive_fields,
                alt_form_parser=parsed_alt_form,
                at_least_one_of=at_least_one_of,
                inline_reference_documentation=inline_reference_documentation,
                path_hint_source_attributes=tuple(path_hint_source_attributes),
                expected_debputy_integration_mode=expected_debputy_integration_mode,
            )
        if is_list_wrapped:
            parser = ListWrappedDeclarativeInputParser(
                parser,
                expected_debputy_integration_mode=expected_debputy_integration_mode,
            )
        return parser

    def _as_type_validator(
        self,
        attribute: str,
        provided_type: Any,
        parsing_typed_dict_attribute: bool,
    ) -> AttributeTypeHandler:
        assert not isinstance(provided_type, tuple)

        if isinstance(provided_type, type) and issubclass(
            provided_type, DebputyDispatchableType
        ):
            return _dispatch_parser(provided_type)

        unmapped_type = self._strip_mapped_types(
            provided_type,
            parsing_typed_dict_attribute,
        )
        type_normalizer = self._type_normalize(
            attribute,
            unmapped_type,
            provided_type,
            parsing_typed_dict_attribute,
        )
        t_unmapped, t_orig, t_args = unpack_type(
            unmapped_type,
            parsing_typed_dict_attribute,
        )

        if (
            t_orig == Union
            and t_args
            and len(t_args) == 2
            and any(v is _NONE_TYPE for v in t_args)
        ):
            _, _, args = unpack_type(provided_type, parsing_typed_dict_attribute)
            actual_type = [a for a in args if a is not _NONE_TYPE][0]
            validator = self._as_type_validator(
                attribute, actual_type, parsing_typed_dict_attribute
            )

            def _validator(v: Any, path: AttributePath) -> None:
                if v is None:
                    return
                validator.ensure_type(v, path)

            return AttributeTypeHandler(
                validator.describe_type(),
                _validator,
                base_type=validator.base_type,
                mapper=type_normalizer,
            )

        if unmapped_type in BASIC_SIMPLE_TYPES:
            type_name = BASIC_SIMPLE_TYPES[unmapped_type]

            type_mapping = self._registered_types.get(provided_type)
            if type_mapping is not None:
                simple_type = f" ({type_name})"
                type_name = type_mapping.target_type.__name__
            else:
                simple_type = ""

            def _validator(v: Any, path: AttributePath) -> None:
                if not isinstance(v, unmapped_type):
                    _validation_type_error(
                        path, f"The attribute must be a {type_name}{simple_type}"
                    )

            return AttributeTypeHandler(
                type_name,
                _validator,
                base_type=unmapped_type,
                mapper=type_normalizer,
            )
        if t_orig == list:
            if not t_args:
                raise ValueError(
                    f'The attribute "{attribute}" is List but does not have Generics (Must use List[X])'
                )
            _, t_provided_orig, t_provided_args = unpack_type(
                provided_type,
                parsing_typed_dict_attribute,
            )
            genetic_type = t_args[0]
            key_mapper = self._as_type_validator(
                attribute,
                genetic_type,
                parsing_typed_dict_attribute,
            )

            def _validator(v: Any, path: AttributePath) -> None:
                if not isinstance(v, list):
                    _validation_type_error(path, "The attribute must be a list")
                for i, v in enumerate(v):
                    key_mapper.ensure_type(v, path[i])

            list_mapper = (
                map_each_element(key_mapper.mapper)
                if key_mapper.mapper is not None
                else None
            )

            return AttributeTypeHandler(
                f"List of {key_mapper.describe_type()}",
                _validator,
                base_type=list,
                mapper=type_normalizer,
            ).combine_mapper(list_mapper)
        if is_typeddict(provided_type):
            subparser = self.generate_parser(cast("Type[TD]", provided_type))
            return AttributeTypeHandler(
                description=f"{provided_type.__name__} (Typed Mapping)",
                ensure_type=lambda v, ap: None,
                base_type=dict,
                mapper=lambda v, ap, cv: subparser.parse_input(
                    v, ap, parser_context=cv
                ),
            )
        if t_orig == dict:
            if not t_args or len(t_args) != 2:
                raise ValueError(
                    f'The attribute "{attribute}" is Dict but does not have Generics (Must use Dict[str, Y])'
                )
            if t_args[0] != str:
                raise ValueError(
                    f'The attribute "{attribute}" is Dict and has a non-str type as key.'
                    " Currently, only `str` is supported (Dict[str, Y])"
                )
            key_mapper = self._as_type_validator(
                attribute,
                t_args[0],
                parsing_typed_dict_attribute,
            )
            value_mapper = self._as_type_validator(
                attribute,
                t_args[1],
                parsing_typed_dict_attribute,
            )

            if key_mapper.base_type is None:
                raise ValueError(
                    f'The attribute "{attribute}" is Dict and the key did not have a trivial base type.  Key types'
                    f" without trivial base types (such as `str`) are not supported at the moment."
                )

            if value_mapper.mapper is not None:
                raise ValueError(
                    f'The attribute "{attribute}" is Dict and the value requires mapping.'
                    " Currently, this is not supported. Consider a simpler type (such as Dict[str, str] or Dict[str, Any])."
                    " Better typing may come later"
                )

            def _validator(uv: Any, path: AttributePath) -> None:
                if not isinstance(uv, dict):
                    _validation_type_error(path, "The attribute must be a mapping")
                key_name = "the first key in the mapping"
                for i, (k, v) in enumerate(uv.items()):
                    if not key_mapper.base_type_match(k):
                        kp = path.copy_with_path_hint(key_name)
                        _validation_type_error(
                            kp,
                            f'The key number {i + 1} in attribute "{kp}" must be a {key_mapper.describe_type()}',
                        )
                    key_name = f"the key after {k}"
                    value_mapper.ensure_type(v, path[k])

            return AttributeTypeHandler(
                f"Mapping of {value_mapper.describe_type()}",
                _validator,
                base_type=dict,
                mapper=type_normalizer,
            ).combine_mapper(key_mapper.mapper)
        if t_orig == Union:
            if _is_two_arg_x_list_x(t_args):
                # Force the order to be "X, List[X]" as it simplifies the code
                x_list_x = (
                    t_args if get_origin(t_args[1]) == list else (t_args[1], t_args[0])
                )

                # X, List[X] could match if X was List[Y].  However, our code below assumes
                # that X is a non-list.  The `_is_two_arg_x_list_x` returns False for this
                # case to avoid this assert and fall into the "generic case".
                assert get_origin(x_list_x[0]) != list
                x_subtype_checker = self._as_type_validator(
                    attribute,
                    x_list_x[0],
                    parsing_typed_dict_attribute,
                )
                list_x_subtype_checker = self._as_type_validator(
                    attribute,
                    x_list_x[1],
                    parsing_typed_dict_attribute,
                )
                type_description = x_subtype_checker.describe_type()
                type_description = f"{type_description} or a list of {type_description}"

                def _validator(v: Any, path: AttributePath) -> None:
                    if isinstance(v, list):
                        list_x_subtype_checker.ensure_type(v, path)
                    else:
                        x_subtype_checker.ensure_type(v, path)

                return AttributeTypeHandler(
                    type_description,
                    _validator,
                    mapper=type_normalizer,
                )
            else:
                subtype_checker = [
                    self._as_type_validator(attribute, a, parsing_typed_dict_attribute)
                    for a in t_args
                ]
                type_description = "one-of: " + ", ".join(
                    f"{sc.describe_type()}" for sc in subtype_checker
                )
                mapper = subtype_checker[0].mapper
                if any(mapper != sc.mapper for sc in subtype_checker):
                    raise ValueError(
                        f'Cannot handle the union "{provided_type}" as the target types need different'
                        " type normalization/mapping logic.  Unions are generally limited to Union[X, List[X]]"
                        " where X is a non-collection type."
                    )

                def _validator(v: Any, path: AttributePath) -> None:
                    partial_matches = []
                    for sc in subtype_checker:
                        try:
                            sc.ensure_type(v, path)
                            return
                        except ManifestParseException as e:
                            if sc.base_type_match(v):
                                partial_matches.append((sc, e))

                    if len(partial_matches) == 1:
                        raise partial_matches[0][1]
                    _validation_type_error(
                        path, f"Could not match against: {type_description}"
                    )

                return AttributeTypeHandler(
                    type_description,
                    _validator,
                    mapper=type_normalizer,
                )
        if t_orig == Literal:
            # We want "x" for string values; repr provides 'x'
            pretty = ", ".join(
                f'"{v}"' if isinstance(v, str) else str(v) for v in t_args
            )

            def _validator(v: Any, path: AttributePath) -> None:
                if v not in t_args:
                    value_hint = ""
                    if isinstance(v, str):
                        value_hint = f"({v}) "
                    _validation_type_error(
                        path,
                        f"Value {value_hint}must be one of the following literal values: {pretty}",
                    )

            return AttributeTypeHandler(
                f"One of the following literal values: {pretty}",
                _validator,
            )

        if provided_type == Any:
            return AttributeTypeHandler(
                "any (unvalidated)",
                lambda *a: None,
            )
        raise ValueError(
            f'The attribute "{attribute}" had/contained a type {provided_type}, which is not supported'
        )

    def _parse_types(
        self,
        spec: Type[TypedDict],
        allow_target_attribute_annotation: bool = False,
        allow_source_attribute_annotations: bool = False,
        forbid_optional: bool = True,
    ) -> Dict[str, AttributeDescription]:
        annotations = get_type_hints(spec, include_extras=True)
        return {
            k: self._attribute_description(
                k,
                t,
                k in spec.__required_keys__,
                allow_target_attribute_annotation=allow_target_attribute_annotation,
                allow_source_attribute_annotations=allow_source_attribute_annotations,
                forbid_optional=forbid_optional,
            )
            for k, t in annotations.items()
        }

    def _attribute_description(
        self,
        attribute: str,
        orig_td: Any,
        is_required: bool,
        forbid_optional: bool = True,
        allow_target_attribute_annotation: bool = False,
        allow_source_attribute_annotations: bool = False,
    ) -> AttributeDescription:
        td, anno, is_optional = _parse_type(
            attribute, orig_td, forbid_optional=forbid_optional
        )
        type_validator = self._as_type_validator(attribute, td, True)
        parsed_annotations = DetectedDebputyParseHint.parse_annotations(
            anno,
            f' Seen with attribute "{attribute}".',
            attribute,
            is_required,
            allow_target_attribute_annotation=allow_target_attribute_annotation,
            allow_source_attribute_annotations=allow_source_attribute_annotations,
        )
        return AttributeDescription(
            target_attribute=parsed_annotations.target_attribute,
            attribute_type=td,
            type_validator=type_validator,
            annotations=anno,
            is_optional=is_optional,
            conflicting_attributes=parsed_annotations.conflict_with_source_attributes,
            conditional_required=parsed_annotations.conditional_required,
            source_attribute_name=assume_not_none(
                parsed_annotations.source_manifest_attribute
            ),
            parse_hints=parsed_annotations,
        )

    def _parse_alt_form(
        self,
        alt_form,
        default_target_attribute: Optional[str],
    ) -> AttributeDescription:
        td, anno, is_optional = _parse_type(
            "source_format alternative form",
            alt_form,
            forbid_optional=True,
            parsing_typed_dict_attribute=False,
        )
        type_validator = self._as_type_validator(
            "source_format alternative form",
            td,
            True,
        )
        parsed_annotations = DetectedDebputyParseHint.parse_annotations(
            anno,
            " The alternative for source_format.",
            None,
            False,
            default_target_attribute=default_target_attribute,
            allow_target_attribute_annotation=True,
            allow_source_attribute_annotations=False,
        )
        return AttributeDescription(
            target_attribute=parsed_annotations.target_attribute,
            attribute_type=td,
            type_validator=type_validator,
            annotations=anno,
            is_optional=is_optional,
            conflicting_attributes=parsed_annotations.conflict_with_source_attributes,
            conditional_required=parsed_annotations.conditional_required,
            source_attribute_name="Alt form of the source_format",
        )

    def _union_narrowing(
        self,
        input_type: Any,
        target_type: Any,
        parsing_typed_dict_attribute: bool,
    ) -> Optional[Callable[[Any, AttributePath, Optional["ParserContextData"]], Any]]:
        _, input_orig, input_args = unpack_type(
            input_type, parsing_typed_dict_attribute
        )
        _, target_orig, target_args = unpack_type(
            target_type, parsing_typed_dict_attribute
        )

        if input_orig != Union or not input_args:
            raise ValueError("input_type must be a Union[...] with non-empty args")

        # Currently, we only support Union[X, List[X]] -> List[Y] narrowing or Union[X, List[X]] -> Union[Y, Union[Y]]
        # - Where X = Y or there is a simple standard transformation from X to Y.

        if target_orig not in (Union, list) or not target_args:
            # Not supported
            return None

        if target_orig == Union and set(input_args) == set(target_args):
            # Not needed (identity mapping)
            return None

        if target_orig == list and not any(get_origin(a) == list for a in input_args):
            # Not supported
            return None

        target_arg = target_args[0]
        simplified_type = self._strip_mapped_types(
            target_arg, parsing_typed_dict_attribute
        )
        acceptable_types = {
            target_arg,
            List[target_arg],  # type: ignore
            simplified_type,
            List[simplified_type],  # type: ignore
        }
        target_format = (
            target_arg,
            List[target_arg],  # type: ignore
        )
        in_target_format = 0
        in_simple_format = 0
        for input_arg in input_args:
            if input_arg not in acceptable_types:
                # Not supported
                return None
            if input_arg in target_format:
                in_target_format += 1
            else:
                in_simple_format += 1

        assert in_simple_format or in_target_format

        if in_target_format and not in_simple_format:
            # Union[X, List[X]] -> List[X]
            return normalize_into_list
        mapped = self._registered_types[target_arg]
        if not in_target_format and in_simple_format:
            # Union[X, List[X]] -> List[Y]

            def _mapper_x_list_y(
                x: Union[Any, List[Any]],
                ap: AttributePath,
                pc: Optional["ParserContextData"],
            ) -> List[Any]:
                in_list_form: List[Any] = normalize_into_list(x, ap, pc)

                return [mapped.mapper(x, ap, pc) for x in in_list_form]

            return _mapper_x_list_y

        # Union[Y, List[X]] -> List[Y]
        if not isinstance(target_arg, type):
            raise ValueError(
                f"Cannot narrow {input_type} -> {target_type}: The automatic conversion does"
                f" not support mixed types.  Please use either {simplified_type} or {target_arg}"
                f" in the source content (but both a mix of both)"
            )

        def _mapper_mixed_list_y(
            x: Union[Any, List[Any]],
            ap: AttributePath,
            pc: Optional["ParserContextData"],
        ) -> List[Any]:
            in_list_form: List[Any] = normalize_into_list(x, ap, pc)

            return [
                x if isinstance(x, target_arg) else mapped.mapper(x, ap, pc)
                for x in in_list_form
            ]

        return _mapper_mixed_list_y

    def _type_normalize(
        self,
        attribute: str,
        input_type: Any,
        target_type: Any,
        parsing_typed_dict_attribute: bool,
    ) -> Optional[Callable[[Any, AttributePath, Optional["ParserContextData"]], Any]]:
        if input_type == target_type:
            return None
        _, input_orig, input_args = unpack_type(
            input_type, parsing_typed_dict_attribute
        )
        _, target_orig, target_args = unpack_type(
            target_type,
            parsing_typed_dict_attribute,
        )
        if input_orig == Union:
            result = self._union_narrowing(
                input_type, target_type, parsing_typed_dict_attribute
            )
            if result:
                return result
        elif target_orig == list and target_args[0] == input_type:
            return wrap_into_list

        mapped = self._registered_types.get(target_type)
        if mapped is not None and input_type == mapped.source_type:
            # Source -> Target
            return mapped.mapper
        if target_orig == list and target_args:
            mapped = self._registered_types.get(target_args[0])
            if mapped is not None:
                # mypy is dense and forgot `mapped` cannot be optional in the comprehensions.
                mapped_type: TypeMapping = mapped
                if input_type == mapped.source_type:
                    # Source -> List[Target]
                    return lambda x, ap, pc: [mapped_type.mapper(x, ap, pc)]
                if (
                    input_orig == list
                    and input_args
                    and input_args[0] == mapped_type.source_type
                ):
                    # List[Source] -> List[Target]
                    return lambda xs, ap, pc: [
                        mapped_type.mapper(x, ap, pc) for x in xs
                    ]

        raise ValueError(
            f'Unsupported type normalization for "{attribute}": Cannot automatically map/narrow'
            f" {input_type} to {target_type}"
        )

    def _strip_mapped_types(
        self, orig_td: Any, parsing_typed_dict_attribute: bool
    ) -> Any:
        m = self._registered_types.get(orig_td)
        if m is not None:
            return m.source_type
        _, v, args = unpack_type(orig_td, parsing_typed_dict_attribute)
        if v == list:
            arg = args[0]
            m = self._registered_types.get(arg)
            if m:
                return List[m.source_type]  # type: ignore
        if v == Union:
            stripped_args = tuple(
                self._strip_mapped_types(x, parsing_typed_dict_attribute) for x in args
            )
            if stripped_args != args:
                return Union[stripped_args]
        return orig_td


def _sort_key(attr: StandardParserAttributeDocumentation) -> Any:
    key = next(iter(attr.attributes))
    return attr.sort_category, key


def _apply_std_docs(
    std_doc_table: Optional[
        Mapping[Type[Any], Sequence[StandardParserAttributeDocumentation]]
    ],
    source_format_typed_dict: Type[Any],
    attribute_docs: Optional[Sequence[ParserAttributeDocumentation]],
) -> Optional[Sequence[ParserAttributeDocumentation]]:
    if std_doc_table is None or not std_doc_table:
        return attribute_docs

    has_docs_for = set()
    if attribute_docs:
        for attribute_doc in attribute_docs:
            has_docs_for.update(attribute_doc.attributes)

    base_seen = set()
    std_docs_used = []

    remaining_bases = set(getattr(source_format_typed_dict, "__orig_bases__", []))
    base_seen.update(remaining_bases)
    while remaining_bases:
        base = remaining_bases.pop()
        new_bases_to_check = {
            x for x in getattr(base, "__orig_bases__", []) if x not in base_seen
        }
        remaining_bases.update(new_bases_to_check)
        base_seen.update(new_bases_to_check)
        std_docs = std_doc_table.get(base)
        if std_docs:
            for std_doc in std_docs:
                if any(a in has_docs_for for a in std_doc.attributes):
                    # If there is any overlap, do not add the docs
                    continue
                has_docs_for.update(std_doc.attributes)
                std_docs_used.append(std_doc)

    if not std_docs_used:
        return attribute_docs
    docs = sorted(std_docs_used, key=_sort_key)
    if attribute_docs:
        # Plugin provided attributes first
        c = list(attribute_docs)
        c.extend(docs)
        docs = c
    return tuple(docs)


def _verify_and_auto_correct_inline_reference_documentation(
    parsed_content: Type[TD],
    source_typed_dict: Type[Any],
    source_content_attributes: Mapping[str, AttributeDescription],
    inline_reference_documentation: Optional[ParserDocumentation],
    has_alt_form: bool,
    automatic_docs: Optional[
        Mapping[Type[Any], Sequence[StandardParserAttributeDocumentation]]
    ] = None,
) -> Optional[ParserDocumentation]:
    orig_attribute_docs = (
        inline_reference_documentation.attribute_doc
        if inline_reference_documentation
        else None
    )
    attribute_docs = _apply_std_docs(
        automatic_docs,
        source_typed_dict,
        orig_attribute_docs,
    )
    if inline_reference_documentation is None and attribute_docs is None:
        return None
    changes = {}
    if attribute_docs:
        seen = set()
        had_any_custom_docs = False
        for attr_doc in attribute_docs:
            if not isinstance(attr_doc, StandardParserAttributeDocumentation):
                had_any_custom_docs = True
            for attr_name in attr_doc.attributes:
                attr = source_content_attributes.get(attr_name)
                if attr is None:
                    raise ValueError(
                        f"The inline_reference_documentation for the source format of {parsed_content.__qualname__}"
                        f' references an attribute "{attr_name}", which does not exist in the source format.'
                    )
                if attr_name in seen:
                    raise ValueError(
                        f"The inline_reference_documentation for the source format of {parsed_content.__qualname__}"
                        f' has documentation for "{attr_name}" twice, which is not supported.'
                        f" Please document it at most once"
                    )
                seen.add(attr_name)
        undocumented = source_content_attributes.keys() - seen
        if undocumented:
            if had_any_custom_docs:
                undocumented_attrs = ", ".join(undocumented)
                raise ValueError(
                    f"The following attributes were not documented for the source format of"
                    f" {parsed_content.__qualname__}.  If this is deliberate, then please"
                    ' declare each them as undocumented (via undocumented_attr("foo")):'
                    f" {undocumented_attrs}"
                )
            combined_docs = list(attribute_docs)
            combined_docs.extend(undocumented_attr(a) for a in sorted(undocumented))
            attribute_docs = combined_docs

    if attribute_docs and orig_attribute_docs != attribute_docs:
        assert attribute_docs is not None
        changes["attribute_doc"] = tuple(attribute_docs)

    if (
        inline_reference_documentation is not None
        and inline_reference_documentation.alt_parser_description
        and not has_alt_form
    ):
        raise ValueError(
            "The inline_reference_documentation had documentation for an non-mapping format,"
            " but the source format does not have a non-mapping format."
        )
    if changes:
        if inline_reference_documentation is None:
            inline_reference_documentation = reference_documentation()
        return inline_reference_documentation.replace(**changes)
    return inline_reference_documentation


def _check_conflicts(
    input_content_attributes: Dict[str, AttributeDescription],
    required_attributes: FrozenSet[str],
    all_attributes: FrozenSet[str],
) -> None:
    for attr_name, attr in input_content_attributes.items():
        if attr_name in required_attributes and attr.conflicting_attributes:
            c = ", ".join(repr(a) for a in attr.conflicting_attributes)
            raise ValueError(
                f'The attribute "{attr_name}" is required and conflicts with the attributes: {c}.'
                " This makes it impossible to use these attributes. Either remove the attributes"
                f' (along with the conflicts for them), adjust the conflicts or make "{attr_name}"'
                " optional (NotRequired)"
            )
        else:
            required_conflicts = attr.conflicting_attributes & required_attributes
            if required_conflicts:
                c = ", ".join(repr(a) for a in required_conflicts)
                raise ValueError(
                    f'The attribute "{attr_name}" conflicts with the following *required* attributes: {c}.'
                    f' This makes it impossible to use the "{attr_name}" attribute. Either remove it,'
                    f" adjust the conflicts or make the listed attributes optional (NotRequired)"
                )
        unknown_attributes = attr.conflicting_attributes - all_attributes
        if unknown_attributes:
            c = ", ".join(repr(a) for a in unknown_attributes)
            raise ValueError(
                f'The attribute "{attr_name}" declares a conflict with the following unknown attributes: {c}.'
                f" None of these attributes were declared in the input."
            )


def _check_attributes(
    content: Type[TypedDict],
    input_content: Type[TypedDict],
    input_content_attributes: Dict[str, AttributeDescription],
    sources: Mapping[str, Collection[str]],
) -> None:
    target_required_keys = content.__required_keys__
    input_required_keys = input_content.__required_keys__
    all_input_keys = input_required_keys | input_content.__optional_keys__

    for input_name in all_input_keys:
        attr = input_content_attributes[input_name]
        target_name = attr.target_attribute
        source_names = sources[target_name]
        input_is_required = input_name in input_required_keys
        target_is_required = target_name in target_required_keys

        assert source_names

        if input_is_required and len(source_names) > 1:
            raise ValueError(
                f'The source attribute "{input_name}" is required, but it maps to "{target_name}",'
                f' which has multiple sources "{source_names}". If "{input_name}" should be required,'
                f' then there is no need for additional sources for "{target_name}". Alternatively,'
                f' "{input_name}" might be missing a NotRequired type'
                f' (example: "{input_name}: NotRequired[<OriginalTypeHere>]")'
            )
        if not input_is_required and target_is_required and len(source_names) == 1:
            raise ValueError(
                f'The source attribute "{input_name}" is not marked as required and maps to'
                f' "{target_name}", which is marked as required. As there are no other attributes'
                f' mapping to "{target_name}", then "{input_name}" must be required as well'
                f' ("{input_name}: Required[<Type>]"). Alternatively, "{target_name}" should be optional'
                f' ("{target_name}: NotRequired[<Type>]") or an "MappingHint.aliasOf" might be missing.'
            )


def _validation_type_error(path: AttributePath, message: str) -> None:
    raise ManifestParseException(
        f'The attribute "{path.path}" did not have a valid structure/type: {message}'
    )


def _is_two_arg_x_list_x(t_args: Tuple[Any, ...]) -> bool:
    if len(t_args) != 2:
        return False
    lhs, rhs = t_args
    if get_origin(lhs) == list:
        if get_origin(rhs) == list:
            # It could still match X, List[X] - but we do not allow this case for now as the caller
            # does not support it.
            return False
        l_args = get_args(lhs)
        return bool(l_args and l_args[0] == rhs)
    if get_origin(rhs) == list:
        r_args = get_args(rhs)
        return bool(r_args and r_args[0] == lhs)
    return False


def _extract_typed_dict(
    base_type,
    default_target_attribute: Optional[str],
) -> Tuple[Optional[Type[TypedDict]], Any]:
    if is_typeddict(base_type):
        return base_type, None
    _, origin, args = unpack_type(base_type, False)
    if origin != Union:
        if isinstance(base_type, type) and issubclass(base_type, (dict, Mapping)):
            raise ValueError(
                "The source_format cannot be nor contain a (non-TypedDict) dict"
            )
        return None, base_type
    typed_dicts = [x for x in args if is_typeddict(x)]
    if len(typed_dicts) > 1:
        raise ValueError(
            "When source_format is a Union, it must contain at most one TypedDict"
        )
    typed_dict = typed_dicts[0] if typed_dicts else None

    if any(x is None or x is _NONE_TYPE for x in args):
        raise ValueError(
            "The source_format cannot be nor contain Optional[X] or Union[X, None]"
        )

    if any(
        isinstance(x, type) and issubclass(x, (dict, Mapping))
        for x in args
        if x is not typed_dict
    ):
        raise ValueError(
            "The source_format cannot be nor contain a (non-TypedDict) dict"
        )
    remaining = [x for x in args if x is not typed_dict]
    has_target_attribute = False
    anno = None
    if len(remaining) == 1:
        base_type, anno, _ = _parse_type(
            "source_format alternative form",
            remaining[0],
            forbid_optional=True,
            parsing_typed_dict_attribute=False,
        )
        has_target_attribute = bool(anno) and any(
            isinstance(x, TargetAttribute) for x in anno
        )
        target_type = base_type
    else:
        target_type = Union[tuple(remaining)]

    if default_target_attribute is None and not has_target_attribute:
        raise ValueError(
            'The alternative format must be Union[TypedDict,Annotated[X, DebputyParseHint.target_attribute("...")]]'
            " OR the parsed_content format must have exactly one attribute that is required."
        )
    if anno:
        final_anno = [target_type]
        final_anno.extend(anno)
        return typed_dict, Annotated[tuple(final_anno)]
    return typed_dict, target_type


def _dispatch_parse_generator(
    dispatch_type: Type[DebputyDispatchableType],
) -> Callable[[Any, AttributePath, Optional["ParserContextData"]], Any]:
    def _dispatch_parse(
        value: Any,
        attribute_path: AttributePath,
        parser_context: Optional["ParserContextData"],
    ):
        assert parser_context is not None
        dispatching_parser = parser_context.dispatch_parser_table_for(dispatch_type)
        return dispatching_parser.parse_input(
            value, attribute_path, parser_context=parser_context
        )

    return _dispatch_parse


def _dispatch_parser(
    dispatch_type: Type[DebputyDispatchableType],
) -> AttributeTypeHandler:
    return AttributeTypeHandler(
        dispatch_type.__name__,
        lambda *a: None,
        mapper=_dispatch_parse_generator(dispatch_type),
    )


def _parse_type(
    attribute: str,
    orig_td: Any,
    forbid_optional: bool = True,
    parsing_typed_dict_attribute: bool = True,
) -> Tuple[Any, Tuple[Any, ...], bool]:
    td, v, args = unpack_type(orig_td, parsing_typed_dict_attribute)
    md: Tuple[Any, ...] = tuple()
    optional = False
    if v is not None:
        if v == Annotated:
            anno = get_args(td)
            md = anno[1:]
            td, v, args = unpack_type(anno[0], parsing_typed_dict_attribute)

        if td is _NONE_TYPE:
            raise ValueError(
                f'The attribute "{attribute}" resolved to type "None".  "Nil" / "None" fields are not allowed in the'
                " debputy manifest, so this attribute does not make sense in its current form."
            )
        if forbid_optional and v == Union and any(a is _NONE_TYPE for a in args):
            raise ValueError(
                f'Detected use of Optional in "{attribute}", which is not allowed here.'
                " Please use NotRequired for optional fields"
            )

    return td, md, optional


def _normalize_attribute_name(attribute: str) -> str:
    if attribute.endswith("_"):
        attribute = attribute[:-1]
    return attribute.replace("_", "-")


@dataclasses.dataclass
class DetectedDebputyParseHint:
    target_attribute: str
    source_manifest_attribute: Optional[str]
    conflict_with_source_attributes: FrozenSet[str]
    conditional_required: Optional[ConditionalRequired]
    applicable_as_path_hint: bool

    @classmethod
    def parse_annotations(
        cls,
        anno: Tuple[Any, ...],
        error_context: str,
        default_attribute_name: Optional[str],
        is_required: bool,
        default_target_attribute: Optional[str] = None,
        allow_target_attribute_annotation: bool = False,
        allow_source_attribute_annotations: bool = False,
    ) -> "DetectedDebputyParseHint":
        target_attr_anno = find_annotation(anno, TargetAttribute)
        if target_attr_anno:
            if not allow_target_attribute_annotation:
                raise ValueError(
                    f"The DebputyParseHint.target_attribute annotation is not allowed in this context.{error_context}"
                )
            target_attribute = target_attr_anno.attribute
        elif default_target_attribute is not None:
            target_attribute = default_target_attribute
        elif default_attribute_name is not None:
            target_attribute = default_attribute_name
        else:
            if default_attribute_name is None:
                raise ValueError(
                    "allow_target_attribute_annotation must be True OR "
                    "default_attribute_name/default_target_attribute must be not None"
                )
            raise ValueError(
                f"Missing DebputyParseHint.target_attribute annotation.{error_context}"
            )
        source_attribute_anno = find_annotation(anno, ManifestAttribute)
        _source_attribute_allowed(
            allow_source_attribute_annotations, error_context, source_attribute_anno
        )
        if source_attribute_anno:
            source_attribute_name = source_attribute_anno.attribute
        elif default_attribute_name is not None:
            source_attribute_name = _normalize_attribute_name(default_attribute_name)
        else:
            source_attribute_name = None
        mutual_exclusive_with_anno = find_annotation(anno, ConflictWithSourceAttribute)
        if mutual_exclusive_with_anno:
            _source_attribute_allowed(
                allow_source_attribute_annotations,
                error_context,
                mutual_exclusive_with_anno,
            )
            conflicting_attributes = mutual_exclusive_with_anno.conflicting_attributes
        else:
            conflicting_attributes = frozenset()
        conditional_required = find_annotation(anno, ConditionalRequired)

        if conditional_required and is_required:
            if default_attribute_name is None:
                raise ValueError(
                    f"is_required cannot be True without default_attribute_name being not None"
                )
            raise ValueError(
                f'The attribute "{default_attribute_name}" is Required while also being conditionally required.'
                ' Please make the attribute "NotRequired" or remove the conditional requirement.'
            )

        not_path_hint_anno = find_annotation(anno, NotPathHint)
        applicable_as_path_hint = not_path_hint_anno is None

        return DetectedDebputyParseHint(
            target_attribute=target_attribute,
            source_manifest_attribute=source_attribute_name,
            conflict_with_source_attributes=conflicting_attributes,
            conditional_required=conditional_required,
            applicable_as_path_hint=applicable_as_path_hint,
        )


def _source_attribute_allowed(
    source_attribute_allowed: bool,
    error_context: str,
    annotation: Optional[DebputyParseHint],
) -> None:
    if source_attribute_allowed or annotation is None:
        return
    raise ValueError(
        f'The annotation "{annotation}" cannot be used here. {error_context}'
    )
