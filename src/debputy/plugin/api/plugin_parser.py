from typing import NotRequired, List, Any, TypedDict

from debputy.manifest_parser.tagging_types import (
    DebputyParsedContent,
    TypeMapping,
)
from debputy.manifest_parser.base_types import OctalMode
from debputy.manifest_parser.declarative_parser import ParserGenerator
from debputy.plugin.api.impl_types import KnownPackagingFileInfo


class PPFReferenceDocumentation(TypedDict):
    description: NotRequired[str]
    format_documentation_uris: NotRequired[List[str]]


class PackagerProvidedFileJsonDescription(DebputyParsedContent):
    stem: str
    installed_path: str
    default_mode: NotRequired[OctalMode]
    default_priority: NotRequired[int]
    allow_name_segment: NotRequired[bool]
    allow_architecture_segment: NotRequired[bool]
    reference_documentation: NotRequired[PPFReferenceDocumentation]


class ManifestVariableJsonDescription(DebputyParsedContent):
    name: str
    value: str
    reference_documentation: NotRequired[str]


class PluginJsonMetadata(DebputyParsedContent):
    api_compat_version: int
    module: NotRequired[str]
    plugin_initializer: NotRequired[str]
    packager_provided_files: NotRequired[List[Any]]
    manifest_variables: NotRequired[List[Any]]
    known_packaging_files: NotRequired[List[Any]]


def _initialize_plugin_metadata_parser_generator() -> ParserGenerator:
    pc = ParserGenerator()
    pc.register_mapped_type(
        TypeMapping(
            OctalMode,
            str,
            lambda v, ap, _: OctalMode.parse_filesystem_mode(v, ap),
        )
    )
    return pc


PLUGIN_METADATA_PARSER_GENERATOR = _initialize_plugin_metadata_parser_generator()
PLUGIN_METADATA_PARSER = PLUGIN_METADATA_PARSER_GENERATOR.generate_parser(
    PluginJsonMetadata
)
PLUGIN_PPF_PARSER = PLUGIN_METADATA_PARSER_GENERATOR.generate_parser(
    PackagerProvidedFileJsonDescription
)
PLUGIN_MANIFEST_VARS_PARSER = PLUGIN_METADATA_PARSER_GENERATOR.generate_parser(
    ManifestVariableJsonDescription
)
PLUGIN_KNOWN_PACKAGING_FILES_PARSER = PLUGIN_METADATA_PARSER_GENERATOR.generate_parser(
    KnownPackagingFileInfo
)
