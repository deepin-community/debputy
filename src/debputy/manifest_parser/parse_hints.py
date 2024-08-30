import dataclasses
from typing import (
    NotRequired,
    TypedDict,
    TYPE_CHECKING,
    Callable,
    FrozenSet,
    Annotated,
    List,
)

from debputy.manifest_parser.util import (
    resolve_package_type_selectors,
    _ALL_PACKAGE_TYPES,
)
from debputy.plugin.api.spec import PackageTypeSelector

if TYPE_CHECKING:
    from debputy.manifest_parser.parser_data import ParserContextData


class DebputyParseHint:
    @classmethod
    def target_attribute(cls, target_attribute: str) -> "DebputyParseHint":
        """Define this source attribute to have a different target attribute name

        As an example:

            >>> from debputy.manifest_parser.declarative_parser import ParserGenerator
            >>> class SourceType(TypedDict):
            ...     source: Annotated[NotRequired[str], DebputyParseHint.target_attribute("sources")]
            ...     sources: NotRequired[List[str]]
            >>> class TargetType(TypedDict):
            ...     sources: List[str]
            >>> pg = ParserGenerator()
            >>> parser = pg.generate_parser(TargetType, source_content=SourceType)

        In this example, the user can provide either `source` or `sources` and the parser will
        map them to the `sources` attribute in the `TargetType`.  Note this example relies on
        the builtin mapping of `str` to `List[str]` to align the types between `source` (from
        SourceType) and `sources` (from TargetType).

        The following rules apply:

         * All source attributes that map to the same target attribute will be mutually exclusive
           (that is, the user cannot give `source` *and* `sources` as input).
         * When the target attribute is required, the source attributes are conditionally
           mandatory requiring the user to provide exactly one of them.
         * When multiple source attributes point to a single target attribute, none of the source
           attributes can be Required.
         * The annotation can only be used for the source type specification and the source type
           specification must be different from the target type specification.

        The `target_attribute` annotation can be used without having multiple source attributes. This
        can be useful if the source attribute name is not valid as a python variable identifier to
        rename it to a valid python identifier.

        :param target_attribute: The attribute name in the target content
        :return: The annotation.
        """
        return TargetAttribute(target_attribute)

    @classmethod
    def conflicts_with_source_attributes(
        cls,
        *conflicting_source_attributes: str,
    ) -> "DebputyParseHint":
        """Declare a conflict with one or more source attributes

        Example:

            >>> from debputy.manifest_parser.declarative_parser import ParserGenerator
            >>> class SourceType(TypedDict):
            ...     source: Annotated[NotRequired[str], DebputyParseHint.target_attribute("sources")]
            ...     sources: NotRequired[List[str]]
            ...     into_dir: NotRequired[str]
            ...     renamed_to: Annotated[
            ...         NotRequired[str],
            ...         DebputyParseHint.conflicts_with_source_attributes("sources", "into_dir")
            ... ]
            >>> class TargetType(TypedDict):
            ...     sources: List[str]
            ...     into_dir: NotRequired[str]
            ...     renamed_to: NotRequired[str]
            >>> pg = ParserGenerator()
            >>> parser = pg.generate_parser(TargetType, source_content=SourceType)

        In this example, if the user was to provide `renamed_to` with `sources` or `into_dir` the parser would report
        an error. However, the parser will allow `renamed_to` with `source` as the conflict is considered only for
        the input source. That is, it is irrelevant that `sources` and `source´ happens to "map" to the same target
        attribute.

        The following rules apply:
          * It is not possible for a target attribute to declare conflicts unless the target type spec is reused as
            source type spec.
          * All attributes involved in a conflict must be NotRequired.  If any of the attributes are Required, then
            the parser generator will reject the input.
          * All attributes listed in the conflict must be valid attributes in the source type spec.

        Note you do not have to specify conflicts between two attributes with the same target attribute name.  The
         `target_attribute` annotation will handle that for you.

        :param conflicting_source_attributes: All source attributes that cannot be used with this attribute.
        :return: The annotation.
        """
        if len(conflicting_source_attributes) < 1:
            raise ValueError(
                "DebputyParseHint.conflicts_with_source_attributes requires at least one attribute as input"
            )
        return ConflictWithSourceAttribute(frozenset(conflicting_source_attributes))

    @classmethod
    def required_when_single_binary(
        cls,
        *,
        package_type: PackageTypeSelector = _ALL_PACKAGE_TYPES,
    ) -> "DebputyParseHint":
        """Declare a source attribute as required when the source package produces exactly one binary package

        The attribute in question must always be declared as `NotRequired` in the TypedDict and this condition
        can only be used for source attributes.
        """
        resolved_package_types = resolve_package_type_selectors(package_type)
        reason = "The field is required for source packages producing exactly one binary package"
        if resolved_package_types != _ALL_PACKAGE_TYPES:
            types = ", ".join(sorted(resolved_package_types))
            reason += f" of type {types}"
            return ConditionalRequired(
                reason,
                lambda c: len(
                    [
                        p
                        for p in c.binary_packages.values()
                        if p.package_type in package_type
                    ]
                )
                == 1,
            )
        return ConditionalRequired(
            reason,
            lambda c: c.is_single_binary_package,
        )

    @classmethod
    def required_when_multi_binary(
        cls,
        *,
        package_type: PackageTypeSelector = _ALL_PACKAGE_TYPES,
    ) -> "DebputyParseHint":
        """Declare a source attribute as required when the source package produces two or more binary package

        The attribute in question must always be declared as `NotRequired` in the TypedDict and this condition
        can only be used for source attributes.
        """
        resolved_package_types = resolve_package_type_selectors(package_type)
        reason = "The field is required for source packages producing two or more binary packages"
        if resolved_package_types != _ALL_PACKAGE_TYPES:
            types = ", ".join(sorted(resolved_package_types))
            reason = (
                "The field is required for source packages producing not producing exactly one binary packages"
                f" of type {types}"
            )
            return ConditionalRequired(
                reason,
                lambda c: len(
                    [
                        p
                        for p in c.binary_packages.values()
                        if p.package_type in package_type
                    ]
                )
                != 1,
            )
        return ConditionalRequired(
            reason,
            lambda c: not c.is_single_binary_package,
        )

    @classmethod
    def manifest_attribute(cls, attribute: str) -> "DebputyParseHint":
        """Declare what the attribute name (as written in the manifest) should be

        By default, debputy will do an attribute normalizing that will take valid python identifiers such
        as `dest_dir` and remap it to the manifest variant (such as `dest-dir`) automatically.  If you have
        a special case, where this built-in normalization is insufficient or the python name is considerably
        different from what the user would write in the manifest, you can use this parse hint to set the
        name that the user would have to write in the manifest for this attribute.

            >>> from debputy.manifest_parser.base_types import FileSystemMatchRule, FileSystemExactMatchRule
            >>> class SourceType(TypedDict):
            ...     source: List[FileSystemMatchRule]
            ...     # Use "as" in the manifest because "as_" was not pretty enough
            ...     install_as: Annotated[NotRequired[FileSystemExactMatchRule], DebputyParseHint.manifest_attribute("as")]

        In this example, we use the parse hint to use "as" as the name in the manifest, because we cannot
        use "as" a valid python identifier (it is a keyword).  While debputy would map `as_` to `as` for us,
        we have chosen to use `install_as` as a python identifier.
        """
        return ManifestAttribute(attribute)

    @classmethod
    def not_path_error_hint(cls) -> "DebputyParseHint":
        """Mark this attribute as not a "path hint" when it comes to reporting errors

        By default, `debputy` will pick up attributes that uses path names (FileSystemMatchRule) as
        candidates for parse error hints (the little "<Search for: VALUE>" in error messages).

        Most rules only have one active path-based attribute and paths tends to be unique enough
        that it helps people spot the issue faster. However, in rare cases, you can have multiple
        attributes that fit the bill. In this case, this hint can be used to "hide" the suboptimal
        choice. As an example:

            >>> from debputy.manifest_parser.base_types import FileSystemMatchRule, FileSystemExactMatchRule
            >>> class SourceType(TypedDict):
            ...     source: List[FileSystemMatchRule]
            ...     install_as: Annotated[NotRequired[FileSystemExactMatchRule], DebputyParseHint.not_path_error_hint()]

        In this case, without the hint, `debputy` might pick up `install_as` as the attribute to
        use as hint for error reporting. However, here we have decided that we never want `install_as`
        leaving `source` as the only option.

        Generally, this type hint must be placed on the **source** format. Any source attribute matching
        the parsed format will be ignored.

        Mind the asymmetry: The annotation is placed in the **source** format while `debputy` looks at
        the type of the target attribute to determine if it counts as path.
        """
        return NOT_PATH_HINT


@dataclasses.dataclass(frozen=True, slots=True)
class TargetAttribute(DebputyParseHint):
    attribute: str


@dataclasses.dataclass(frozen=True, slots=True)
class ConflictWithSourceAttribute(DebputyParseHint):
    conflicting_attributes: FrozenSet[str]


@dataclasses.dataclass(frozen=True, slots=True)
class ConditionalRequired(DebputyParseHint):
    reason: str
    condition: Callable[["ParserContextData"], bool]

    def condition_applies(self, context: "ParserContextData") -> bool:
        return self.condition(context)


@dataclasses.dataclass(frozen=True, slots=True)
class ManifestAttribute(DebputyParseHint):
    attribute: str


class NotPathHint(DebputyParseHint):
    pass


NOT_PATH_HINT = NotPathHint()
