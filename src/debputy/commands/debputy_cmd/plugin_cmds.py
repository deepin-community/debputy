import argparse
import itertools
import operator
import os
import sys
from itertools import chain
from typing import (
    Sequence,
    Union,
    Tuple,
    Iterable,
    Any,
    Optional,
    Type,
    Mapping,
    Callable,
)

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.commands.debputy_cmd.context import (
    CommandContext,
    add_arg,
    ROOT_COMMAND,
)
from debputy.commands.debputy_cmd.dc_util import flatten_ppfs
from debputy.commands.debputy_cmd.output import (
    _stream_to_pager,
    _output_styling,
    OutputStylingBase,
)
from debputy.exceptions import DebputySubstitutionError
from debputy.filesystem_scan import build_virtual_fs
from debputy.manifest_parser.base_types import TypeMapping
from debputy.manifest_parser.declarative_parser import (
    DeclarativeMappingInputParser,
    DeclarativeNonMappingInputParser,
    BASIC_SIMPLE_TYPES,
)
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.parser_doc import render_rule
from debputy.manifest_parser.util import unpack_type, AttributePath
from debputy.packager_provided_files import detect_all_packager_provided_files
from debputy.plugin.api.example_processing import (
    process_discard_rule_example,
    DiscardVerdict,
)
from debputy.plugin.api.impl import plugin_metadata_for_debputys_own_plugin
from debputy.plugin.api.impl_types import (
    PackagerProvidedFileClassSpec,
    PluginProvidedManifestVariable,
    DispatchingParserBase,
    DeclarativeInputParser,
    DebputyPluginMetadata,
    DispatchingObjectParser,
    SUPPORTED_DISPATCHABLE_TABLE_PARSERS,
    OPARSER_MANIFEST_ROOT,
    PluginProvidedDiscardRule,
    AutomaticDiscardRuleExample,
    MetadataOrMaintscriptDetector,
    PluginProvidedTypeMapping,
)
from debputy.plugin.api.spec import (
    ParserDocumentation,
    reference_documentation,
    undocumented_attr,
    TypeMappingExample,
)
from debputy.substitution import Substitution
from debputy.util import _error, assume_not_none, _warn

plugin_dispatcher = ROOT_COMMAND.add_dispatching_subcommand(
    "plugin",
    "plugin_subcommand",
    default_subcommand="--help",
    help_description="Interact with debputy plugins",
    metavar="command",
)

plugin_list_cmds = plugin_dispatcher.add_dispatching_subcommand(
    "list",
    "plugin_subcommand_list",
    metavar="topic",
    default_subcommand="plugins",
    help_description="List plugins or things provided by plugins (unstable format)."
    " Pass `--help` *after* `list` get a topic listing",
)

plugin_show_cmds = plugin_dispatcher.add_dispatching_subcommand(
    "show",
    "plugin_subcommand_show",
    metavar="topic",
    help_description="Show details about a plugin or things provided by plugins (unstable format)."
    " Pass `--help` *after* `show` get a topic listing",
)


def format_output_arg(
    default_format: str,
    allowed_formats: Sequence[str],
    help_text: str,
) -> Callable[[argparse.ArgumentParser], None]:
    if default_format not in allowed_formats:
        raise ValueError("The default format must be in the allowed_formats...")

    def _configurator(argparser: argparse.ArgumentParser) -> None:
        argparser.add_argument(
            "--output-format",
            dest="output_format",
            default=default_format,
            choices=allowed_formats,
            help=help_text,
        )

    return _configurator


# To let --output-format=... "always" work
TEXT_ONLY_FORMAT = format_output_arg(
    "text",
    ["text"],
    "Select a given output format (options and output are not stable between releases)",
)


TEXT_CSV_FORMAT_NO_STABILITY_PROMISE = format_output_arg(
    "text",
    ["text", "csv"],
    "Select a given output format (options and output are not stable between releases)",
)


@plugin_list_cmds.register_subcommand(
    "plugins",
    help_description="List known plugins with their versions",
    argparser=TEXT_CSV_FORMAT_NO_STABILITY_PROMISE,
)
def _plugin_cmd_list_plugins(context: CommandContext) -> None:
    plugin_metadata_entries = context.load_plugins().plugin_data.values()
    # Because the "plugins" part is optional, we are not guaranteed that TEXT_CSV_FORMAT applies
    output_format = getattr(context.parsed_args, "output_format", "text")
    assert output_format in {"text", "csv"}
    with _stream_to_pager(context.parsed_args) as (fd, fo):
        fo.print_list_table(
            ["Plugin Name", "Plugin Path"],
            [(p.plugin_name, p.plugin_path) for p in plugin_metadata_entries],
        )


def _path(path: str) -> str:
    if path.startswith("./"):
        return path[1:]
    return path


