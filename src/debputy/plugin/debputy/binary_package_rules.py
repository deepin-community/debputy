import dataclasses
import os
import textwrap
from typing import (
    Any,
    List,
    NotRequired,
    Union,
    Literal,
    TypedDict,
    Annotated,
    Optional,
    FrozenSet,
    Self,
    cast,
)

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy.maintscript_snippet import DpkgMaintscriptHelperCommand, MaintscriptSnippet
from debputy.manifest_parser.base_types import FileSystemExactMatchRule
from debputy.manifest_parser.tagging_types import DebputyParsedContent
from debputy.manifest_parser.parse_hints import DebputyParseHint
from debputy.manifest_parser.declarative_parser import ParserGenerator
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.path_matcher import MatchRule, MATCH_ANYTHING, ExactFileSystemPath
from debputy.plugin.api import reference_documentation
from debputy.plugin.api.impl import (
    DebputyPluginInitializerProvider,
    ServiceDefinitionImpl,
)
from debputy.plugin.api.parser_tables import OPARSER_PACKAGES
from debputy.plugin.api.spec import (
    ServiceUpgradeRule,
    ServiceDefinition,
    DSD,
    documented_attr,
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
    not_integrations,
)
from debputy.transformation_rules import TransformationRule
from debputy.util import _error

ACCEPTABLE_CLEAN_ON_REMOVAL_FOR_GLOBS_AND_EXACT_MATCHES = frozenset(
    [
        "./var/log",
    ]
)


ACCEPTABLE_CLEAN_ON_REMOVAL_IF_EXACT_MATCH_OR_SUBDIR_OF = frozenset(
    [
        "./etc",
        "./run",
        "./var/lib",
        "./var/cache",
        "./var/backups",
        "./var/spool",
        # linux-image uses these paths with some `rm -f`
        "./usr/lib/modules",
        "./lib/modules",
        # udev special case
        "./lib/udev",
        "./usr/lib/udev",
        # pciutils deletes /usr/share/misc/pci.ids.<ext>
        "./usr/share/misc",
    ]
)


