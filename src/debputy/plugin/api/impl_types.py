import dataclasses
import os.path
from typing import (
    Optional,
    Callable,
    FrozenSet,
    Dict,
    List,
    Tuple,
    Generic,
    TYPE_CHECKING,
    TypeVar,
    cast,
    Any,
    Sequence,
    Union,
    Type,
    TypedDict,
    Iterable,
    Mapping,
    NotRequired,
    Literal,
    Set,
    Iterator,
    Container,
    Protocol,
)
from weakref import ref

from debputy.exceptions import (
    DebputyFSIsROError,
    PluginAPIViolationError,
    PluginConflictError,
    UnhandledOrUnexpectedErrorFromPluginError,
    PluginBaseError,
    PluginInitializationError,
)
from debputy.filesystem_scan import as_path_def
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.manifest_parser.tagging_types import DebputyParsedContent, TypeMapping
from debputy.manifest_parser.util import AttributePath, check_integration_mode
from debputy.packages import BinaryPackage
from debputy.plugin.api import (
    VirtualPath,
    BinaryCtrlAccessor,
    PackageProcessingContext,
)
from debputy.plugin.api.spec import (
    DebputyPluginInitializer,
    MetadataAutoDetector,
    DpkgTriggerType,
    ParserDocumentation,
    PackageProcessor,
    PathDef,
    ParserAttributeDocumentation,
    undocumented_attr,
    documented_attr,
    reference_documentation,
    PackagerProvidedFileReferenceDocumentation,
    TypeMappingDocumentation,
    DebputyIntegrationMode,
)
from debputy.plugin.plugin_state import (
    run_in_context_of_plugin,
)
from debputy.substitution import VariableContext
from debputy.util import _normalize_path, package_cross_check_precheck

if TYPE_CHECKING:
    from debputy.plugin.api.spec import (
        ServiceDetector,
        ServiceIntegrator,
    )
    from debputy.manifest_parser.parser_data import ParserContextData
    from debputy.highlevel_manifest import (
        HighLevelManifest,
        PackageTransformationDefinition,
        BinaryPackageData,
    )
    from debputy.plugin.debputy.to_be_api_types import (
        BuildSystemRule,
        BuildRuleParsedFormat,
    )


TD = TypeVar("TD", bound="Union[DebputyParsedContent, List[DebputyParsedContent]]")
PF = TypeVar("PF")
SF = TypeVar("SF")
TP = TypeVar("TP")
TTP = Type[TP]
BSR = TypeVar("BSR", bound="BuildSystemRule")

DIPKWHandler = Callable[[str, AttributePath, "ParserContextData"], TP]
DIPHandler = Callable[[str, PF, AttributePath, "ParserContextData"], TP]


@dataclasses.dataclass(slots=True)
class DebputyPluginMetadata:
    plugin_name: str
    api_compat_version: int
    plugin_loader: Optional[Callable[[], Callable[["DebputyPluginInitializer"], None]]]
    plugin_initializer: Optional[Callable[["DebputyPluginInitializer"], None]]
    plugin_path: str
    _is_initialized: bool = False

    @property
    def is_bundled(self) -> bool:
        return self.plugin_path == "<bundled>"

    @property
    def is_loaded(self) -> bool:
        return self.plugin_initializer is not None

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    def initialize_plugin(self, api: "DebputyPluginInitializer") -> None:
        if self.is_initialized:
            raise RuntimeError("Cannot load plugins twice")
        if not self.is_loaded:
            self.load_plugin()
        plugin_initializer = self.plugin_initializer
        assert plugin_initializer is not None
        plugin_initializer(api)
        self._is_initialized = True

    def load_plugin(self) -> None:
        plugin_loader = self.plugin_loader
        assert plugin_loader is not None
        try:
            self.plugin_initializer = run_in_context_of_plugin(
                self.plugin_name,
                plugin_loader,
            )
        except PluginBaseError:
            raise
        except Exception as e:
            raise PluginInitializationError(
                f"Initialization of {self.plugin_name} failed due to its initializer raising an exception"
            ) from e
        assert self.plugin_initializer is not None


@dataclasses.dataclass(slots=True, frozen=True)
class PluginProvidedParser(Generic[PF, TP]):
    parser: "DeclarativeInputParser[PF]"
    handler: Callable[[str, PF, "AttributePath", "ParserContextData"], TP]
    plugin_metadata: DebputyPluginMetadata

    def parse(
        self,
        name: str,
        value: object,
        attribute_path: "AttributePath",
        *,
        parser_context: "ParserContextData",
    ) -> TP:
        parsed_value = self.parser.parse_input(
            value, attribute_path, parser_context=parser_context
        )
        return self.handler(name, parsed_value, attribute_path, parser_context)


class PPFFormatParam(TypedDict):
    priority: Optional[int]
    name: str
    owning_package: str