def _ppf_flags(ppf: PackagerProvidedFileClassSpec) -> str:
    flags = []
    if ppf.allow_name_segment:
        flags.append("named")
    if ppf.allow_architecture_segment:
        flags.append("arch")
    if ppf.supports_priority:
        flags.append(f"priority={ppf.default_priority}")
    if ppf.packageless_is_fallback_for_all_packages:
        flags.append("main-all-fallback")
    if ppf.post_formatting_rewrite:
        flags.append("post-format-hook")
    return ",".join(flags)


@plugin_list_cmds.register_subcommand(
    ["used-packager-provided-files", "uppf", "u-p-p-f"],
    help_description="List packager provided files used by this package (debian/pkg.foo)",
    argparser=TEXT_ONLY_FORMAT,
)
def _plugin_cmd_list_uppf(context: CommandContext) -> None:
    ppf_table = context.load_plugins().packager_provided_files
    all_ppfs = detect_all_packager_provided_files(
        ppf_table,
        context.debian_dir,
        context.binary_packages(),
    )
    requested_plugins = set(context.requested_plugins())
    requested_plugins.add("debputy")
    all_detected_ppfs = list(flatten_ppfs(all_ppfs))

    used_ppfs = [
        p
        for p in all_detected_ppfs
        if p.definition.debputy_plugin_metadata.plugin_name in requested_plugins
    ]
    inactive_ppfs = [
        p
        for p in all_detected_ppfs
        if p.definition.debputy_plugin_metadata.plugin_name not in requested_plugins
    ]

    if not used_ppfs and not inactive_ppfs:
        print("No packager provided files detected; not even a changelog... ?")
        return

    with _stream_to_pager(context.parsed_args) as (fd, fo):
        if used_ppfs:
            headers: Sequence[Union[str, Tuple[str, str]]] = [
                "File",
                "Matched Stem",
                "Installed Into",
                "Installed As",
            ]
            fo.print_list_table(
                headers,
                [
                    (
                        ppf.path.path,
                        ppf.definition.stem,
                        ppf.package_name,
                        "/".join(ppf.compute_dest()).lstrip("."),
                    )
                    for ppf in sorted(
                        used_ppfs, key=operator.attrgetter("package_name")
                    )
                ],
            )

        if inactive_ppfs:
            headers: Sequence[Union[str, Tuple[str, str]]] = [
                "UNUSED FILE",
                "Matched Stem",
                "Installed Into",
                "Could Be Installed As",
                "If B-D Had",
            ]
            fo.print_list_table(
                headers,
                [
                    (
                        f"~{ppf.path.path}~",
                        ppf.definition.stem,
                        f"~{ppf.package_name}~",
                        "/".join(ppf.compute_dest()).lstrip("."),
                        f"debputy-plugin-{ppf.definition.debputy_plugin_metadata.plugin_name}",
                    )
                    for ppf in sorted(
                        inactive_ppfs, key=operator.attrgetter("package_name")
                    )
                ],
            )


@plugin_list_cmds.register_subcommand(
    ["packager-provided-files", "ppf", "p-p-f"],
    help_description="List packager provided file definitions (debian/pkg.foo)",
    argparser=TEXT_CSV_FORMAT_NO_STABILITY_PROMISE,
)
def _plugin_cmd_list_ppf(context: CommandContext) -> None:
    ppfs: Iterable[PackagerProvidedFileClassSpec]
    ppfs = context.load_plugins().packager_provided_files.values()
    with _stream_to_pager(context.parsed_args) as (fd, fo):
        headers: Sequence[Union[str, Tuple[str, str]]] = [
            "Stem",
            "Installed As",
            ("Mode", ">"),
            "Features",
            "Provided by",
        ]
        fo.print_list_table(
            headers,
            [
                (
                    ppf.stem,
                    _path(ppf.installed_as_format),
                    "0" + oct(ppf.default_mode)[2:],
                    _ppf_flags(ppf),
                    ppf.debputy_plugin_metadata.plugin_name,
                )
                for ppf in sorted(ppfs, key=operator.attrgetter("stem"))
            ],
        )

        if os.path.isdir("debian/") and fo.output_format == "text":
            fo.print()
            fo.print(
                "Hint: You can use `debputy plugin list used-packager-provided-files` to have `debputy`",
            )
            fo.print("list all the files in debian/ that matches these definitions.")


@plugin_list_cmds.register_subcommand(
    ["metadata-detectors"],
    help_description="List metadata detectors",
    argparser=TEXT_CSV_FORMAT_NO_STABILITY_PROMISE,
)
def _plugin_cmd_list_metadata_detectors(context: CommandContext) -> None:
    mds = list(
        chain.from_iterable(
            context.load_plugins().metadata_maintscript_detectors.values()
        )
    )

    def _sort_key(md: "MetadataOrMaintscriptDetector") -> Any:
        return md.plugin_metadata.plugin_name, md.detector_id

    with _stream_to_pager(context.parsed_args) as (fd, fo):
        fo.print_list_table(
            ["Provided by", "Detector Id"],
            [
                (md.plugin_metadata.plugin_name, md.detector_id)
                for md in sorted(mds, key=_sort_key)
            ],
        )