def register_binary_package_rules(api: DebputyPluginInitializerProvider) -> None:
    api.pluggable_manifest_rule(
        OPARSER_PACKAGES,
        "binary-version",
        BinaryVersionParsedFormat,
        _parse_binary_version,
        source_format=str,
        inline_reference_documentation=reference_documentation(
            title="Custom binary version (`binary-version`)",
            description=textwrap.dedent(
                """\
                In the *rare* case that you need a binary package to have a custom version, you can use
                the `binary-version:` key to describe the desired package version.  An example being:

                    packages:
                        foo:
                            # The foo package needs a different epoch because we took it over from a different
                            # source package with higher epoch version
                            binary-version: '1:{{DEB_VERSION_UPSTREAM_REVISION}}'

                Use this feature sparingly as it is generally not possible to undo as each version must be
                monotonously higher than the previous one. This feature translates into `-v` option for
                `dpkg-gencontrol`.

                The value for the `binary-version` key is a string that defines the binary version.  Generally,
                you will want it to contain one of the versioned related substitution variables such as
                `{{DEB_VERSION_UPSTREAM_REVISION}}`.  Otherwise, you will have to remember to bump the version
                manually with each upload as versions cannot be reused and the package would not support binNMUs
                either.
            """
            ),
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#custom-binary-version-binary-version",
        ),
    )

    api.pluggable_manifest_rule(
        OPARSER_PACKAGES,
        "transformations",
        List[TransformationRule],
        _unpack_list,
        inline_reference_documentation=reference_documentation(
            title="Transformations (`transformations`)",
            description=textwrap.dedent(
                """\
                You can define a `transformations` under the package definition, which is a list a transformation
                rules.  An example:

                    packages:
                        foo:
                            transformations:
                              - remove: 'usr/share/doc/{{PACKAGE}}/INSTALL.md'
                              - move:
                                    source: bar/*
                                    target: foo/


                Transformations are ordered and are applied in the listed order.  A path can be matched by multiple
                transformations; how that plays out depends on which transformations are applied and in which order.
                A quick summary:

                 - Transformations that modify the file system layout affect how path matches in later transformations.
                   As an example, `move` and `remove` transformations affects what globs and path matches expand to in
                   later transformation rules.

                 - For other transformations generally the latter transformation overrules the earlier one, when they
                   overlap or conflict.
            """
            ),
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#transformations-transformations",
        ),
    )

    api.pluggable_manifest_rule(
        OPARSER_PACKAGES,
        "conffile-management",
        List[DpkgMaintscriptHelperCommand],
        _unpack_list,
        expected_debputy_integration_mode=not_integrations(
            INTEGRATION_MODE_DH_DEBPUTY_RRR
        ),
    )

    api.pluggable_manifest_rule(
        OPARSER_PACKAGES,
        "services",
        List[ServiceRuleParsedFormat],
        _process_service_rules,
        source_format=List[ServiceRuleSourceFormat],
        expected_debputy_integration_mode=not_integrations(
            INTEGRATION_MODE_DH_DEBPUTY_RRR
        ),
        inline_reference_documentation=reference_documentation(
            title="Define how services in the package will be handled (`services`)",
            description=textwrap.dedent(
                """\
                If you have non-standard requirements for certain services in the package, you can define those via
                the `services` attribute. The `services` attribute is a list of service rules. Example:

                    packages:
                        foo:
                            services:
                              - service: "foo"
                                enable-on-install: false
                              - service: "bar"
                                on-upgrade: stop-then-start
            """
            ),
            attributes=[
                documented_attr(
                    "service",
                    textwrap.dedent(
                        f"""\
                        Name of the service to match. The name is usually the basename of the service file.
                        However, aliases can also be used for relevant system managers. When aliases **and**
                        multiple service managers are involved, then the rule will apply to all matches.
                        For details on aliases, please see
                        {DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#service-managers-and-aliases.

                          - Note: For systemd, the `.service` suffix can be omitted from name, but other
                            suffixes such as `.timer` cannot.
                """
                    ),
                ),
                documented_attr(
                    "type_of_service",
                    textwrap.dedent(
                        """\
                        The type of service this rule applies to. To act on a `systemd` timer, you would
                        set this to `timer` (etc.). Each service manager defines its own set of types
                        of services.
                """
                    ),
                ),
                documented_attr(
                    "service_scope",
                    textwrap.dedent(
                        """\
                        The scope of the service. It must be either `system` and `user`.
                        - Note: The keyword is defined to support `user`, but `debputy` does not support `user`
                          services at the moment (the detection logic is missing).
                """
                    ),
                ),
                documented_attr(
                    ["service_manager", "service_managers"],
                    textwrap.dedent(
                        """\
                        Which service managers this rule is for. When omitted, all service managers with this
                        service will be affected. This can be used to specify separate rules for the same
                        service under different service managers.
                        - When this attribute is explicitly given, then all the listed service managers must
                          provide at least one service matching the definition. In contract, when it is omitted,
                          then all service manager integrations are consulted but as long as at least one
                          service is match from any service manager, the rule is accepted.
                    """
                    ),
                ),
                documented_attr(
                    "enable_on_install",
                    textwrap.dedent(
                        """\
                            Whether to automatically enable the service on installation. Note: This does
                            **not** affect whether the service will be started nor how restarts during
                            upgrades will happen.
                            - If omitted, the plugin detecting the service decides the default.
                            """
                    ),
                ),
                documented_attr(
                    "start_on_install",
                    textwrap.dedent(
                        """\
                            Whether to automatically start the service on installation. Whether it is
                            enabled or how upgrades are handled have separate attributes.
                            - If omitted, the plugin detecting the service decides the default.
                """
                    ),
                ),
                documented_attr(
                    "on_upgrade",
                    textwrap.dedent(
                        """\
                           How `debputy` should handle the service during upgrades. The default depends on the
                           plugin detecting the service. Valid values are:

                           - `do-nothing`: During an upgrade, the package should not attempt to stop, reload or
                              restart the service.
                           - `reload`: During an upgrade, prefer reloading the service rather than restarting
                              if possible. Note that the result may become `restart` instead if the service
                              manager integration determines that `reload` is not supported.
                           - `restart`: During an upgrade, `restart` the service post upgrade. The service
                              will be left running during the upgrade process.
                           - `stop-then-start`: Stop the service before the upgrade, perform the upgrade and
                              then start the service.
                """
                    ),
                ),
            ],
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#service-management-services",
        ),
    )

    api.pluggable_manifest_rule(
        OPARSER_PACKAGES,
        "clean-after-removal",
        ListParsedFormat,
        _parse_clean_after_removal,
        source_format=List[Any],
        expected_debputy_integration_mode=not_integrations(
            INTEGRATION_MODE_DH_DEBPUTY_RRR
        ),
        # FIXME: debputy won't see the attributes for this one :'(
        inline_reference_documentation=reference_documentation(
            title="Remove runtime created paths on purge or post removal (`clean-after-removal`)",
            description=textwrap.dedent(
                """\
        For some packages, it is necessary to clean up some run-time created paths. Typical use cases are
        deleting log files, cache files, or persistent state. This can be done via the `clean-after-removal`.
        An example being:

            packages:
                foo:
                    clean-after-removal:
                    - /var/log/foo/*.log
                    - /var/log/foo/*.log.gz
                    - path: /var/log/foo/
                      ignore-non-empty-dir: true
                    - /etc/non-conffile-configuration.conf
                    - path: /var/cache/foo
                      recursive: true

        The `clean-after-removal` key accepts a list, where each element is either a mapping, a string or a list
        of strings. When an element is a mapping, then the following key/value pairs are applicable:

         * `path` or `paths` (required): A path match (`path`) or a list of path matches (`paths`) defining the
           path(s) that should be removed after clean. The path match(es) can use globs and manifest variables.
           Every path matched will by default be removed via `rm -f` or `rmdir` depending on whether the path
           provided ends with a *literal* `/`. Special-rules for matches:
            - Glob is interpreted by the shell, so shell (`/bin/sh`) rules apply to globs rather than
              `debputy`'s glob rules.  As an example, `foo/*` will **not** match `foo/.hidden-file`.
            - `debputy` cannot evaluate whether these paths/globs will match the desired paths (or anything at
              all). Be sure to test the resulting package.
            - When a symlink is matched, it is not followed.
            - Directory handling depends on the `recursive` attribute and whether the pattern ends with a literal
              "/".
            - `debputy` has restrictions on the globs being used to prevent rules that could cause massive damage
              to the system.

         * `recursive` (optional): When `true`, the removal rule will use `rm -fr` rather than `rm -f` or `rmdir`
            meaning any directory matched will be deleted along with all of its contents.

         * `ignore-non-empty-dir` (optional): When `true`, each path must be or match a directory (and as a
           consequence each path must with a literal `/`). The affected directories will be deleted only if they
           are empty. Non-empty directories will be skipped. This option is mutually exclusive with `recursive`.

         * `delete-on` (optional, defaults to `purge`): This attribute defines when the removal happens. It can
           be set to one of the following values:
           - `purge`: The removal happens with the package is being purged. This is the default. At a technical
             level, the removal occurs at `postrm purge`.
           - `removal`: The removal happens immediately after the package has been removed. At a technical level,
             the removal occurs at `postrm remove`.

        This feature resembles the concept of `rpm`'s `%ghost` files.
            """
            ),
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#remove-runtime-created-paths-on-purge-or-post-removal-clean-after-removal",
        ),
    )

    api.pluggable_manifest_rule(
        OPARSER_PACKAGES,
        "installation-search-dirs",
        InstallationSearchDirsParsedFormat,
        _parse_installation_search_dirs,
        source_format=List[FileSystemExactMatchRule],
        expected_debputy_integration_mode=not_integrations(
            INTEGRATION_MODE_DH_DEBPUTY_RRR
        ),
        inline_reference_documentation=reference_documentation(
            title="Custom installation time search directories (`installation-search-dirs`)",
            description=textwrap.dedent(
                """\
        For source packages that does multiple build, it can be an advantage to provide a custom list of
        installation-time search directories. This can be done via the `installation-search-dirs` key. A common
        example is building  the source twice with different optimization and feature settings where the second
        build is for the `debian-installer` (in the form of a `udeb` package). A sample manifest snippet could
        look something like:

            installations:
            - install:
                # Because of the search order (see below), `foo` installs `debian/tmp/usr/bin/tool`,
                # while `foo-udeb` installs `debian/tmp-udeb/usr/bin/tool` (assuming both paths are
                # available). Note the rule can be split into two with the same effect if that aids
                # readability or understanding.
                source: usr/bin/tool
                into:
                  - foo
                  - foo-udeb
            packages:
                foo-udeb:
                    installation-search-dirs:
                    - debian/tmp-udeb


        The `installation-search-dirs` key accepts a list, where each element is a path (str) relative from the
        source root to the directory that should be used as a search directory (absolute paths are still interpreted
        as relative to the source root).  This list should contain all search directories that should be applicable
        for this package (except the source root itself, which is always appended after the provided list). If the
        key is omitted, then `debputy` will provide a default  search order (In the `dh` integration, the default
        is the directory `debian/tmp`).

        If a non-existing or non-directory path is listed, then it will be skipped (info-level note). If the path
        exists and is a directory, it will also be checked for "not-installed" paths.
            """
            ),
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#custom-installation-time-search-directories-installation-search-dirs",
        ),
    )