@dataclasses.dataclass(slots=True, frozen=True)
class PackagerProvidedFileClassSpec:
    debputy_plugin_metadata: DebputyPluginMetadata
    stem: str
    installed_as_format: str
    default_mode: int
    default_priority: Optional[int]
    allow_name_segment: bool
    allow_architecture_segment: bool
    post_formatting_rewrite: Optional[Callable[[str], str]]
    packageless_is_fallback_for_all_packages: bool
    reservation_only: bool
    formatting_callback: Optional[Callable[[str, PPFFormatParam, VirtualPath], str]] = (
        None
    )
    reference_documentation: Optional[PackagerProvidedFileReferenceDocumentation] = None
    bug_950723: bool = False
    has_active_command: bool = True

    @property
    def supports_priority(self) -> bool:
        return self.default_priority is not None

    def compute_dest(
        self,
        assigned_name: str,
        # Note this method is currently used 1:1 inside plugin tests.
        *,
        owning_package: Optional[str] = None,
        assigned_priority: Optional[int] = None,
        path: Optional[VirtualPath] = None,
    ) -> Tuple[str, str]:
        if assigned_priority is not None and not self.supports_priority:
            raise ValueError(
                f"Cannot assign priority to packager provided files with stem"
                f' "{self.stem}" (e.g., "debian/foo.{self.stem}"). They'
                " do not use priority at all."
            )

        path_format = self.installed_as_format
        if self.supports_priority and assigned_priority is None:
            assigned_priority = self.default_priority

        if owning_package is None:
            owning_package = assigned_name

        params: PPFFormatParam = {
            "priority": assigned_priority,
            "name": assigned_name,
            "owning_package": owning_package,
        }

        if self.formatting_callback is not None:
            if path is None:
                raise ValueError(
                    "The path parameter is required for PPFs with formatting_callback"
                )
            dest_path = self.formatting_callback(path_format, params, path)
        else:
            dest_path = path_format.format(**params)

        dirname, basename = os.path.split(dest_path)
        dirname = _normalize_path(dirname)

        if self.post_formatting_rewrite:
            basename = self.post_formatting_rewrite(basename)
        return dirname, basename


@dataclasses.dataclass(slots=True)
class MetadataOrMaintscriptDetector:
    plugin_metadata: DebputyPluginMetadata
    detector_id: str
    detector: MetadataAutoDetector
    applies_to_package_types: FrozenSet[str]
    enabled: bool = True

    def applies_to(self, binary_package: BinaryPackage) -> bool:
        return binary_package.package_type in self.applies_to_package_types

    def run_detector(
        self,
        fs_root: "VirtualPath",
        ctrl: "BinaryCtrlAccessor",
        context: "PackageProcessingContext",
    ) -> None:
        try:
            self.detector(fs_root, ctrl, context)
        except DebputyFSIsROError as e:
            nv = self.plugin_metadata.plugin_name
            raise PluginAPIViolationError(
                f'The plugin {nv} violated the API contract for "metadata detectors"'
                " by attempting to mutate the provided file system in its metadata detector"
                f" with id {self.detector_id}.  File system mutation is *not* supported at"
                " this stage (file system layout is committed and the attempted changes"
                " would be lost)."
            ) from e
        except UnhandledOrUnexpectedErrorFromPluginError as e:
            e.add_note(
                f"The exception was raised by the detector with the ID: {self.detector_id}"
            )