def _resolve_variable_for_list(
    substitution: Substitution,
    variable: PluginProvidedManifestVariable,
) -> str:
    var = "{{" + variable.variable_name + "}}"
    try:
        value = substitution.substitute(var, "CLI request")
    except DebputySubstitutionError:
        value = None
    return _render_manifest_variable_value(value)


def _render_manifest_variable_flag(variable: PluginProvidedManifestVariable) -> str:
    flags = []
    if variable.is_for_special_case:
        flags.append("special-use-case")
    if variable.is_internal:
        flags.append("internal")
    return ",".join(flags)


def _render_list_filter(v: Optional[bool]) -> str:
    if v is None:
        return "N/A"
    return "shown" if v else "hidden"


@plugin_list_cmds.register_subcommand(
    ["manifest-variables"],
    help_description="List plugin provided manifest variables (such as `{{path:FOO}}`)",
)
def plugin_cmd_list_manifest_variables(context: CommandContext) -> None:
    variables = context.load_plugins().manifest_variables
    substitution = context.substitution.with_extra_substitutions(
        PACKAGE="<package-name>"
    )
    parsed_args = context.parsed_args
    show_special_case_vars = parsed_args.show_special_use_variables
    show_token_vars = parsed_args.show_token_variables
    show_all_vars = parsed_args.show_all_variables

    def _include_var(var: PluginProvidedManifestVariable) -> bool:
        if show_all_vars:
            return True
        if var.is_internal:
            return False
        if var.is_for_special_case and not show_special_case_vars:
            return False
        if var.is_token and not show_token_vars:
            return False
        return True

    with _stream_to_pager(context.parsed_args) as (fd, fo):
        fo.print_list_table(
            ["Variable (use via: `{{ NAME }}`)", "Value", "Flag", "Provided by"],
            [
                (
                    k,
                    _resolve_variable_for_list(substitution, var),
                    _render_manifest_variable_flag(var),
                    var.plugin_metadata.plugin_name,
                )
                for k, var in sorted(variables.items())
                if _include_var(var)
            ],
        )

        fo.print()

        filters = [
            (
                "Token variables",
                show_token_vars if not show_all_vars else None,
                "--show-token-variables",
            ),
            (
                "Special use variables",
                show_special_case_vars if not show_all_vars else None,
                "--show-special-case-variables",
            ),
        ]

        fo.print_list_table(
            ["Variable type", "Value", "Option"],
            [
                (
                    fname,
                    _render_list_filter(value or show_all_vars),
                    f"{option} OR --show-all-variables",
                )
                for fname, value, option in filters
            ],
        )


@plugin_cmd_list_manifest_variables.configure_handler
def list_manifest_variable_arg_parser(
    plugin_list_manifest_variables_parser: argparse.ArgumentParser,
) -> None:
    plugin_list_manifest_variables_parser.add_argument(
        "--show-special-case-variables",
        dest="show_special_use_variables",
        default=False,
        action="store_true",
        help="Show variables that are only used in special / niche cases",
    )
    plugin_list_manifest_variables_parser.add_argument(
        "--show-token-variables",
        dest="show_token_variables",
        default=False,
        action="store_true",
        help="Show token (syntactical) variables like {{token:TAB}}",
    )
    plugin_list_manifest_variables_parser.add_argument(
        "--show-all-variables",
        dest="show_all_variables",
        default=False,
        action="store_true",
        help="Show all variables regardless of type/kind (overrules other filter settings)",
    )
    TEXT_ONLY_FORMAT(plugin_list_manifest_variables_parser)


def _parser_type_name(v: Union[str, Type[Any]]) -> str:
    if isinstance(v, str):
        return v if v != "<ROOT>" else ""
    return v.__name__


@plugin_list_cmds.register_subcommand(
    ["pluggable-manifest-rules", "p-m-r", "pmr"],
    help_description="Pluggable manifest rules (such as install rules)",
    argparser=TEXT_CSV_FORMAT_NO_STABILITY_PROMISE,
)
def _plugin_cmd_list_manifest_rules(context: CommandContext) -> None:
    feature_set = context.load_plugins()

    # Type hint to make the chain call easier for the type checker, which does not seem
    # to derive to this common base type on its own.
    base_type = Iterable[Tuple[Union[str, Type[Any]], DispatchingParserBase[Any]]]

    parser_generator = feature_set.manifest_parser_generator
    table_parsers: base_type = parser_generator.dispatchable_table_parsers.items()
    object_parsers: base_type = parser_generator.dispatchable_object_parsers.items()

    parsers = chain(
        table_parsers,
        object_parsers,
    )

    with _stream_to_pager(context.parsed_args) as (fd, fo):
        fo.print_list_table(
            ["Rule Name", "Rule Type", "Provided By"],
            [
                (
                    rn,
                    _parser_type_name(rt),
                    pt.parser_for(rn).plugin_metadata.plugin_name,
                )
                for rt, pt in parsers
                for rn in pt.registered_keywords()
            ],
        )


