import textwrap
from typing import List, Any, Dict, Tuple, TYPE_CHECKING, cast

from debputy._manifest_constants import (
    ManifestVersion,
    MK_MANIFEST_VERSION,
    MK_INSTALLATIONS,
    SUPPORTED_MANIFEST_VERSIONS,
    MK_MANIFEST_DEFINITIONS,
    MK_PACKAGES,
    MK_MANIFEST_VARIABLES,
)
from debputy.exceptions import DebputySubstitutionError
from debputy.installations import InstallRule
from debputy.manifest_parser.tagging_types import DebputyParsedContent
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.plugin.api import reference_documentation
from debputy.plugin.api.impl import DebputyPluginInitializerProvider
from debputy.plugin.api.parser_tables import (
    OPARSER_MANIFEST_ROOT,
    OPARSER_MANIFEST_DEFINITIONS,
    OPARSER_PACKAGES,
)
from debputy.plugin.api.spec import (
    not_integrations,
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
)
from debputy.plugin.debputy.build_system_rules import register_build_system_rules
from debputy.substitution import VariableNameState, SUBST_VAR_RE

if TYPE_CHECKING:
    from debputy.highlevel_manifest_parser import YAMLManifestParser


def register_manifest_root_rules(api: DebputyPluginInitializerProvider) -> None:
    # Registration order matters. Notably, definitions must come before anything that can
    # use definitions (variables), which is why it is second only to the manifest version.
    api.pluggable_manifest_rule(
        OPARSER_MANIFEST_ROOT,
        MK_MANIFEST_VERSION,
        ManifestVersionFormat,
        _handle_version,
        source_format=ManifestVersion,
        inline_reference_documentation=reference_documentation(
            title="Manifest version",
            description=textwrap.dedent(
                """\
                All `debputy` manifests must include a `debputy` manifest version, which will enable the
                format to change over time.  For now, there is only one version (`"0.1"`) and you have
                to include the line:

                    manifest-version: "0.1"

                On its own, the manifest containing only `manifest-version: "..."` will not do anything.  So if you
                end up only having the `manifest-version` key in the manifest, you can just remove the manifest and
                rely entirely on the built-in rules.
            """
            ),
        ),
    )
    api.pluggable_object_parser(
        OPARSER_MANIFEST_ROOT,
        MK_MANIFEST_DEFINITIONS,
        object_parser_key=OPARSER_MANIFEST_DEFINITIONS,
        on_end_parse_step=lambda _a, _b, _c, mp: mp._ensure_package_states_is_initialized(),
    )
    api.pluggable_manifest_rule(
        OPARSER_MANIFEST_DEFINITIONS,
        MK_MANIFEST_VARIABLES,
        ManifestVariablesParsedFormat,
        _handle_manifest_variables,
        source_format=Dict[str, str],
        inline_reference_documentation=reference_documentation(
            title="Manifest Variables (`variables`)",
            description=textwrap.dedent(
                """\
                It is possible to provide custom manifest variables via the `variables` attribute.  An example:

                    manifest-version: '0.1'
                    definitions:
                      variables:
                        LIBPATH: "/usr/lib/{{DEB_HOST_MULTIARCH}}"
                        SONAME: "1"
                    installations:
                      - install:
                           source: build/libfoo.so.{{SONAME}}*
                           # The quotes here is for the YAML parser's sake.
                           dest-dir: "{{LIBPATH}}"
                           into: libfoo{{SONAME}}

                The value of the `variables` key must be a mapping, where each key is a new variable name and
                the related value is the value of said key. The keys must be valid variable name and not shadow
                existing variables (that is, variables such as `PACKAGE` and `DEB_HOST_MULTIARCH` *cannot* be
                redefined). The value for each variable *can* refer to *existing* variables as seen in the
                example above.

                As usual, `debputy` will insist that all declared variables must be used.

                Limitations:
                 * When declaring variables that depends on another variable declared in the manifest, the
                   order is important. The variables are resolved from top to bottom.
                 * When a manifest variable depends on another manifest variable, the existing variable is
                   currently always resolved in source context. As a consequence, some variables such as
                   `{{PACKAGE}}` cannot be used when defining a variable. This restriction may be
                   lifted in the future.
            """
            ),
        ),
    )
    api.pluggable_manifest_rule(
        OPARSER_MANIFEST_ROOT,
        MK_INSTALLATIONS,
        List[InstallRule],
        _handle_installation_rules,
        expected_debputy_integration_mode=not_integrations(
            INTEGRATION_MODE_DH_DEBPUTY_RRR
        ),
        inline_reference_documentation=reference_documentation(
            title="Installations",
            description=textwrap.dedent(
                """\
        For source packages building a single binary, the `dh_auto_install` from debhelper will default to
        providing everything from upstream's install in the binary package.  The `debputy` tool matches this
        behaviour and accordingly, the `installations` feature is only relevant in this case when you need to
        manually specify something upstream's install did not cover.

        For sources, that build multiple binaries, where `dh_auto_install` does not detect anything to install,
        or when `dh_auto_install --destdir debian/tmp` is used, the `installations` section of the manifest is
        used to declare what goes into which binary package. An example:

            installations:
              - install:
                    sources: "usr/bin/foo"
                    into: foo
              - install:
                    sources: "usr/*"
                    into: foo-extra

        All installation rules are processed in order (top to bottom).  Once a path has been matched, it can
        no longer be matched by future rules.  In the above example, then `usr/bin/foo` would be in the `foo`
        package while everything in `usr` *except* `usr/bin/foo` would be in `foo-extra`.  If these had been
        ordered in reverse, the `usr/bin/foo` rule would not have matched anything and caused `debputy`
        to reject the input as an error on that basis.  This behaviour is similar to "DEP-5" copyright files,
        except the order is reversed ("DEP-5" uses "last match wins", where here we are doing "first match wins")

        In the rare case that some path need to be installed into two packages at the same time, then this is
        generally done by changing `into` into a list of packages.

        All installations are currently run in *source* package context.  This implies that:

          1) No package specific substitutions are available. Notably `{{PACKAGE}}` cannot be resolved.
          2) All conditions are evaluated in source context.  For 99.9% of users, this makes no difference,
             but there is a cross-build feature that changes the "per package" architecture which is affected.

        This is a limitation that should be fixed in `debputy`.

        **Attention debhelper users**: Note the difference between `dh_install` (etc.) vs. `debputy` on
        overlapping matches for installation.
            """
            ),
        ),
    )
    api.pluggable_object_parser(
        OPARSER_MANIFEST_ROOT,
        MK_PACKAGES,
        object_parser_key=OPARSER_PACKAGES,
        on_end_parse_step=lambda _a, _b, _c, mp: mp._ensure_package_states_is_initialized(),
        nested_in_package_context=True,
    )

    register_build_system_rules(api)