class DeclarativeInputParser(Generic[TD]):
    @property
    def inline_reference_documentation(self) -> Optional[ParserDocumentation]:
        return None

    @property
    def expected_debputy_integration_mode(
        self,
    ) -> Optional[Container[DebputyIntegrationMode]]:
        return None

    @property
    def reference_documentation_url(self) -> Optional[str]:
        doc = self.inline_reference_documentation
        return doc.documentation_reference_url if doc is not None else None

    def parse_input(
        self,
        value: object,
        path: "AttributePath",
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        raise NotImplementedError


class DelegatingDeclarativeInputParser(DeclarativeInputParser[TD]):
    __slots__ = (
        "delegate",
        "_reference_documentation",
        "_expected_debputy_integration_mode",
    )

    def __init__(
        self,
        delegate: DeclarativeInputParser[TD],
        *,
        inline_reference_documentation: Optional[ParserDocumentation] = None,
        expected_debputy_integration_mode: Optional[
            Container[DebputyIntegrationMode]
        ] = None,
    ) -> None:
        self.delegate = delegate
        self._reference_documentation = inline_reference_documentation
        self._expected_debputy_integration_mode = expected_debputy_integration_mode

    @property
    def expected_debputy_integration_mode(
        self,
    ) -> Optional[Container[DebputyIntegrationMode]]:
        return self._expected_debputy_integration_mode

    @property
    def inline_reference_documentation(self) -> Optional[ParserDocumentation]:
        doc = self._reference_documentation
        if doc is None:
            return self.delegate.inline_reference_documentation
        return doc


class ListWrappedDeclarativeInputParser(DelegatingDeclarativeInputParser[TD]):
    __slots__ = ()

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
        path: "AttributePath",
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        check_integration_mode(
            path, parser_context, self._expected_debputy_integration_mode
        )
        if not isinstance(value, list):
            doc_ref = self._doc_url_error_suffix(see_url_version=True)
            raise ManifestParseException(
                f"The attribute {path.path} must be a list.{doc_ref}"
            )
        result = []
        delegate = self.delegate
        for idx, element in enumerate(value):
            element_path = path[idx]
            result.append(
                delegate.parse_input(
                    element,
                    element_path,
                    parser_context=parser_context,
                )
            )
        return result


class DispatchingParserBase(Generic[TP]):
    def __init__(self, manifest_attribute_path_template: str) -> None:
        self.manifest_attribute_path_template = manifest_attribute_path_template
        self._parsers: Dict[str, PluginProvidedParser[Any, TP]] = {}

    def is_known_keyword(self, keyword: str) -> bool:
        return keyword in self._parsers

    def registered_keywords(self) -> Iterable[str]:
        yield from self._parsers

    def parser_for(self, keyword: str) -> PluginProvidedParser[Any, TP]:
        return self._parsers[keyword]

    def register_keyword(
        self,
        keyword: Union[str, Sequence[str]],
        handler: DIPKWHandler,
        plugin_metadata: DebputyPluginMetadata,
        *,
        inline_reference_documentation: Optional[ParserDocumentation] = None,
    ) -> None:
        reference_documentation_url = None
        if inline_reference_documentation:
            if inline_reference_documentation.attribute_doc:
                raise ValueError(
                    "Cannot provide per-attribute documentation for a value-less keyword!"
                )
            if inline_reference_documentation.alt_parser_description:
                raise ValueError(
                    "Cannot provide non-mapping-format documentation for a value-less keyword!"
                )
            reference_documentation_url = (
                inline_reference_documentation.documentation_reference_url
            )
        parser = DeclarativeValuelessKeywordInputParser(
            inline_reference_documentation,
            documentation_reference=reference_documentation_url,
        )

        def _combined_handler(
            name: str,
            _ignored: Any,
            attr_path: AttributePath,
            context: "ParserContextData",
        ) -> TP:
            return handler(name, attr_path, context)

        p = PluginProvidedParser(
            parser,
            _combined_handler,
            plugin_metadata,
        )

        self._add_parser(keyword, p)

    def register_parser(
        self,
        keyword: Union[str, List[str]],
        parser: "DeclarativeInputParser[PF]",
        handler: Callable[[str, PF, "AttributePath", "ParserContextData"], TP],
        plugin_metadata: DebputyPluginMetadata,
    ) -> None:
        p = PluginProvidedParser(
            parser,
            handler,
            plugin_metadata,
        )
        self._add_parser(keyword, p)

    def _add_parser(
        self,
        keyword: Union[str, Iterable[str]],
        ppp: "PluginProvidedParser[PF, TP]",
    ) -> None:
        ks = [keyword] if isinstance(keyword, str) else keyword
        for k in ks:
            existing_parser = self._parsers.get(k)
            if existing_parser is not None:
                message = (
                    f'The rule name "{k}" is already taken by the plugin'
                    f" {existing_parser.plugin_metadata.plugin_name}. This conflict was triggered"
                    f" when plugin {ppp.plugin_metadata.plugin_name} attempted to register its parser."
                )
                raise PluginConflictError(
                    message,
                    existing_parser.plugin_metadata,
                    ppp.plugin_metadata,
                )
            self._new_parser(k, ppp)

    def _new_parser(self, keyword: str, ppp: "PluginProvidedParser[PF, TP]") -> None:
        self._parsers[keyword] = ppp

    def parse_input(
        self,
        orig_value: object,
        attribute_path: "AttributePath",
        *,
        parser_context: "ParserContextData",
    ) -> TP:
        raise NotImplementedError


class DispatchingObjectParser(
    DispatchingParserBase[Mapping[str, Any]],
    DeclarativeInputParser[Mapping[str, Any]],
):
    def __init__(
        self,
        manifest_attribute_path_template: str,
        *,
        parser_documentation: Optional[ParserDocumentation] = None,
        expected_debputy_integration_mode: Optional[
            Container[DebputyIntegrationMode]
        ] = None,
    ) -> None:
        super().__init__(manifest_attribute_path_template)
        self._attribute_documentation: List[ParserAttributeDocumentation] = []
        if parser_documentation is None:
            parser_documentation = reference_documentation()
        self._parser_documentation = parser_documentation
        self._expected_debputy_integration_mode = expected_debputy_integration_mode

    @property
    def expected_debputy_integration_mode(
        self,
    ) -> Optional[Container[DebputyIntegrationMode]]:
        return self._expected_debputy_integration_mode

    @property
    def reference_documentation_url(self) -> Optional[str]:
        return self._parser_documentation.documentation_reference_url

    @property
    def inline_reference_documentation(self) -> Optional[ParserDocumentation]:
        ref_doc = self._parser_documentation
        return reference_documentation(
            title=ref_doc.title,
            description=ref_doc.description,
            attributes=self._attribute_documentation,
            reference_documentation_url=self.reference_documentation_url,
        )

    def _new_parser(self, keyword: str, ppp: "PluginProvidedParser[PF, TP]") -> None:
        super()._new_parser(keyword, ppp)
        doc = ppp.parser.inline_reference_documentation
        if doc is None or doc.description is None:
            self._attribute_documentation.append(undocumented_attr(keyword))
        else:
            self._attribute_documentation.append(
                documented_attr(keyword, doc.description)
            )

    def register_child_parser(
        self,
        keyword: str,
        parser: "DispatchingObjectParser",
        plugin_metadata: DebputyPluginMetadata,
        *,
        on_end_parse_step: Optional[
            Callable[
                [str, Optional[Mapping[str, Any]], AttributePath, "ParserContextData"],
                None,
            ]
        ] = None,
        nested_in_package_context: bool = False,
    ) -> None:
        def _handler(
            name: str,
            value: Mapping[str, Any],
            path: AttributePath,
            parser_context: "ParserContextData",
        ) -> Mapping[str, Any]:
            on_end_parse_step(name, value, path, parser_context)
            return value

        if nested_in_package_context:
            parser = InPackageContextParser(
                keyword,
                parser,
            )

        p = PluginProvidedParser(
            parser,
            _handler,
            plugin_metadata,
        )
        self._add_parser(keyword, p)

    def parse_input(
        self,
        orig_value: object,
        attribute_path: "AttributePath",
        *,
        parser_context: "ParserContextData",
    ) -> TP:
        check_integration_mode(
            attribute_path,
            parser_context,
            self._expected_debputy_integration_mode,
        )
        doc_ref = ""
        if self.reference_documentation_url is not None:
            doc_ref = (
                f" Please see {self.reference_documentation_url} for the documentation."
            )
        if not isinstance(orig_value, dict):
            raise ManifestParseException(
                f"The attribute {attribute_path.path_container_lc} must be a non-empty mapping.{doc_ref}"
            )
        if not orig_value:
            raise ManifestParseException(
                f"The attribute {attribute_path.path_container_lc} must be a non-empty mapping.{doc_ref}"
            )
        result = {}
        unknown_keys = orig_value.keys() - self._parsers.keys()
        if unknown_keys:
            first_key = next(iter(unknown_keys))
            remaining_valid_attributes = self._parsers.keys() - orig_value.keys()
            if not remaining_valid_attributes:
                raise ManifestParseException(
                    f'The attribute "{first_key}" is not applicable at {attribute_path.path} (with the'
                    f" current set of plugins).{doc_ref}"
                )
            remaining_valid_attribute_names = ", ".join(remaining_valid_attributes)
            raise ManifestParseException(
                f'The attribute "{first_key}" is not applicable at {attribute_path.path} (with the current set'
                " of plugins). Possible attributes available (and not already used) are:"
                f" {remaining_valid_attribute_names}.{doc_ref}"
            )
        # Parse order is important for the root level (currently we use rule registration order)
        for key, provided_parser in self._parsers.items():
            value = orig_value.get(key)
            if value is None:
                if isinstance(provided_parser.parser, DispatchingObjectParser):
                    provided_parser.handler(
                        key,
                        {},
                        attribute_path[key],
                        parser_context,
                    )
                continue
            value_path = attribute_path[key]
            if provided_parser is None:
                valid_keys = ", ".join(sorted(self._parsers.keys()))
                raise ManifestParseException(
                    f'Unknown or unsupported option "{key}" at {value_path.path}.'
                    " Valid options at this location are:"
                    f" {valid_keys}\n{doc_ref}"
                )
            parsed_value = provided_parser.parse(
                key, value, value_path, parser_context=parser_context
            )
            result[key] = parsed_value
        return result


@dataclasses.dataclass(slots=True, frozen=True)
class PackageContextData(Generic[TP]):
    resolved_package_name: str
    value: TP


class InPackageContextParser(
    DelegatingDeclarativeInputParser[Mapping[str, PackageContextData[TP]]]
):
    __slots__ = ()

    def __init__(
        self,
        manifest_attribute_path_template: str,
        delegate: DeclarativeInputParser[TP],
        *,
        parser_documentation: Optional[ParserDocumentation] = None,
    ) -> None:
        self.manifest_attribute_path_template = manifest_attribute_path_template
        self._attribute_documentation: List[ParserAttributeDocumentation] = []
        super().__init__(delegate, inline_reference_documentation=parser_documentation)

    def parse_input(
        self,
        orig_value: object,
        attribute_path: "AttributePath",
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TP:
        assert parser_context is not None
        check_integration_mode(
            attribute_path,
            parser_context,
            self._expected_debputy_integration_mode,
        )
        doc_ref = ""
        if self.reference_documentation_url is not None:
            doc_ref = (
                f" Please see {self.reference_documentation_url} for the documentation."
            )
        if not isinstance(orig_value, dict) or not orig_value:
            raise ManifestParseException(
                f"The attribute {attribute_path.path_container_lc} must be a non-empty mapping.{doc_ref}"
            )
        delegate = self.delegate
        result = {}
        for package_name_raw, value in orig_value.items():

            definition_source = attribute_path[package_name_raw]
            package_name = package_name_raw
            if "{{" in package_name:
                package_name = parser_context.substitution.substitute(
                    package_name_raw,
                    definition_source.path,
                )
            package_state: PackageTransformationDefinition
            with parser_context.binary_package_context(package_name) as package_state:
                if package_state.is_auto_generated_package:
                    # Maybe lift (part) of this restriction.
                    raise ManifestParseException(
                        f'Cannot define rules for package "{package_name}" (at {definition_source.path}). It is an'
                        " auto-generated package."
                    )
                parsed_value = delegate.parse_input(
                    value, definition_source, parser_context=parser_context
                )
                result[package_name_raw] = PackageContextData(
                    package_name, parsed_value
                )
        return result


class DispatchingTableParser(
    DispatchingParserBase[TP],
    DeclarativeInputParser[TP],
):
    def __init__(self, base_type: TTP, manifest_attribute_path_template: str) -> None:
        super().__init__(manifest_attribute_path_template)
        self.base_type = base_type

    def parse_input(
        self,
        orig_value: object,
        attribute_path: "AttributePath",
        *,
        parser_context: "ParserContextData",
    ) -> TP:
        if isinstance(orig_value, str):
            key = orig_value
            value = None
            value_path = attribute_path
        elif isinstance(orig_value, dict):
            if len(orig_value) != 1:
                valid_keys = ", ".join(sorted(self._parsers.keys()))
                raise ManifestParseException(
                    f'The mapping "{attribute_path.path}" had two keys, but it should only have one top level key.'
                    " Maybe you are missing a list marker behind the second key or some indentation.  The"
                    f" possible keys are: {valid_keys}"
                )
            key, value = next(iter(orig_value.items()))
            value_path = attribute_path[key]
        else:
            raise ManifestParseException(
                f"The attribute {attribute_path.path} must be a string or a mapping."
            )
        provided_parser = self._parsers.get(key)
        if provided_parser is None:
            valid_keys = ", ".join(sorted(self._parsers.keys()))
            raise ManifestParseException(
                f'Unknown or unsupported action "{key}" at {value_path.path}.'
                " Valid actions at this location are:"
                f" {valid_keys}"
            )
        return provided_parser.parse(
            key, value, value_path, parser_context=parser_context
        )


@dataclasses.dataclass(slots=True)
class DeclarativeValuelessKeywordInputParser(DeclarativeInputParser[None]):
    inline_reference_documentation: Optional[ParserDocumentation] = None
    documentation_reference: Optional[str] = None

    def parse_input(
        self,
        value: object,
        path: "AttributePath",
        *,
        parser_context: Optional["ParserContextData"] = None,
    ) -> TD:
        if value is None:
            return cast("TD", value)
        if self.documentation_reference is not None:
            doc_ref = f" (Documentation: {self.documentation_reference})"
        else:
            doc_ref = ""
        raise ManifestParseException(
            f"Expected attribute {path.path} to be a string.{doc_ref}"
        )


@dataclasses.dataclass(slots=True)
class PluginProvidedManifestVariable:
    plugin_metadata: DebputyPluginMetadata
    variable_name: str
    variable_value: Optional[Union[str, Callable[[VariableContext], str]]]
    is_context_specific_variable: bool
    variable_reference_documentation: Optional[str] = None
    is_documentation_placeholder: bool = False
    is_for_special_case: bool = False

    @property
    def is_internal(self) -> bool:
        return self.variable_name.startswith("_") or ":_" in self.variable_name

    @property
    def is_token(self) -> bool:
        return self.variable_name.startswith("token:")

    def resolve(self, variable_context: VariableContext) -> str:
        value_resolver = self.variable_value
        if isinstance(value_resolver, str):
            res = value_resolver
        else:
            res = value_resolver(variable_context)
        return res


@dataclasses.dataclass(slots=True, frozen=True)
class AutomaticDiscardRuleExample:
    content: Sequence[Tuple[PathDef, bool]]
    description: Optional[str] = None


def automatic_discard_rule_example(
    *content: Union[str, PathDef, Tuple[Union[str, PathDef], bool]],
    example_description: Optional[str] = None,
) -> AutomaticDiscardRuleExample:
    """Provide an example for an automatic discard rule

    The return value of this method should be passed to the `examples` parameter of
    `automatic_discard_rule` method - either directly for a single example or as a
    part of a sequence of examples.

    >>> # Possible example for an exclude rule for ".la" files
    >>> # Example shows two files; The ".la" file that will be removed and another file that
    >>> # will be kept.
    >>> automatic_discard_rule_example(   # doctest: +ELLIPSIS
    ...     "usr/lib/libfoo.la",
    ...     ("usr/lib/libfoo.so.1.0.0", False),
    ... )
    AutomaticDiscardRuleExample(...)

    Keep in mind that you have to explicitly include directories that are relevant for the test
    if you want them shown. Also, if a directory is excluded, all path beneath it will be
    automatically excluded in the example as well. Your example data must account for that.

    >>> # Possible example for python cache file discard rule
    >>> # In this example, we explicitly list the __pycache__ directory itself because we
    >>> # want it shown in the output (otherwise, we could have omitted it)
    >>> automatic_discard_rule_example(   # doctest: +ELLIPSIS
    ...     (".../foo.py", False),
    ...     ".../__pycache__/",
    ...     ".../__pycache__/...",
    ...     ".../foo.pyc",
    ...     ".../foo.pyo",
    ... )
    AutomaticDiscardRuleExample(...)

    Note: Even if `__pycache__` had been implicit, the result would have been the same. However,
    the rendered example would not have shown the directory on its own.  The use of `...` as
    path names is useful for denoting "anywhere" or "anything". Though, there is nothing "magic"
    about this name - it happens to be allowed as a path name (unlike `.` or `..`).

    These examples can be seen via `debputy plugin show automatic-discard-rules <name-here>`.

    :param content: The content of the example.  Each element can be either a path definition or
      a tuple of a path definition followed by a verdict (boolean). Each provided path definition
      describes the paths to be presented in the example. Implicit paths such as parent
      directories will be created but not shown in the example.  Therefore, if a directory is
      relevant to the example, be sure to explicitly list it.

      The verdict associated with a path determines whether the path should be discarded (when
      True) or kept (when False). When a path is not explicitly associated with a verdict, the
      verdict is assumed to be discarded (True).
    :param example_description: An optional description displayed together with the example.
    :return: An opaque data structure containing the example.
    """
    example = []
    for d in content:
        if not isinstance(d, tuple):
            pd = d
            verdict = True
        else:
            pd, verdict = d

        path_def = as_path_def(pd)
        example.append((path_def, verdict))

    if not example:
        raise ValueError("At least one path must be given for an example")

    return AutomaticDiscardRuleExample(
        tuple(example),
        description=example_description,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class PluginProvidedPackageProcessor:
    processor_id: str
    applies_to_package_types: FrozenSet[str]
    package_processor: PackageProcessor
    dependencies: FrozenSet[Tuple[str, str]]
    plugin_metadata: DebputyPluginMetadata

    def applies_to(self, binary_package: BinaryPackage) -> bool:
        return binary_package.package_type in self.applies_to_package_types

    @property
    def dependency_id(self) -> Tuple[str, str]:
        return self.plugin_metadata.plugin_name, self.processor_id

    def run_package_processor(
        self,
        fs_root: "VirtualPath",
        unused: None,
        context: "PackageProcessingContext",
    ) -> None:
        self.package_processor(fs_root, unused, context)


@dataclasses.dataclass(slots=True, frozen=True)
class PluginProvidedDiscardRule:
    name: str
    plugin_metadata: DebputyPluginMetadata
    discard_check: Callable[[VirtualPath], bool]
    reference_documentation: Optional[str]
    examples: Sequence[AutomaticDiscardRuleExample] = tuple()

    def should_discard(self, path: VirtualPath) -> bool:
        return self.discard_check(path)


@dataclasses.dataclass(slots=True, frozen=True)
class ServiceManagerDetails:
    service_manager: str
    service_detector: "ServiceDetector"
    service_integrator: "ServiceIntegrator"
    plugin_metadata: DebputyPluginMetadata


ReferenceValue = TypedDict(
    "ReferenceValue",
    {
        "description": str,
    },
)


def _reference_data_value(
    *,
    description: str,
) -> ReferenceValue:
    return {
        "description": description,
    }


KnownPackagingFileCategories = Literal[
    "generated",
    "generic-template",
    "ppf-file",
    "ppf-control-file",
    "maint-config",
    "pkg-metadata",
    "pkg-helper-config",
    "testing",
    "lint-config",
]
KNOWN_PACKAGING_FILE_CATEGORY_DESCRIPTIONS: Mapping[
    KnownPackagingFileCategories, ReferenceValue
] = {
    "generated": _reference_data_value(
        description="The file is (likely) generated from another file"
    ),
    "generic-template": _reference_data_value(
        description="The file is (likely) a generic template that generates a known packaging file. While the"
        " file is annotated as if it was the target file, the file might uses a custom template"
        " language inside it."
    ),
    "ppf-file": _reference_data_value(
        description="Packager provided file to be installed on the file system - usually as-is."
        " When `install-pattern` or `install-path` are provided, this is where the file is installed."
    ),
    "ppf-control-file": _reference_data_value(
        description="Packager provided file that becomes a control file - possible after processing. "
        " If `install-pattern` or `install-path` are provided, they denote where the is placed"
        " (generally, this will be of the form `DEBIAN/<name>`)"
    ),
    "maint-config": _reference_data_value(
        description="Maintenance configuration for a specific tool that the maintainer uses (tool / style preferences)"
    ),
    "pkg-metadata": _reference_data_value(
        description="The file is related to standard package metadata (usually documented in Debian Policy)"
    ),
    "pkg-helper-config": _reference_data_value(
        description="The file is packaging helper configuration or instruction file"
    ),
    "testing": _reference_data_value(
        description="The file is related to automated testing (autopkgtests, salsa/gitlab CI)."
    ),
    "lint-config": _reference_data_value(
        description="The file is related to a linter (such as overrides for false-positives or style preferences)"
    ),
}

KnownPackagingConfigFeature = Literal[
    "dh-filearray",
    "dh-filedoublearray",
    "dh-hash-subst",
    "dh-dollar-subst",
    "dh-glob",
    "dh-partial-glob",
    "dh-late-glob",
    "dh-glob-after-execute",
    "dh-executable-config",
    "dh-custom-format",
    "dh-file-list",
    "dh-install-list",
    "dh-install-list-dest-dir-like-dh_install",
    "dh-install-list-fixed-dest-dir",
    "dh-fixed-dest-dir",
    "dh-exec-rename",
    "dh-docs-only",
]

KNOWN_PACKAGING_FILE_CONFIG_FEATURE_DESCRIPTION: Mapping[
    KnownPackagingConfigFeature, ReferenceValue
] = {
    "dh-filearray": _reference_data_value(
        description="The file will be read as a list of space/newline separated tokens",
    ),
    "dh-filedoublearray": _reference_data_value(
        description="Each line in the file will be read as a list of space-separated tokens",
    ),
    "dh-hash-subst": _reference_data_value(
        description="Supports debhelper #PACKAGE# style substitutions (udebs often excluded)",
    ),
    "dh-dollar-subst": _reference_data_value(
        description="Supports debhelper ${PACKAGE} style substitutions (usually requires compat 13+)",
    ),
    "dh-glob": _reference_data_value(
        description="Supports standard debhelper globing",
    ),
    "dh-partial-glob": _reference_data_value(
        description="Supports standard debhelper globing but only to a subset of the values (implies dh-late-glob)",
    ),
    "dh-late-glob": _reference_data_value(
        description="Globbing is done separately instead of using the built-in function",
    ),
    "dh-glob-after-execute": _reference_data_value(
        description="When the dh config file is executable, the generated output will be subject to globbing",
    ),
    "dh-executable-config": _reference_data_value(
        description="If marked executable, debhelper will execute the file and read its output",
    ),
    "dh-custom-format": _reference_data_value(
        description="The dh tool will or may have a custom parser for this file",
    ),
    "dh-file-list": _reference_data_value(
        description="The dh file contains a list of paths to be processed",
    ),
    "dh-install-list": _reference_data_value(
        description="The dh file contains a list of paths/globs to be installed but the tool specific knowledge"
        " required to understand the file cannot be conveyed via this interface.",
    ),
    "dh-install-list-dest-dir-like-dh_install": _reference_data_value(
        description="The dh file is processed similar to dh_install (notably dest-dir handling derived"
        " from the path or the last token on the line)",
    ),
    "dh-install-list-fixed-dest-dir": _reference_data_value(
        description="The dh file is an install list and the dest-dir is always the same for all patterns"
        " (when `install-pattern` or `install-path` are provided, they identify the directory - not the file location)",
    ),
    "dh-exec-rename": _reference_data_value(
        description="When `dh-exec` is the interpreter of this dh config file, its renaming (=>) feature can be"
        " requested/used",
    ),
    "dh-docs-only": _reference_data_value(
        description="The dh config file is used for documentation only. Implicit <!nodocs> Build-Profiles support",
    ),
}

CONFIG_FEATURE_ALIASES: Dict[
    KnownPackagingConfigFeature, List[Tuple[KnownPackagingConfigFeature, int]]
] = {
    "dh-filearray": [
        ("dh-filearray", 0),
        ("dh-executable-config", 9),
        ("dh-dollar-subst", 13),
    ],
    "dh-filedoublearray": [
        ("dh-filedoublearray", 0),
        ("dh-executable-config", 9),
        ("dh-dollar-subst", 13),
    ],
}


def _implies(
    features: List[KnownPackagingConfigFeature],
    seen: Set[KnownPackagingConfigFeature],
    implying: Sequence[KnownPackagingConfigFeature],
    implied: KnownPackagingConfigFeature,
) -> None:
    if implied in seen:
        return
    if all(f in seen for f in implying):
        seen.add(implied)
        features.append(implied)


def expand_known_packaging_config_features(
    compat_level: int,
    features: List[KnownPackagingConfigFeature],
) -> List[KnownPackagingConfigFeature]:
    final_features: List[KnownPackagingConfigFeature] = []
    seen = set()
    for feature in features:
        expanded = CONFIG_FEATURE_ALIASES.get(feature)
        if not expanded:
            expanded = [(feature, 0)]
        for v, c in expanded:
            if compat_level < c or v in seen:
                continue
            seen.add(v)
            final_features.append(v)
    if "dh-glob" in seen and "dh-late-glob" in seen:
        final_features.remove("dh-glob")

    _implies(final_features, seen, ["dh-partial-glob"], "dh-late-glob")
    _implies(
        final_features,
        seen,
        ["dh-late-glob", "dh-executable-config"],
        "dh-glob-after-execute",
    )
    return sorted(final_features)


class InstallPatternDHCompatRule(DebputyParsedContent):
    install_pattern: NotRequired[str]
    add_config_features: NotRequired[List[KnownPackagingConfigFeature]]
    starting_with_compat_level: NotRequired[int]


class KnownPackagingFileInfo(DebputyParsedContent):
    # Exposed directly in the JSON plugin parsing; be careful with changes
    path: NotRequired[str]
    pkgfile: NotRequired[str]
    detection_method: NotRequired[Literal["path", "dh.pkgfile"]]
    file_categories: NotRequired[List[KnownPackagingFileCategories]]
    documentation_uris: NotRequired[List[str]]
    debputy_cmd_templates: NotRequired[List[List[str]]]
    debhelper_commands: NotRequired[List[str]]
    config_features: NotRequired[List[KnownPackagingConfigFeature]]
    install_pattern: NotRequired[str]
    dh_compat_rules: NotRequired[List[InstallPatternDHCompatRule]]
    default_priority: NotRequired[int]
    post_formatting_rewrite: NotRequired[Literal["period-to-underscore"]]
    packageless_is_fallback_for_all_packages: NotRequired[bool]
    has_active_command: NotRequired[bool]


@dataclasses.dataclass(slots=True)
class PluginProvidedKnownPackagingFile:
    info: KnownPackagingFileInfo
    detection_method: Literal["path", "dh.pkgfile"]
    detection_value: str
    plugin_metadata: DebputyPluginMetadata


class BuildSystemAutoDetector(Protocol):

    def __call__(self, source_root: VirtualPath, *args: Any, **kwargs: Any) -> bool: ...


@dataclasses.dataclass(slots=True, frozen=True)
class PluginProvidedTypeMapping:
    mapped_type: TypeMapping[Any, Any]
    reference_documentation: Optional[TypeMappingDocumentation]
    plugin_metadata: DebputyPluginMetadata


@dataclasses.dataclass(slots=True, frozen=True)
class PluginProvidedBuildSystemAutoDetection(Generic[BSR]):
    manifest_keyword: str
    build_system_rule_type: Type[BSR]
    detector: BuildSystemAutoDetector
    constructor: Callable[
        ["BuildRuleParsedFormat", AttributePath, "HighLevelManifest"],
        BSR,
    ]
    auto_detection_shadow_build_systems: FrozenSet[str]
    plugin_metadata: DebputyPluginMetadata


class PackageDataTable:
    def __init__(self, package_data_table: Mapping[str, "BinaryPackageData"]) -> None:
        self._package_data_table = package_data_table
        # This is enabled for metadata-detectors. But it is deliberate not enabled for package processors,
        # because it is not clear how it should interact with dependencies. For metadata-detectors, things
        # read-only and there are no dependencies, so we cannot "get them wrong".
        self.enable_cross_package_checks = False

    def __iter__(self) -> Iterator["BinaryPackageData"]:
        return iter(self._package_data_table.values())

    def __getitem__(self, item: str) -> "BinaryPackageData":
        return self._package_data_table[item]

    def __contains__(self, item: str) -> bool:
        return item in self._package_data_table


class PackageProcessingContextProvider(PackageProcessingContext):
    __slots__ = (
        "_manifest",
        "_binary_package",
        "_related_udeb_package",
        "_package_data_table",
        "_cross_check_cache",
    )

    def __init__(
        self,
        manifest: "HighLevelManifest",
        binary_package: BinaryPackage,
        related_udeb_package: Optional[BinaryPackage],
        package_data_table: PackageDataTable,
    ) -> None:
        self._manifest = manifest
        self._binary_package = binary_package
        self._related_udeb_package = related_udeb_package
        self._package_data_table = ref(package_data_table)
        self._cross_check_cache: Optional[
            Sequence[Tuple[BinaryPackage, "VirtualPath"]]
        ] = None

    def _package_state_for(
        self,
        package: BinaryPackage,
    ) -> "PackageTransformationDefinition":
        return self._manifest.package_state_for(package.name)

    def _package_version_for(
        self,
        package: BinaryPackage,
    ) -> str:
        package_state = self._package_state_for(package)
        version = package_state.binary_version
        if version is not None:
            return version
        return self._manifest.source_version(
            include_binnmu_version=not package.is_arch_all
        )

    @property
    def binary_package(self) -> BinaryPackage:
        return self._binary_package

    @property
    def related_udeb_package(self) -> Optional[BinaryPackage]:
        return self._related_udeb_package

    @property
    def binary_package_version(self) -> str:
        return self._package_version_for(self._binary_package)

    @property
    def related_udeb_package_version(self) -> Optional[str]:
        udeb = self._related_udeb_package
        if udeb is None:
            return None
        return self._package_version_for(udeb)

    def accessible_package_roots(self) -> Iterable[Tuple[BinaryPackage, "VirtualPath"]]:
        package_table = self._package_data_table()
        if package_table is None:
            raise ReferenceError(
                "Internal error: package_table was garbage collected too early"
            )
        if not package_table.enable_cross_package_checks:
            raise PluginAPIViolationError(
                "Cross package content checks are not available at this time."
            )
        cache = self._cross_check_cache
        if cache is None:
            matches = []
            pkg = self.binary_package
            for pkg_data in package_table:
                if pkg_data.binary_package.name == pkg.name:
                    continue
                res = package_cross_check_precheck(pkg, pkg_data.binary_package)
                if not res[0]:
                    continue
                matches.append((pkg_data.binary_package, pkg_data.fs_root))
            cache = tuple(matches) if matches else tuple()
            self._cross_check_cache = cache
        return cache


@dataclasses.dataclass(slots=True, frozen=True)
class PluginProvidedTrigger:
    dpkg_trigger_type: DpkgTriggerType
    dpkg_trigger_target: str
    provider: DebputyPluginMetadata
    provider_source_id: str

    def serialized_format(self) -> str:
        return f"{self.dpkg_trigger_type} {self.dpkg_trigger_target}"