@plugin_list_cmds.register_subcommand(
    ["automatic-discard-rules", "a-d-r"],
    help_description="List automatic discard rules",
    argparser=TEXT_CSV_FORMAT_NO_STABILITY_PROMISE,
)
def _plugin_cmd_list_automatic_discard_rules(context: CommandContext) -> None:
    auto_discard_rules = context.load_plugins().auto_discard_rules

    with _stream_to_pager(context.parsed_args) as (fd, fo):
        fo.print_list_table(
            ["Name", "Provided By"],
            [
                (
                    name,
                    ppdr.plugin_metadata.plugin_name,
                )
                for name, ppdr in auto_discard_rules.items()
            ],
        )


def _render_manifest_variable_value(v: Optional[str]) -> str:
    if v is None:
        return "(N/A: Cannot resolve the variable)"
    v = v.replace("\n", "\\n").replace("\t", "\\t")
    return v


def _render_multiline_documentation(
    documentation: str,
    *,
    first_line_prefix: str = "Documentation: ",
    following_line_prefix: str = " ",
) -> None:
    current_prefix = first_line_prefix
    for line in documentation.splitlines(keepends=False):
        if line.isspace():
            if not current_prefix.isspace():
                print(current_prefix.rstrip())
                current_prefix = following_line_prefix
            else:
                print()
            continue
        print(f"{current_prefix}{line}")
        current_prefix = following_line_prefix


@plugin_show_cmds.register_subcommand(
    ["manifest-variables"],
    help_description="Plugin provided manifest variables (such as `{{path:FOO}}`)",
    argparser=add_arg(
        "manifest_variable",
        metavar="manifest-variable",
        help="Name of the variable (such as `path:FOO` or `{{path:FOO}}`) to display details about",
    ),
)
def _plugin_cmd_show_manifest_variables(context: CommandContext) -> None:
    plugin_feature_set = context.load_plugins()
    variables = plugin_feature_set.manifest_variables
    substitution = context.substitution
    parsed_args = context.parsed_args
    variable_name = parsed_args.manifest_variable
    fo = _output_styling(context.parsed_args, sys.stdout)
    if variable_name.startswith("{{") and variable_name.endswith("}}"):
        variable_name = variable_name[2:-2]
    variable: Optional[PluginProvidedManifestVariable]
    if variable_name.startswith("env:") and len(variable_name) > 4:
        env_var = variable_name[4:]
        variable = PluginProvidedManifestVariable(
            plugin_feature_set.plugin_data["debputy"],
            variable_name,
            variable_value=None,
            is_context_specific_variable=False,
            is_documentation_placeholder=True,
            variable_reference_documentation=f'Environment variable "{env_var}"',
        )
    else:
        variable = variables.get(variable_name)
    if variable is None:
        _error(
            f'Cannot resolve "{variable_name}" as a known variable from any of the available'
            f" plugins. Please use `debputy plugin list manifest-variables` to list all known"
            f" provided variables."
        )

    var_with_braces = "{{" + variable_name + "}}"
    try:
        source_value = substitution.substitute(var_with_braces, "CLI request")
    except DebputySubstitutionError:
        source_value = None
    binary_value = source_value
    print(f"Variable: {variable_name}")
    fo.print_visual_formatting(f"=========={'=' * len(variable_name)}")
    print()

    if variable.is_context_specific_variable:
        try:
            binary_value = substitution.with_extra_substitutions(
                PACKAGE="<package-name>",
            ).substitute(var_with_braces, "CLI request")
        except DebputySubstitutionError:
            binary_value = None

    doc = variable.variable_reference_documentation or "No documentation provided"
    _render_multiline_documentation(doc)

    if source_value == binary_value:
        print(f"Resolved: {_render_manifest_variable_value(source_value)}")
    else:
        print("Resolved:")
        print(f"    [source context]: {_render_manifest_variable_value(source_value)}")
        print(f"    [binary context]: {_render_manifest_variable_value(binary_value)}")

    if variable.is_for_special_case:
        print(
            'Special-case: The variable has been marked as a "special-case"-only variable.'
        )

    if not variable.is_documentation_placeholder:
        print(f"Plugin: {variable.plugin_metadata.plugin_name}")

    if variable.is_internal:
        print()
        # I knew everything I felt was showing on my face, and I hate that. I grated out,
        print("That was private.")


