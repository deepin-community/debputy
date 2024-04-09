import dataclasses
import textwrap
from typing import Dict, List, Tuple, Sequence, Any

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.manifest_parser.declarative_parser import ParserGenerator
from debputy.plugin.api import reference_documentation
from debputy.plugin.api.impl_types import (
    DebputyPluginMetadata,
    PackagerProvidedFileClassSpec,
    MetadataOrMaintscriptDetector,
    TTP,
    DispatchingTableParser,
    TP,
    SUPPORTED_DISPATCHABLE_TABLE_PARSERS,
    DispatchingObjectParser,
    SUPPORTED_DISPATCHABLE_OBJECT_PARSERS,
    PluginProvidedManifestVariable,
    PluginProvidedPackageProcessor,
    PluginProvidedDiscardRule,
    ServiceManagerDetails,
    PluginProvidedKnownPackagingFile,
    PluginProvidedTypeMapping,
    OPARSER_PACKAGES,
    OPARSER_PACKAGES_ROOT,
)


def _initialize_parser_generator() -> ParserGenerator:
    pg = ParserGenerator()

    for path, ref_doc in SUPPORTED_DISPATCHABLE_OBJECT_PARSERS.items():
        pg.add_object_parser(path, parser_documentation=ref_doc)

    for rt, path in SUPPORTED_DISPATCHABLE_TABLE_PARSERS.items():
        pg.add_table_parser(rt, path)

    return pg


@dataclasses.dataclass(slots=True)
class PluginProvidedFeatureSet:
    plugin_data: Dict[str, DebputyPluginMetadata] = dataclasses.field(
        default_factory=dict
    )
    packager_provided_files: Dict[str, PackagerProvidedFileClassSpec] = (
        dataclasses.field(default_factory=dict)
    )
    metadata_maintscript_detectors: Dict[str, List[MetadataOrMaintscriptDetector]] = (
        dataclasses.field(default_factory=dict)
    )
    manifest_variables: Dict[str, PluginProvidedManifestVariable] = dataclasses.field(
        default_factory=dict
    )
    all_package_processors: Dict[Tuple[str, str], PluginProvidedPackageProcessor] = (
        dataclasses.field(default_factory=dict)
    )
    auto_discard_rules: Dict[str, PluginProvidedDiscardRule] = dataclasses.field(
        default_factory=dict
    )
    service_managers: Dict[str, ServiceManagerDetails] = dataclasses.field(
        default_factory=dict
    )
    known_packaging_files: Dict[str, PluginProvidedKnownPackagingFile] = (
        dataclasses.field(default_factory=dict)
    )
    mapped_types: Dict[Any, PluginProvidedTypeMapping] = dataclasses.field(
        default_factory=dict
    )
    manifest_parser_generator: ParserGenerator = dataclasses.field(
        default_factory=_initialize_parser_generator
    )

    def package_processors_in_order(self) -> Sequence[PluginProvidedPackageProcessor]:
        order = []
        delayed = []
        for plugin_processor in self.all_package_processors.values():
            if not plugin_processor.dependencies:
                order.append(plugin_processor)
            else:
                delayed.append(plugin_processor)

        # At the time of writing, insert order will work as a plugin cannot declare
        # dependencies out of order in the current version.  However, we want to
        # ensure dependencies are taken a bit seriously, so we ensure that processors
        # without dependencies are run first.  This should weed out anything that
        # needs dependencies but do not add them.
        #
        # It is still far from as any dependency issues will be hidden if you just
        # add a single dependency.
        order.extend(delayed)
        return order