class ManifestVersionFormat(DebputyParsedContent):
    manifest_version: ManifestVersion


class ListOfInstallRulesFormat(DebputyParsedContent):
    elements: List[InstallRule]


class DictFormat(DebputyParsedContent):
    mapping: Dict[str, Any]


class ManifestVariablesParsedFormat(DebputyParsedContent):
    variables: Dict[str, str]


def _handle_version(
    _name: str,
    parsed_data: ManifestVersionFormat,
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> str:
    manifest_version = parsed_data["manifest_version"]
    if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestParseException(
            "Unsupported manifest-version.  This implementation supports the following versions:"
            f' {", ".join(repr(v) for v in SUPPORTED_MANIFEST_VERSIONS)}"'
        )
    return manifest_version


def _handle_manifest_variables(
    _name: str,
    parsed_data: ManifestVariablesParsedFormat,
    variables_path: AttributePath,
    parser_context: ParserContextData,
) -> None:
    variables = parsed_data.get("variables", {})
    resolved_vars: Dict[str, Tuple[str, AttributePath]] = {}
    manifest_parser: "YAMLManifestParser" = cast("YAMLManifestParser", parser_context)
    substitution = manifest_parser.substitution
    for key, value_raw in variables.items():
        key_path = variables_path[key]
        if not SUBST_VAR_RE.match("{{" + key + "}}"):
            raise ManifestParseException(
                f"The variable at {key_path.path_key_lc} has an invalid name and therefore cannot"
                " be used."
            )
        if substitution.variable_state(key) != VariableNameState.UNDEFINED:
            raise ManifestParseException(
                f'The variable "{key}" is already reserved/defined. Error triggered by'
                f" {key_path.path_key_lc}."
            )
        try:
            value = substitution.substitute(value_raw, key_path.path)
        except DebputySubstitutionError:
            if not resolved_vars:
                raise
            # See if flushing the variables work
            substitution = manifest_parser.add_extra_substitution_variables(
                **resolved_vars
            )
            resolved_vars = {}
            value = substitution.substitute(value_raw, key_path.path)
        resolved_vars[key] = (value, key_path)
        substitution = manifest_parser.add_extra_substitution_variables(**resolved_vars)


def _handle_installation_rules(
    _name: str,
    parsed_data: List[InstallRule],
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> List[Any]:
    return parsed_data


def _handle_opaque_dict(
    _name: str,
    parsed_data: DictFormat,
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> Dict[str, Any]:
    return parsed_data["mapping"]