def _determine_ppf(
    context: CommandContext,
) -> Tuple[PackagerProvidedFileClassSpec, bool]:
    feature_set = context.load_plugins()
    ppf_name = context.parsed_args.ppf_name
    try:
        return feature_set.packager_provided_files[ppf_name], False
    except KeyError:
        pass

    orig_ppf_name = ppf_name
    if (
        ppf_name.startswith("d/")
        and not os.path.lexists(ppf_name)
        and os.path.lexists("debian/" + ppf_name[2:])
    ):
        ppf_name = "debian/" + ppf_name[2:]

    if ppf_name in ("debian/control", "debian/debputy.manifest", "debian/rules"):
        if ppf_name == "debian/debputy.manifest":
            doc = f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md"
        else:
            doc = "Debian Policy Manual or a packaging tutorial"
        _error(
            f"Sorry. While {orig_ppf_name} is a well-defined packaging file, it does not match the definition of"
            f" a packager provided file. Please see {doc} for more information about this file"
        )

    if context.has_dctrl_file and os.path.lexists(ppf_name):
        basename = ppf_name[7:]
        if "/" not in basename:
            debian_dir = build_virtual_fs([basename])
            all_ppfs = detect_all_packager_provided_files(
                feature_set.packager_provided_files,
                debian_dir,
                context.binary_packages(),
            )
            if all_ppfs:
                matched = next(iter(all_ppfs.values()))
                if len(matched.auto_installable) == 1 and not matched.reserved_only:
                    return matched.auto_installable[0].definition, True
                if not matched.auto_installable and len(matched.reserved_only) == 1:
                    reserved = next(iter(matched.reserved_only.values()))
                    if len(reserved) == 1:
                        return reserved[0].definition, True

    _error(
        f'Unknown packager provided file "{orig_ppf_name}". Please use'
        f" `debputy plugin list packager-provided-files` to see them all."
    )


@plugin_show_cmds.register_subcommand(
    ["packager-provided-files", "ppf", "p-p-f"],
    help_description="Show details about a given packager provided file (debian/pkg.foo)",
    argparser=add_arg(
        "ppf_name",
        metavar="name",
        help="Name of the packager provided file (such as `changelog`) to display details about",
    ),
)
def _plugin_cmd_show_ppf(context: CommandContext) -> None:
    ppf, matched_file = _determine_ppf(context)

    fo = _output_styling(context.parsed_args, sys.stdout)

    fo.print(f"Packager Provided File: {ppf.stem}")
    fo.print_visual_formatting(f"========================{'=' * len(ppf.stem)}")
    fo.print()
    ref_doc = ppf.reference_documentation
    description = ref_doc.description if ref_doc else None
    doc_uris = ref_doc.format_documentation_uris if ref_doc else tuple()
    if description is None:
        fo.print(
            f"Sorry, no description provided by the plugin {ppf.debputy_plugin_metadata.plugin_name}."
        )
    else:
        for line in description.splitlines(keepends=False):
            fo.print(line)

    fo.print()
    fo.print("Features:")
    if ppf.packageless_is_fallback_for_all_packages:
        fo.print(f" * debian/{ppf.stem} is used for *ALL* packages")
    else:
        fo.print(f' * debian/{ppf.stem} is used for only for the "main" package')
    if ppf.allow_name_segment:
        fo.print(" * Supports naming segment (multiple files and custom naming).")
    else:
        fo.print(
            " * No naming support; at most one per package and it is named after the package."
        )
    if ppf.allow_architecture_segment:
        fo.print(" * Supports architecture specific variants.")
    else:
        fo.print(" * No architecture specific variants.")
    if ppf.supports_priority:
        fo.print(
            f" * Has a priority system (default priority: {ppf.default_priority})."
        )

    fo.print()
    fo.print("Examples matches:")

    if context.has_dctrl_file:
        first_pkg = next(iter(context.binary_packages()))
    else:
        first_pkg = "example-package"
    example_files = [
        (f"debian/{ppf.stem}", first_pkg),
        (f"debian/{first_pkg}.{ppf.stem}", first_pkg),
    ]
    if ppf.allow_name_segment:
        example_files.append(
            (f"debian/{first_pkg}.my.custom.name.{ppf.stem}", "my.custom.name")
        )
    if ppf.allow_architecture_segment:
        example_files.append((f"debian/{first_pkg}.{ppf.stem}.amd64", first_pkg)),
        if ppf.allow_name_segment:
            example_files.append(
                (
                    f"debian/{first_pkg}.my.custom.name.{ppf.stem}.amd64",
                    "my.custom.name",
                )
            )
    fs_root = build_virtual_fs([x for x, _ in example_files])
    priority = ppf.default_priority if ppf.supports_priority else None
    rendered_examples = []
    for example_file, assigned_name in example_files:
        example_path = fs_root.lookup(example_file)
        assert example_path is not None and example_path.is_file
        dest = ppf.compute_dest(
            assigned_name,
            owning_package=first_pkg,
            assigned_priority=priority,
            path=example_path,
        )
        dest_path = "/".join(dest).lstrip(".")
        rendered_examples.append((example_file, dest_path))

    fo.print_list_table(["Source file", "Installed As"], rendered_examples)

    if doc_uris:
        fo.print()
        fo.print("Documentation URIs:")
        for uri in doc_uris:
            fo.print(f" * {fo.render_url(uri)}")

    plugin_name = ppf.debputy_plugin_metadata.plugin_name
    fo.print()
    fo.print(f"Install Mode: 0{oct(ppf.default_mode)[2:]}")
    fo.print(f"Provided by plugin: {plugin_name}")
    if (
        matched_file
        and plugin_name != "debputy"
        and plugin_name not in context.requested_plugins()
    ):
        fo.print()
        _warn(
            f"The file might *NOT* be used due to missing Build-Depends on debputy-plugin-{plugin_name}"
        )