class ServiceRuleSourceFormat(TypedDict):
    service: str
    type_of_service: NotRequired[str]
    service_scope: NotRequired[Literal["system", "user"]]
    enable_on_install: NotRequired[bool]
    start_on_install: NotRequired[bool]
    on_upgrade: NotRequired[ServiceUpgradeRule]
    service_manager: NotRequired[
        Annotated[str, DebputyParseHint.target_attribute("service_managers")]
    ]
    service_managers: NotRequired[List[str]]


class ServiceRuleParsedFormat(DebputyParsedContent):
    service: str
    type_of_service: NotRequired[str]
    service_scope: NotRequired[Literal["system", "user"]]
    enable_on_install: NotRequired[bool]
    start_on_install: NotRequired[bool]
    on_upgrade: NotRequired[ServiceUpgradeRule]
    service_managers: NotRequired[List[str]]


@dataclasses.dataclass(slots=True, frozen=True)
class ServiceRule:
    definition_source: str
    service: str
    type_of_service: str
    service_scope: Literal["system", "user"]
    enable_on_install: Optional[bool]
    start_on_install: Optional[bool]
    on_upgrade: Optional[ServiceUpgradeRule]
    service_managers: Optional[FrozenSet[str]]

    @classmethod
    def from_service_rule_parsed_format(
        cls,
        data: ServiceRuleParsedFormat,
        attribute_path: AttributePath,
    ) -> "Self":
        service_managers = data.get("service_managers")
        return cls(
            attribute_path.path,
            data["service"],
            data.get("type_of_service", "service"),
            cast("Literal['system', 'user']", data.get("service_scope", "system")),
            data.get("enable_on_install"),
            data.get("start_on_install"),
            data.get("on_upgrade"),
            frozenset(service_managers) if service_managers else service_managers,
        )

    def applies_to_service_manager(self, service_manager: str) -> bool:
        return self.service_managers is None or service_manager in self.service_managers

    def apply_to_service_definition(
        self,
        service_definition: ServiceDefinition[DSD],
    ) -> ServiceDefinition[DSD]:
        assert isinstance(service_definition, ServiceDefinitionImpl)
        if not service_definition.is_plugin_provided_definition:
            _error(
                f"Conflicting definitions related to {self.service} (type: {self.type_of_service},"
                f" scope: {self.service_scope}). First definition at {service_definition.definition_source},"
                f" the second at {self.definition_source}). If they are for different service managers,"
                " you can often avoid this problem by explicitly defining which service managers are applicable"
                ' to each rule via the "service-managers" keyword.'
            )
        changes = {
            "definition_source": self.definition_source,
            "is_plugin_provided_definition": False,
        }
        if (
            self.service != service_definition.name
            and self.service in service_definition.names
        ):
            changes["name"] = self.service
        if self.enable_on_install is not None:
            changes["auto_start_on_install"] = self.enable_on_install
        if self.start_on_install is not None:
            changes["auto_start_on_install"] = self.start_on_install
        if self.on_upgrade is not None:
            changes["on_upgrade"] = self.on_upgrade

        return service_definition.replace(**changes)