@plugin_show_cmds.register_subcommand(
    ["pluggable-manifest-rules", "p-m-r", "pmr"],
    help_description="Pluggable manifest rules (such as install rules)",
    argparser=add_arg(
        "pmr_rule_name",
        metavar="rule-name",
        help="Name of the rule (such as `install`) to display details about",
    ),
)
def _plugin_cmd_show_manifest_rule(context: CommandContext) -> None:
    feature_set = context.load_plugins()
    parsed_args = context.parsed_args
    req_rule_type = None
    rule_name = parsed_args.pmr_rule_name
    if "::" in rule_name and rule_name != "::":
        req_rule_type, rule_name = rule_name.split("::", 1)

    matched = []

    base_type = Iterable[Tuple[Union[str, Type[Any]], DispatchingParserBase[Any]]]
    parser_generator = feature_set.manifest_parser_generator
    table_parsers: base_type = parser_generator.dispatchable_table_parsers.items()
    object_parsers: base_type = parser_generator.dispatchable_object_parsers.items()

    parsers = chain(
        table_parsers,
        object_parsers,
    )

    for rule_type, dispatching_parser in parsers:
        if req_rule_type is not None and req_rule_type not in _parser_type_name(
            rule_type
        ):
            continue
        if dispatching_parser.is_known_keyword(rule_name):
            matched.append((rule_type, dispatching_parser))

    if len(matched) != 1 and (matched or rule_name != "::"):
        if not matched:
            _error(
                f"Could not find any pluggable manifest rule related to {parsed_args.pmr_rule_name}."
                f" Please use `debputy plugin list pluggable-manifest-rules` to see the list of rules."
            )
        match_a = matched[0][0]
        match_b = matched[1][0]
        _error(
            f"The name {rule_name} was ambiguous and matched multiple rule types.  Please use"
            f" <rule-type>::{rule_name} to clarify which rule to use"
            f" (such as {_parser_type_name(match_a)}::{rule_name} or {_parser_type_name(match_b)}::{rule_name})."
            f" Please use `debputy plugin list pluggable-manifest-rules` to see the list of rules."
        )

    if matched:
        rule_type, matched_dispatching_parser = matched[0]
        plugin_provided_parser = matched_dispatching_parser.parser_for(rule_name)
        if isinstance(rule_type, str):
            manifest_attribute_path = rule_type
        else:
            manifest_attribute_path = SUPPORTED_DISPATCHABLE_TABLE_PARSERS[rule_type]
        parser_type_name = _parser_type_name(rule_type)
        parser = plugin_provided_parser.parser
        plugin_metadata = plugin_provided_parser.plugin_metadata
    else:
        rule_name = "::"
        parser = parser_generator.dispatchable_object_parsers[OPARSER_MANIFEST_ROOT]
        parser_type_name = ""
        plugin_metadata = plugin_metadata_for_debputys_own_plugin()
        manifest_attribute_path = ""

    is_root_rule = rule_name == "::"
    print(
        render_rule(
            rule_name,
            parser,
            plugin_metadata,
            is_root_rule=is_root_rule,
        )
    )

    if not is_root_rule:
        print(
            f"Used in: {manifest_attribute_path if manifest_attribute_path != '<ROOT>' else 'The manifest root'}"
        )
        print(f"Rule reference: {parser_type_name}::{rule_name}")
        print(f"Plugin: {plugin_metadata.plugin_name}")
    else:
        print(f"Rule reference: {rule_name}")

    print()
    print(
        "PS: If you want to know more about a non-trivial type of an attribute such as `FileSystemMatchRule`,"
    )
    print(
        "you can use `debputy plugin show type-mappings FileSystemMatchRule` to look it up "
    )


def _render_discard_rule_example(
    fo: OutputStylingBase,
    discard_rule: PluginProvidedDiscardRule,
    example: AutomaticDiscardRuleExample,
) -> None:
    processed = process_discard_rule_example(discard_rule, example)

    if processed.inconsistent_paths:
        plugin_name = discard_rule.plugin_metadata.plugin_name
        _warn(
            f"This example is inconsistent with what the code actually does."
            f" Please consider filing a bug against the plugin {plugin_name}"
        )

    doc = example.description
    if doc:
        print(doc)

    print("Consider the following source paths matched by a glob or directory match:")
    print()
    if fo.optimize_for_screen_reader:
        for p, _ in processed.rendered_paths:
            path_name = p.absolute
            print(
                f"The path {path_name} is a {'directory' if p.is_dir else 'file or symlink.'}"
            )

        print()
        if any(v.is_consistent and v.is_discarded for _, v in processed.rendered_paths):
            print("The following paths will be discarded by this rule:")
            for p, verdict in processed.rendered_paths:
                path_name = p.absolute
                if verdict.is_consistent and verdict.is_discarded:
                    print()
                    if p.is_dir:
                        print(f"{path_name} along with anything beneath it")
                    else:
                        print(path_name)
        else:
            print("No paths will be discarded in this example.")

        print()
        if any(v.is_consistent and v.is_kept for _, v in processed.rendered_paths):
            print("The following paths will be not be discarded by this rule:")
            for p, verdict in processed.rendered_paths:
                path_name = p.absolute
                if verdict.is_consistent and verdict.is_kept:
                    print()
                    print(path_name)

        if any(not v.is_consistent for _, v in processed.rendered_paths):
            print()
            print(
                "The example was inconsistent with the code. These are the paths where the code disagrees with"
                " the provided example:"
            )
            for p, verdict in processed.rendered_paths:
                path_name = p.absolute
                if not verdict.is_consistent:
                    print()
                    if verdict == DiscardVerdict.DISCARDED_BY_CODE:
                        print(
                            f"The path {path_name} was discarded by the code, but the example said it should"
                            f" have been installed."
                        )
                    else:
                        print(
                            f"The path {path_name} was not discarded by the code, but the example said it should"
                            f" have been discarded."
                        )
        return

    # Add +1 for dirs because we want trailing slashes in the output
    max_len = max(
        (len(p.absolute) + (1 if p.is_dir else 0)) for p, _ in processed.rendered_paths
    )
    for p, verdict in processed.rendered_paths:
        path_name = p.absolute
        if p.is_dir:
            path_name += "/"

        if not verdict.is_consistent:
            print(f"    {path_name:<{max_len}}  !! {verdict.message}")
        elif verdict.is_discarded:
            print(f"    {path_name:<{max_len}}  << {verdict.message}")
        else:
            print(f"    {path_name:<{max_len}}")


def _render_discard_rule(
    context: CommandContext,
    discard_rule: PluginProvidedDiscardRule,
) -> None:
    fo = _output_styling(context.parsed_args, sys.stdout)
    print(fo.colored(f"Automatic Discard Rule: {discard_rule.name}", style="bold"))
    fo.print_visual_formatting(
        f"========================{'=' * len(discard_rule.name)}"
    )
    print()
    doc = discard_rule.reference_documentation or "No documentation provided"
    _render_multiline_documentation(doc, first_line_prefix="", following_line_prefix="")

    if len(discard_rule.examples) > 1:
        print()
        fo.print_visual_formatting("Examples")
        fo.print_visual_formatting("--------")
        print()
        for no, example in enumerate(discard_rule.examples, start=1):
            print(
                fo.colored(
                    f"Example {no} of {len(discard_rule.examples)}", style="bold"
                )
            )
            fo.print_visual_formatting(f"........{'.' * len(str(no))}")
            _render_discard_rule_example(fo, discard_rule, example)
    elif discard_rule.examples:
        print()
        print(fo.colored("Example", style="bold"))
        fo.print_visual_formatting("-------")
        print()
        _render_discard_rule_example(fo, discard_rule, discard_rule.examples[0])


@plugin_show_cmds.register_subcommand(
    ["automatic-discard-rules", "a-d-r"],
    help_description="Pluggable manifest rules (such as install rules)",
    argparser=add_arg(
        "discard_rule",
        metavar="automatic-discard-rule",
        help="Name of the automatic discard rule (such as `backup-files`)",
    ),
)
def _plugin_cmd_show_automatic_discard_rules(context: CommandContext) -> None:
    auto_discard_rules = context.load_plugins().auto_discard_rules
    name = context.parsed_args.discard_rule
    discard_rule = auto_discard_rules.get(name)
    if discard_rule is None:
        _error(
            f'No automatic discard rule with the name "{name}". Please use'
            f" `debputy plugin list automatic-discard-rules` to see the list of automatic discard rules"
        )

    _render_discard_rule(context, discard_rule)


def _render_source_type(t: Any) -> str:
    _, origin_type, args = unpack_type(t, False)
    if origin_type == Union:
        at = ", ".join(_render_source_type(st) for st in args)
        return f"One of: {at}"
    name = BASIC_SIMPLE_TYPES.get(t)
    if name is not None:
        return name
    try:
        return t.__name__
    except AttributeError:
        return str(t)