class BinaryVersionParsedFormat(DebputyParsedContent):
    binary_version: str


class ListParsedFormat(DebputyParsedContent):
    elements: List[Any]


class ListOfTransformationRulesFormat(DebputyParsedContent):
    elements: List[TransformationRule]


class ListOfDpkgMaintscriptHelperCommandFormat(DebputyParsedContent):
    elements: List[DpkgMaintscriptHelperCommand]


class InstallationSearchDirsParsedFormat(DebputyParsedContent):
    installation_search_dirs: List[FileSystemExactMatchRule]


def _parse_binary_version(
    _name: str,
    parsed_data: BinaryVersionParsedFormat,
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> str:
    return parsed_data["binary_version"]


def _parse_installation_search_dirs(
    _name: str,
    parsed_data: InstallationSearchDirsParsedFormat,
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> List[FileSystemExactMatchRule]:
    return parsed_data["installation_search_dirs"]


def _process_service_rules(
    _name: str,
    parsed_data: List[ServiceRuleParsedFormat],
    attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> List[ServiceRule]:
    return [
        ServiceRule.from_service_rule_parsed_format(x, attribute_path[i])
        for i, x in enumerate(parsed_data)
    ]


def _unpack_list(
    _name: str,
    parsed_data: List[Any],
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> List[Any]:
    return parsed_data


class CleanAfterRemovalRuleSourceFormat(TypedDict):
    path: NotRequired[Annotated[str, DebputyParseHint.target_attribute("paths")]]
    paths: NotRequired[List[str]]
    delete_on: NotRequired[Literal["purge", "removal"]]
    recursive: NotRequired[bool]
    ignore_non_empty_dir: NotRequired[bool]


class CleanAfterRemovalRule(DebputyParsedContent):
    paths: List[str]
    delete_on: NotRequired[Literal["purge", "removal"]]
    recursive: NotRequired[bool]
    ignore_non_empty_dir: NotRequired[bool]


# FIXME: Not optimal that we are doing an initialization of ParserGenerator here. But the rule is not depending on any
#  complex types that is registered by plugins, so it will work for now.
_CLEAN_AFTER_REMOVAL_RULE_PARSER = ParserGenerator().generate_parser(
    CleanAfterRemovalRule,
    source_content=Union[CleanAfterRemovalRuleSourceFormat, str, List[str]],
    inline_reference_documentation=reference_documentation(
        reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#remove-runtime-created-paths-on-purge-or-post-removal-clean-after-removal",
    ),
)


# Order between clean_on_removal and conffile_management is
# important. We want the dpkg conffile management rules to happen before the
# clean clean_on_removal rules.  Since the latter only affects `postrm`
# and the order is reversed for `postrm` scripts (among other), we need do
# clean_on_removal first to account for the reversing of order.
#
# FIXME: All of this is currently not really possible todo, but it should be.
# (I think it is the correct order by "mistake" rather than by "design", which is
# what this note is about)
def _parse_clean_after_removal(
    _name: str,
    parsed_data: ListParsedFormat,
    attribute_path: AttributePath,
    parser_context: ParserContextData,
) -> None:  # TODO: Return and pass to a maintscript helper
    raw_clean_after_removal = parsed_data["elements"]
    package_state = parser_context.current_binary_package_state

    for no, raw_transformation in enumerate(raw_clean_after_removal):
        definition_source = attribute_path[no]
        clean_after_removal_rules = _CLEAN_AFTER_REMOVAL_RULE_PARSER.parse_input(
            raw_transformation,
            definition_source,
            parser_context=parser_context,
        )
        patterns = clean_after_removal_rules["paths"]
        if patterns:
            definition_source.path_hint = patterns[0]
        delete_on = clean_after_removal_rules.get("delete_on") or "purge"
        recurse = clean_after_removal_rules.get("recursive") or False
        ignore_non_empty_dir = (
            clean_after_removal_rules.get("ignore_non_empty_dir") or False
        )
        if delete_on == "purge":
            condition = '[ "$1" = "purge" ]'
        else:
            condition = '[ "$1" = "remove" ]'

        if ignore_non_empty_dir:
            if recurse:
                raise ManifestParseException(
                    'The "recursive" and "ignore-non-empty-dir" options are mutually exclusive.'
                    f" Both were enabled at the same time in at {definition_source.path}"
                )
            for pattern in patterns:
                if not pattern.endswith("/"):
                    raise ManifestParseException(
                        'When ignore-non-empty-dir is True, then all patterns must end with a literal "/"'
                        f' to ensure they only apply to directories. The pattern "{pattern}" at'
                        f" {definition_source.path} did not."
                    )

        substitution = parser_context.substitution
        match_rules = [
            MatchRule.from_path_or_glob(
                p, definition_source.path, substitution=substitution
            )
            for p in patterns
        ]
        content_lines = [
            f"if {condition}; then\n",
        ]
        for idx, match_rule in enumerate(match_rules):
            original_pattern = patterns[idx]
            if match_rule is MATCH_ANYTHING:
                raise ManifestParseException(
                    f'Using "{original_pattern}" in a clean rule would trash the system.'
                    f" Please restrict this pattern at {definition_source.path} considerably."
                )
            is_subdir_match = False
            matched_directory: Optional[str]
            if isinstance(match_rule, ExactFileSystemPath):
                matched_directory = (
                    os.path.dirname(match_rule.path)
                    if match_rule.path not in ("/", ".", "./")
                    else match_rule.path
                )
                is_subdir_match = True
            else:
                matched_directory = getattr(match_rule, "directory", None)

            if matched_directory is None:
                raise ManifestParseException(
                    f'The pattern "{original_pattern}" defined at {definition_source.path} is not'
                    f" trivially anchored in a specific directory. Cowardly refusing to use it"
                    f" in a clean rule as it may trash the system if the pattern is overreaching."
                    f" Please avoid glob characters in the top level directories."
                )
            assert matched_directory.startswith("./") or matched_directory in (
                ".",
                "./",
                "",
            )
            acceptable_directory = False
            would_have_allowed_direct_match = False
            while matched_directory not in (".", "./", ""):
                # Our acceptable paths set includes "/var/lib" or "/etc".  We require that the
                # pattern is either an exact match, in which case it may match directly inside
                # the acceptable directory OR it is a pattern against a subdirectory of the
                # acceptable path. As an example:
                #
                # /etc/inputrc <-- OK, exact match
                # /etc/foo/*   <-- OK, subdir match
                # /etc/*       <-- ERROR, glob directly in the accepted directory.
                if is_subdir_match and (
                    matched_directory
                    in ACCEPTABLE_CLEAN_ON_REMOVAL_IF_EXACT_MATCH_OR_SUBDIR_OF
                ):
                    acceptable_directory = True
                    break
                if (
                    matched_directory
                    in ACCEPTABLE_CLEAN_ON_REMOVAL_FOR_GLOBS_AND_EXACT_MATCHES
                ):
                    # Special-case: In some directories (such as /var/log), we allow globs directly.
                    # Notably, X11's log files are /var/log/Xorg.*.log
                    acceptable_directory = True
                    break
                if (
                    matched_directory
                    in ACCEPTABLE_CLEAN_ON_REMOVAL_IF_EXACT_MATCH_OR_SUBDIR_OF
                ):
                    would_have_allowed_direct_match = True
                    break
                matched_directory = os.path.dirname(matched_directory)
                is_subdir_match = True

            if would_have_allowed_direct_match and not acceptable_directory:
                raise ManifestParseException(
                    f'The pattern "{original_pattern}" defined at {definition_source.path} seems to'
                    " be overreaching. If it has been a path (and not use a glob), the rule would"
                    " have been permitted."
                )
            elif not acceptable_directory:
                raise ManifestParseException(
                    f'The pattern or path "{original_pattern}" defined at {definition_source.path} seems to'
                    f' be overreaching or not limited to the set of "known acceptable" directories.'
                )

            try:
                shell_escaped_pattern = match_rule.shell_escape_pattern()
            except TypeError:
                raise ManifestParseException(
                    f'Sorry, the pattern "{original_pattern}" defined at {definition_source.path}'
                    f" is unfortunately not supported by `debputy` for clean-after-removal rules."
                    f" If you can rewrite the rule to something like `/var/log/foo/*.log` or"
                    f' similar "trivial" patterns. You may have to rewrite the pattern the rule '
                    f" into multiple patterns to achieve this.  This restriction is to enable "
                    f' `debputy` to ensure the pattern is correctly executed plus catch "obvious'
                    f' system trashing" patterns. Apologies for the inconvenience.'
                )

            if ignore_non_empty_dir:
                cmd = f'    rmdir --ignore-fail-on-non-empty "${{DPKG_ROOT}}"{shell_escaped_pattern}\n'
            elif recurse:
                cmd = f'    rm -fr "${{DPKG_ROOT}}"{shell_escaped_pattern}\n'
            elif original_pattern.endswith("/"):
                cmd = f'    rmdir "${{DPKG_ROOT}}"{shell_escaped_pattern}\n'
            else:
                cmd = f'    rm -f "${{DPKG_ROOT}}"{shell_escaped_pattern}\n'
            content_lines.append(cmd)
        content_lines.append("fi\n")

        snippet = MaintscriptSnippet(definition_source.path, "".join(content_lines))
        package_state.maintscript_snippets["postrm"].append(snippet)