@plugin_list_cmds.register_subcommand(
    "type-mappings",
    help_description="Registered type mappings/descriptions",
)
def _plugin_cmd_list_type_mappings(context: CommandContext) -> None:
    type_mappings = context.load_plugins().mapped_types

    with _stream_to_pager(context.parsed_args) as (fd, fo):
        fo.print_list_table(
            ["Type", "Base Type", "Provided By"],
            [
                (
                    target_type.__name__,
                    _render_source_type(type_mapping.mapped_type.source_type),
                    type_mapping.plugin_metadata.plugin_name,
                )
                for target_type, type_mapping in type_mappings.items()
            ],
        )


@plugin_show_cmds.register_subcommand(
    "type-mappings",
    help_description="Register type mappings/descriptions",
    argparser=add_arg(
        "type_mapping",
        metavar="type-mapping",
        help="Name of the type",
    ),
)
def _plugin_cmd_show_type_mappings(context: CommandContext) -> None:
    type_mapping_name = context.parsed_args.type_mapping
    type_mappings = context.load_plugins().mapped_types

    matches = []
    for type_ in type_mappings:
        if type_.__name__ == type_mapping_name:
            matches.append(type_)

    if not matches:
        simple_types = set(BASIC_SIMPLE_TYPES.values())
        simple_types.update(t.__name__ for t in BASIC_SIMPLE_TYPES)

        if type_mapping_name in simple_types:
            print(f"The type {type_mapping_name} is a YAML scalar.")
            return
        if type_mapping_name == "Any":
            print(
                "The Any type is a placeholder for when no typing information is provided. Often this implies"
                " custom parse logic."
            )
            return

        if type_mapping_name in ("List", "list"):
            print(
                f"The {type_mapping_name} is a YAML Sequence. Please see the YAML documentation for examples."
            )
            return

        if type_mapping_name in ("Mapping", "dict"):
            print(
                f"The {type_mapping_name} is a YAML mapping. Please see the YAML documentation for examples."
            )
            return

        if "[" in type_mapping_name:
            _error(
                f"No known matches for {type_mapping_name}. Note: It looks like a composite type. Try searching"
                " for its component parts. As an example, replace List[FileSystemMatchRule] with FileSystemMatchRule."
            )

        _error(f"Sorry, no known matches for {type_mapping_name}")

    if len(matches) > 1:
        _error(
            f"Too many matches for {type_mapping_name}... Sorry, there is no way to avoid this right now :'("
        )

    match = matches[0]
    _render_type(context, type_mappings[match])


def _render_type_example(
    context: CommandContext,
    fo: OutputStylingBase,
    parser_context: ParserContextData,
    type_mapping: TypeMapping[Any, Any],
    example: TypeMappingExample,
) -> Tuple[str, bool]:
    attr_path = AttributePath.builtin_path()["CLI Request"]
    v = _render_value(example.source_input)
    try:
        type_mapping.mapper(
            example.source_input,
            attr_path,
            parser_context,
        )
    except RuntimeError:
        if context.parsed_args.debug_mode:
            raise
        fo.print(
            fo.colored("Broken example: ", fg="red")
            + f"Provided example input ({v})"
            + " caused an exception when parsed. Please file a bug against the plugin."
            + " Use --debug to see the stack trace"
        )
        return fo.colored(v, fg="red") + " [Example value could not be parsed]", True
    return fo.colored(v, fg="green"), False


def _render_type(
    context: CommandContext,
    pptm: PluginProvidedTypeMapping,
) -> None:
    fo = _output_styling(context.parsed_args, sys.stdout)
    type_mapping = pptm.mapped_type
    target_type = type_mapping.target_type
    ref_doc = pptm.reference_documentation
    desc = ref_doc.description if ref_doc is not None else None
    examples = ref_doc.examples if ref_doc is not None else tuple()

    fo.print(fo.colored(f"# Type Mapping: {target_type.__name__}", style="bold"))
    fo.print()
    if desc is not None:
        _render_multiline_documentation(
            desc, first_line_prefix="", following_line_prefix=""
        )
    else:
        fo.print("No documentation provided.")

    context.parse_manifest()

    manifest_parser = context.manifest_parser()

    if examples:
        had_issues = False
        fo.print()
        fo.print(fo.colored("## Example values", style="bold"))
        fo.print()
        for no, example in enumerate(examples, start=1):
            v, i = _render_type_example(
                context, fo, manifest_parser, type_mapping, example
            )
            fo.print(f" * {v}")
            if i:
                had_issues = True
    else:
        had_issues = False

    fo.print()
    fo.print(f"Provided by plugin: {pptm.plugin_metadata.plugin_name}")

    if had_issues:
        fo.print()
        fo.print(
            fo.colored(
                "Examples had issues. Please file a bug against the plugin", fg="red"
            )
        )
        fo.print()
        fo.print("Use --debug to see the stacktrace")


def _render_value(v: Any) -> str:
    if isinstance(v, str) and '"' not in v:
        return f'"{v}"'
    return str(v)


def ensure_plugin_commands_are_loaded():
    # Loading the module does the heavy lifting
    # However, having this function means that we do not have an "unused" import that some tool
    # gets tempted to remove
    assert ROOT_COMMAND.has_command("plugin")
