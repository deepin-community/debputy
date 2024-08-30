import dataclasses
import json
import os
import subprocess
import textwrap
from typing import (
    NotRequired,
    TypedDict,
    Self,
    cast,
    Dict,
    Mapping,
    Sequence,
    MutableMapping,
    Iterable,
    Container,
    List,
    Tuple,
    Union,
    Optional,
    TYPE_CHECKING,
    Literal,
)

from debian.debian_support import Version

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy._manifest_constants import MK_BUILDS
from debputy.manifest_parser.base_types import (
    BuildEnvironmentDefinition,
    DebputyParsedContentStandardConditional,
    FileSystemExactMatchRule,
)
from debputy.manifest_parser.exceptions import (
    ManifestParseException,
    ManifestInvalidUserDataException,
)
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.plugin.api import reference_documentation
from debputy.plugin.api.impl import (
    DebputyPluginInitializerProvider,
)
from debputy.plugin.api.parser_tables import OPARSER_MANIFEST_ROOT
from debputy.plugin.api.spec import (
    documented_attr,
    INTEGRATION_MODE_FULL,
    only_integrations,
    VirtualPath,
)
from debputy.plugin.api.std_docs import docs_from
from debputy.plugin.debputy.to_be_api_types import (
    BuildRule,
    StepBasedBuildSystemRule,
    OptionalInstallDirectly,
    BuildSystemCharacteristics,
    OptionalBuildDirectory,
    OptionalInSourceBuild,
    MakefileSupport,
    BuildRuleParsedFormat,
    debputy_build_system,
    CleanHelper,
    NinjaBuildSupport,
)
from debputy.types import EnvironmentModification
from debputy.util import (
    _warn,
    run_build_system_command,
    _error,
    PerlConfigVars,
    resolve_perl_config,
    generated_content_dir,
)

if TYPE_CHECKING:
    from debputy.build_support.build_context import BuildContext
    from debputy.highlevel_manifest import HighLevelManifest


PERL_CMD = "perl"


def register_build_system_rules(api: DebputyPluginInitializerProvider) -> None:
    register_build_keywords(api)
    register_build_rules(api)


def register_build_keywords(api: DebputyPluginInitializerProvider) -> None:

    api.pluggable_manifest_rule(
        OPARSER_MANIFEST_ROOT,
        "build-environments",
        List[NamedEnvironmentSourceFormat],
        _parse_build_environments,
        expected_debputy_integration_mode=only_integrations(INTEGRATION_MODE_FULL),
        inline_reference_documentation=reference_documentation(
            title="Build Environments (`build-environments`)",
            description=textwrap.dedent(
                """\
                Define named environments to set the environment for any build commands that needs
                a non-default environment.

                The environment definitions can be used to tweak the environment variables used by the
                build commands. An example:

                    build-environments:
                      - name: custom-env
                        set:
                          ENV_VAR: foo
                          ANOTHER_ENV_VAR: bar
                    builds:
                      - autoconf:
                          environment: custom-env

                The environment definition has multiple attributes for setting environment variables
                which determines when the definition is applied. The resulting environment is the
                result of the following order of operations.

                  1. The environment `debputy` received from its parent process.
                  2. Apply all the variable definitions from `set` (if the attribute is present)
                  3. Apply all computed variables (such as variables from `dpkg-buildflags`).
                  4. Apply all the variable definitions from `override` (if the attribute is present)
                  5. Remove all variables listed in `unset` (if the attribute is present).

                Accordingly, both `override` and `unset` will overrule any computed variables while
                `set` will be overruled by any computed variables.

                Note that these variables are not available via manifest substitution (they are only
                visible to build commands). They are only available to build commands.
            """
            ),
            attributes=[
                documented_attr(
                    "name",
                    textwrap.dedent(
                        """\
                        The name of the environment

                        The name is used to reference the environment from build rules.
                    """
                    ),
                ),
                documented_attr(
                    "set",
                    textwrap.dedent(
                        """\
                        A mapping of environment variables to be set.

                        Note these environment variables are set before computed variables (such
                        as `dpkg-buildflags`) are provided. They can affect the content of the
                        computed variables, but they cannot overrule them. If you need to overrule
                        a computed variable, please use `override` instead.
                """
                    ),
                ),
                documented_attr(
                    "override",
                    textwrap.dedent(
                        """\
                        A mapping of environment variables to set.

                        Similar to `set`, but it can overrule computed variables like those from
                        `dpkg-buildflags`.
                """
                    ),
                ),
                documented_attr(
                    "unset",
                    textwrap.dedent(
                        """\
                        A list of environment variables to unset.

                        Any environment variable named here will be unset. No warnings or errors
                        will be raised if a given variable was not set.
                    """
                    ),
                ),
            ],
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#build-environment-build-environment",
        ),
    )
    api.pluggable_manifest_rule(
        OPARSER_MANIFEST_ROOT,
        "default-build-environment",
        EnvironmentSourceFormat,
        _parse_default_environment,
        expected_debputy_integration_mode=only_integrations(INTEGRATION_MODE_FULL),
        inline_reference_documentation=reference_documentation(
            title="Default Build Environment (`default-build-environment`)",
            description=textwrap.dedent(
                """\
                Define the environment variables used in all build commands that uses the default
                environment.

                The environment definition can be used to tweak the environment variables used by the
                build commands. An example:
                
                    default-build-environment:
                      set:
                        ENV_VAR: foo
                        ANOTHER_ENV_VAR: bar

                The environment definition has multiple attributes for setting environment variables
                which determines when the definition is applied. The resulting environment is the
                result of the following order of operations.
                
                  1. The environment `debputy` received from its parent process.
                  2. Apply all the variable definitions from `set` (if the attribute is present)
                  3. Apply all computed variables (such as variables from `dpkg-buildflags`).
                  4. Apply all the variable definitions from `override` (if the attribute is present)
                  5. Remove all variables listed in `unset` (if the attribute is present).

                Accordingly, both `override` and `unset` will overrule any computed variables while
                `set` will be overruled by any computed variables.

                Note that these variables are not available via manifest substitution (they are only
                visible to build commands). They are only available to build commands.
            """
            ),
            attributes=[
                documented_attr(
                    "set",
                    textwrap.dedent(
                        """\
                        A mapping of environment variables to be set.

                        Note these environment variables are set before computed variables (such
                        as `dpkg-buildflags`) are provided. They can affect the content of the
                        computed variables, but they cannot overrule them. If you need to overrule
                        a computed variable, please use `override` instead.
                """
                    ),
                ),
                documented_attr(
                    "override",
                    textwrap.dedent(
                        """\
                        A mapping of environment variables to set.

                        Similar to `set`, but it can overrule computed variables like those from
                        `dpkg-buildflags`.
                """
                    ),
                ),
                documented_attr(
                    "unset",
                    textwrap.dedent(
                        """\
                        A list of environment variables to unset.

                        Any environment variable named here will be unset. No warnings or errors
                        will be raised if a given variable was not set.
                    """
                    ),
                ),
            ],
            reference_documentation_url=f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#build-environment-build-environment",
        ),
    )
    api.pluggable_manifest_rule(
        OPARSER_MANIFEST_ROOT,
        MK_BUILDS,
        List[BuildRule],
        _handle_build_rules,
        expected_debputy_integration_mode=only_integrations(INTEGRATION_MODE_FULL),
        inline_reference_documentation=reference_documentation(
            title="Build rules",
            description=textwrap.dedent(
                """\
                Define how to build the upstream part of the package. Usually this is done via "build systems",
                which also defines the clean rules.
            """
            ),
        ),
    )


def register_build_rules(api: DebputyPluginInitializerProvider) -> None:
    api.register_build_system(ParsedAutoconfBuildRuleDefinition)
    api.register_build_system(ParsedMakeBuildRuleDefinition)

    api.register_build_system(ParsedPerlBuildBuildRuleDefinition)
    api.register_build_system(ParsedPerlMakeMakerBuildRuleDefinition)
    api.register_build_system(ParsedDebhelperBuildRuleDefinition)

    api.register_build_system(ParsedCMakeBuildRuleDefinition)
    api.register_build_system(ParsedMesonBuildRuleDefinition)

    api.register_build_system(ParsedQmakeBuildRuleDefinition)
    api.register_build_system(ParsedQmake6BuildRuleDefinition)


class EnvironmentSourceFormat(TypedDict):
    set: NotRequired[Dict[str, str]]
    override: NotRequired[Dict[str, str]]
    unset: NotRequired[List[str]]


class NamedEnvironmentSourceFormat(EnvironmentSourceFormat):
    name: str


_READ_ONLY_ENV_VARS = {
    "DEB_CHECK_COMMAND": None,
    "DEB_SIGN_KEYID": None,
    "DEB_SIGN_KEYFILE": None,
    "DEB_BUILD_OPTIONS": "DEB_BUILD_MAINT_OPTIONS",
    "DEB_BUILD_PROFILES": None,
    "DEB_RULES_REQUIRES_ROOT": None,
    "DEB_GAIN_ROOT_COMMAND": None,
    "DH_EXTRA_ADDONS": None,
    "DH_NO_ACT": None,
}


def _check_variables(
    env_vars: Iterable[str],
    attribute_path: AttributePath,
) -> None:
    for env_var in env_vars:
        if env_var not in _READ_ONLY_ENV_VARS:
            continue
        alt = _READ_ONLY_ENV_VARS.get(env_var)
        var_path = attribute_path[env_var].path_key_lc
        if alt is None:
            raise ManifestParseException(
                f"The variable {env_var} cannot be modified by the manifest. This restriction is generally"
                f" because the build should not touch those variables or changing them have no effect"
                f" (since the consumer will not see the change). The problematic definition was {var_path}"
            )
        else:
            raise ManifestParseException(
                f"The variable {env_var} cannot be modified by the manifest. This restriction is generally"
                f" because the build should not touch those variables or changing them have no effect"
                f" (since the consumer will not see the change). Depending on what you are trying to"
                f' accomplish, the variable "{alt}" might be a suitable alternative.'
                f" The problematic definition was {var_path}"
            )


def _no_overlap(
    lhs: Iterable[Union[str, Tuple[int, str]]],
    rhs: Container[str],
    lhs_key: str,
    rhs_key: str,
    redundant_key: str,
    attribute_path: AttributePath,
) -> None:
    for kt in lhs:
        if isinstance(kt, tuple):
            lhs_path_key, var = kt
        else:
            lhs_path_key = var = kt
        if var not in rhs:
            continue
        lhs_path = attribute_path[lhs_key][lhs_path_key].path_key_lc
        rhs_path = attribute_path[rhs_key][var].path_key_lc
        r_path = lhs_path if redundant_key == rhs_key else rhs_path
        raise ManifestParseException(
            f"The environment variable {var} was declared in {lhs_path} and {rhs_path}."
            f" Due to how the variables are applied, the definition in {r_path} is redundant"
            f" and can effectively be removed. Please review the manifest and remove one of"
            f" the two definitions."
        )


@dataclasses.dataclass(slots=True, frozen=True)
class ManifestProvidedBuildEnvironment(BuildEnvironmentDefinition):

    name: str
    is_default: bool
    attribute_path: AttributePath
    parser_context: ParserContextData

    set_vars: Mapping[str, str]
    override_vars: Mapping[str, str]
    unset_vars: Sequence[str]

    @classmethod
    def from_environment_definition(
        cls,
        env: EnvironmentSourceFormat,
        attribute_path: AttributePath,
        parser_context: ParserContextData,
        is_default: bool = False,
    ) -> Self:
        reference_name: Optional[str]
        if is_default:
            name = "default-env"
            reference_name = None
        else:
            named_env = cast("NamedEnvironmentSourceFormat", env)
            name = named_env["name"]
            reference_name = name

        set_vars = env.get("set", {})
        override_vars = env.get("override", {})
        unset_vars = env.get("unset", [])
        _check_variables(set_vars, attribute_path["set"])
        _check_variables(override_vars, attribute_path["override"])
        _check_variables(unset_vars, attribute_path["unset"])

        if not set_vars and not override_vars and not unset_vars:
            raise ManifestParseException(
                f"The environment definition {attribute_path.path_key_lc} was empty. Please provide"
                " some content or delete the definition."
            )

        _no_overlap(
            enumerate(unset_vars),
            set_vars,
            "unset",
            "set",
            "set",
            attribute_path,
        )
        _no_overlap(
            enumerate(unset_vars),
            override_vars,
            "unset",
            "override",
            "override",
            attribute_path,
        )
        _no_overlap(
            override_vars,
            set_vars,
            "override",
            "set",
            "set",
            attribute_path,
        )

        r = cls(
            name,
            is_default,
            attribute_path,
            parser_context,
            set_vars,
            override_vars,
            unset_vars,
        )
        parser_context._register_build_environment(
            reference_name,
            r,
            attribute_path,
            is_default,
        )

        return r

    def update_env(self, env: MutableMapping[str, str]) -> None:
        if set_vars := self.set_vars:
            env.update(set_vars)
        dpkg_env = self.dpkg_buildflags_env(env, self.attribute_path.path_key_lc)
        self.log_computed_env(f"dpkg-buildflags [{self.name}]", dpkg_env)
        if overlapping_env := dpkg_env.keys() & set_vars.keys():
            for var in overlapping_env:
                key_lc = self.attribute_path["set"][var].path_key_lc
                _warn(
                    f'The variable "{var}" defined at {key_lc} is shadowed by a computed variable.'
                    f" If the manifest definition is more important, please define it via `override` rather than"
                    f" `set`."
                )
        env.update(dpkg_env)
        if override_vars := self.override_vars:
            env.update(override_vars)
        if unset_vars := self.unset_vars:
            for var in unset_vars:
                try:
                    del env[var]
                except KeyError:
                    pass


_MAKE_DEFAULT_TOOLS = [
    ("CC", "gcc"),
    ("CXX", "g++"),
    ("PKG_CONFIG", "pkg-config"),
]


class MakefileBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = ("_make_support", "_build_target", "_install_target", "_directory")

    def __init__(
        self,
        attributes: "ParsedMakeBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(attributes, attribute_path, parser_context)
        directory = attributes.get("directory")
        if directory is not None:
            self._directory = directory.match_rule.path
        else:
            self._directory = None
        self._make_support = MakefileSupport.from_build_system(self)
        self._build_target = attributes.get("build_target")
        self._test_target = attributes.get("test_target")
        self._install_target = attributes.get("install_target")

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        return any(p in source_root for p in ("Makefile", "makefile", "GNUmakefile"))

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="not-supported",
        )

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        # No configure step
        pass

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        extra_vars = []
        build_target = self._build_target
        if build_target is not None:
            extra_vars.append(build_target)
        if context.is_cross_compiling:
            for envvar, tool in _MAKE_DEFAULT_TOOLS:
                cross_tool = os.environ.get(envvar)
                if cross_tool is None:
                    cross_tool = context.cross_tool(tool)
                extra_vars.append(f"{envvar}={cross_tool}")
        self._make_support.run_make(
            context,
            *extra_vars,
            "INSTALL=install --strip-program=true",
            directory=self._directory,
        )

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._run_make_maybe_explicit_target(
            context,
            self._test_target,
            ["test", "check"],
        )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        self._run_make_maybe_explicit_target(
            context,
            self._install_target,
            ["install"],
            f"DESTDIR={dest_dir}",
            "AM_UPDATE_INFO_DIR=no",
            "INSTALL=install --strip-program=true",
        )

    def _run_make_maybe_explicit_target(
        self,
        context: "BuildContext",
        provided_target: Optional[str],
        fallback_targets: Sequence[str],
        *make_args: str,
    ) -> None:
        make_support = self._make_support
        if provided_target is not None:
            make_support.run_make(
                context,
                provided_target,
                *make_args,
                directory=self._directory,
            )
        else:
            make_support.run_first_existing_target_if_any(
                context,
                fallback_targets,
                *make_args,
                directory=self._directory,
            )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        self._make_support.run_first_existing_target_if_any(
            context,
            ["distclean", "realclean", "clean"],
        )


class PerlBuildBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = "configure_args"

    def __init__(
        self,
        attributes: "ParsedPerlBuildBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(attributes, attribute_path, parser_context)
        self.configure_args = attributes.get("configure_args", [])

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        return "Build.PL" in source_root

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="not-supported",
        )

    @staticmethod
    def _perl_cross_build_env(
        context: "BuildContext",
    ) -> Tuple[PerlConfigVars, Optional[EnvironmentModification]]:
        perl_config_data = resolve_perl_config(
            context.dpkg_architecture_variables,
            None,
        )
        if context.is_cross_compiling:
            perl5lib_dir = perl_config_data.cross_inc_dir
            if perl5lib_dir is not None:
                env_perl5lib = os.environ.get("PERL5LIB")
                if env_perl5lib is not None:
                    perl5lib_dir = (
                        perl5lib_dir + perl_config_data.path_sep + env_perl5lib
                    )
                env_mod = EnvironmentModification(
                    replacements=(("PERL5LIB", perl5lib_dir),),
                )
                return perl_config_data, env_mod
        return perl_config_data, None

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        perl_config_data, cross_env_mod = self._perl_cross_build_env(context)
        configure_env = EnvironmentModification(
            replacements=(
                ("PERL_MM_USE_DEFAULT", "1"),
                ("PKG_CONFIG", context.cross_tool("pkg-config")),
            )
        )
        if cross_env_mod is not None:
            configure_env = configure_env.combine(cross_env_mod)

        configure_cmd = [
            PERL_CMD,
            "Build.PL",
            "--installdirs",
            "vendor",
        ]
        cflags = os.environ.get("CFLAGS", "")
        cppflags = os.environ.get("CPPFLAGS", "")
        ldflags = os.environ.get("LDFLAGS", "")

        if cflags != "" or cppflags != "":
            configure_cmd.append("--config")
            combined = f"{cflags} {cppflags}".strip()
            configure_cmd.append(f"optimize={combined}")

        if ldflags != "" or cflags != "" or context.is_cross_compiling:
            configure_cmd.append("--config")
            combined = f"{perl_config_data.ld} {cflags} {ldflags}".strip()
            configure_cmd.append(f"ld={combined}")
        if self.configure_args:
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            configure_cmd.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )
        run_build_system_command(*configure_cmd, env_mod=configure_env)

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        _, cross_env_mod = self._perl_cross_build_env(context)
        run_build_system_command(PERL_CMD, "Build", env_mod=cross_env_mod)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        _, cross_env_mod = self._perl_cross_build_env(context)
        run_build_system_command(
            PERL_CMD,
            "Build",
            "test",
            "--verbose",
            "1",
            env_mod=cross_env_mod,
        )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        _, cross_env_mod = self._perl_cross_build_env(context)
        run_build_system_command(
            PERL_CMD,
            "Build",
            "install",
            "--destdir",
            dest_dir,
            "--create_packlist",
            "0",
            env_mod=cross_env_mod,
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        _, cross_env_mod = self._perl_cross_build_env(context)
        if os.path.lexists("Build"):
            run_build_system_command(
                PERL_CMD,
                "Build",
                "realclean",
                "--allow_mb_mismatch",
                "1",
                env_mod=cross_env_mod,
            )


class PerlMakeMakerBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = ("configure_args", "_make_support")

    def __init__(
        self,
        attributes: "ParsedPerlBuildBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(attributes, attribute_path, parser_context)
        self.configure_args = attributes.get("configure_args", [])
        self._make_support = MakefileSupport.from_build_system(self)

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        return "Makefile.PL" in source_root

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="not-supported",
        )

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        configure_env = EnvironmentModification(
            replacements=(
                ("PERL_MM_USE_DEFAULT", "1"),
                ("PERL_AUTOINSTALL", "--skipdeps"),
                ("PKG_CONFIG", context.cross_tool("pkg-config")),
            )
        )
        perl_args = []
        mm_args = ["INSTALLDIRS=vendor"]
        if "CFLAGS" in os.environ:
            mm_args.append(
                f"OPTIMIZE={os.environ['CFLAGS']} {os.environ['CPPFLAGS']}".rstrip()
            )

        perl_config_data = resolve_perl_config(
            context.dpkg_architecture_variables,
            None,
        )

        if "LDFLAGS" in os.environ:
            mm_args.append(
                f"LD={perl_config_data.ld} {os.environ['CFLAGS']} {os.environ['LDFLAGS']}"
            )

        if context.is_cross_compiling:
            perl5lib_dir = perl_config_data.cross_inc_dir
            if perl5lib_dir is not None:
                perl_args.append(f"-I{perl5lib_dir}")

        if self.configure_args:
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            mm_args.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )
        run_build_system_command(
            PERL_CMD,
            *perl_args,
            "Makefile.PL",
            *mm_args,
            env_mod=configure_env,
        )

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._make_support.run_make(context)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._make_support.run_first_existing_target_if_any(
            context,
            ["check", "test"],
            "TEST_VERBOSE=1",
        )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        is_mm_makefile = False
        with open("Makefile", "rb") as fd:
            for line in fd:
                if b"generated automatically by MakeMaker" in line:
                    is_mm_makefile = True
                    break

        install_args = [f"DESTDIR={dest_dir}"]

        # Special case for Makefile.PL that uses
        # Module::Build::Compat. PREFIX should not be passed
        # for those; it already installs into /usr by default.
        if is_mm_makefile:
            install_args.append("PREFIX=/usr")

        self._make_support.run_first_existing_target_if_any(
            context,
            ["install"],
            *install_args,
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        self._make_support.run_first_existing_target_if_any(
            context,
            ["distclean", "realclean", "clean"],
        )


class DebhelperBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = ("configure_args", "dh_build_system")

    def __init__(
        self,
        parsed_data: "ParsedDebhelperBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(parsed_data, attribute_path, parser_context)
        self.configure_args = parsed_data.get("configure_args", [])
        self.dh_build_system = parsed_data.get("dh_build_system")

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        try:
            v = subprocess.check_output(
                ["dh_assistant", "which-build-system"],
                # Packages without `debhelper-compat` will trigger an error, which will just be noise
                stderr=subprocess.DEVNULL,
                cwd=source_root.fs_path,
            )
        except subprocess.CalledProcessError:
            return False
        else:
            d = json.loads(v)
            build_system = d.get("build-system")
            return build_system is not None

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="supported-but-not-default",
        )

    def before_first_impl_step(
        self, *, stage: Literal["build", "clean"], **kwargs
    ) -> None:
        dh_build_system = self.dh_build_system
        if dh_build_system is None:
            return
        try:
            subprocess.check_call(
                ["dh_assistant", "which-build-system", f"-S{dh_build_system}"]
            )
        except FileNotFoundError:
            _error(
                "The debhelper build system assumes `dh_assistant` is available (`debhelper (>= 13.5~)`)"
            )
        except subprocess.SubprocessError:
            raise ManifestInvalidUserDataException(
                f'The debhelper build system "{dh_build_system}" does not seem to'
                f" be available according to"
                f" `dh_assistant which-build-system -S{dh_build_system}`"
            ) from None

    def _default_options(self) -> List[str]:
        default_options = []
        if self.dh_build_system is not None:
            default_options.append(f"-S{self.dh_build_system}")
        if self.build_directory is not None:
            default_options.append(f"-B{self.build_directory}")

        return default_options

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        if (
            os.path.lexists("configure.ac") or os.path.lexists("configure.in")
        ) and not os.path.lexists("debian/autoreconf.before"):
            run_build_system_command("dh_update_autotools_config")
            run_build_system_command("dh_autoreconf")

        default_options = self._default_options()
        configure_args = default_options.copy()
        if self.configure_args:
            configure_args.append("--")
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            configure_args.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )
        run_build_system_command("dh_auto_configure", *configure_args)

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        default_options = self._default_options()
        run_build_system_command("dh_auto_build", *default_options)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        default_options = self._default_options()
        run_build_system_command("dh_auto_test", *default_options)

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        default_options = self._default_options()
        run_build_system_command(
            "dh_auto_install",
            *default_options,
            f"--destdir={dest_dir}",
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        default_options = self._default_options()
        run_build_system_command("dh_auto_clean", *default_options)
        # The "global" clean logic takes care of `dh_autoreconf_clean` and `dh_clean`


class AutoconfBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = ("configure_args", "_make_support")

    def __init__(
        self,
        parsed_data: "ParsedAutoconfBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(parsed_data, attribute_path, parser_context)
        configure_args = [a for a in parsed_data.get("configure_args", [])]
        self.configure_args = configure_args
        self._make_support = MakefileSupport.from_build_system(self)

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="supported-and-default",
        )

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        if "configure.ac" in source_root:
            return True
        configure_in = source_root.get("configure.in")
        if configure_in is not None and configure_in.is_file:
            with configure_in.open(byte_io=True, buffering=4096) as fd:
                for no, line in enumerate(fd):
                    if no > 100:
                        break
                    if b"AC_INIT" in line or b"AC_PREREQ" in line:
                        return True
        configure = source_root.get("configure")
        if configure is None or not configure.is_executable or not configure.is_file:
            return False
        with configure.open(byte_io=True, buffering=4096) as fd:
            for no, line in enumerate(fd):
                if no > 10:
                    break
                if b"GNU Autoconf" in line:
                    return True
        return False

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        if (
            os.path.lexists("configure.ac") or os.path.lexists("configure.in")
        ) and not os.path.lexists("debian/autoreconf.before"):
            run_build_system_command("dh_update_autotools_config")
            run_build_system_command("dh_autoreconf")

        dpkg_architecture_variables = context.dpkg_architecture_variables
        multi_arch = dpkg_architecture_variables.current_host_multiarch
        silent_rules = (
            "--enable-silent-rules"
            if context.is_terse_build
            else "--disable-silent-rules"
        )

        configure_args = [
            f"--build={dpkg_architecture_variables['DEB_BUILD_GNU_TYPE']}",
            "--prefix=/usr",
            "--includedir=${prefix}/include",
            "--mandir=${prefix}/share/man",
            "--infodir=${prefix}/share/info",
            "--sysconfdir=/etc",
            "--localstatedir=/var",
            "--disable-option-checking",
            silent_rules,
            f"--libdir=${{prefix}}/{multi_arch}",
            "--runstatedir=/run",
            "--disable-maintainer-mode",
            "--disable-dependency-tracking",
        ]
        if dpkg_architecture_variables.is_cross_compiling:
            configure_args.append(
                f"--host={dpkg_architecture_variables['DEB_HOST_GNU_TYPE']}"
            )
        if self.configure_args:
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            configure_args.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )
        self.ensure_build_dir_exists()
        configure_script = self.relative_from_builddir_to_source("configure")
        with self.dump_logs_on_error("config.log"):
            run_build_system_command(
                configure_script,
                *configure_args,
                cwd=self.build_directory,
            )

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._make_support.run_make(context)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        limit = context.parallelization_limit(support_zero_as_unlimited=True)
        testsuite_flags = [f"-j{limit}"] if limit else ["-j"]

        if not context.is_terse_build:
            testsuite_flags.append("--verbose")
        self._make_support.run_first_existing_target_if_any(
            context,
            # Order is deliberately inverse compared to debhelper (#924052)
            ["check", "test"],
            f"TESTSUITEFLAGS={' '.join(testsuite_flags)}",
            "VERBOSE=1",
        )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        enable_parallelization = not os.path.lexists(self.build_dir_path("libtool"))
        self._make_support.run_first_existing_target_if_any(
            context,
            ["install"],
            f"DESTDIR={dest_dir}",
            "AM_UPDATE_INFO_DIR=no",
            enable_parallelization=enable_parallelization,
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        if self.out_of_source_build:
            return
        self._make_support.run_first_existing_target_if_any(
            context,
            ["distclean", "realclean", "clean"],
        )
        # The "global" clean logic takes care of `dh_autoreconf_clean` and `dh_clean`


class CMakeBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = (
        "configure_args",
        "target_build_system",
        "_make_support",
        "_ninja_support",
    )

    def __init__(
        self,
        parsed_data: "ParsedCMakeBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(parsed_data, attribute_path, parser_context)
        configure_args = [a for a in parsed_data.get("configure_args", [])]
        self.configure_args = configure_args
        self.target_build_system: Literal["make", "ninja"] = parsed_data.get(
            "target_build_system", "make"
        )
        self._make_support = MakefileSupport.from_build_system(self)
        self._ninja_support = NinjaBuildSupport.from_build_system(self)

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="required",
        )

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        return "CMakeLists.txt" in source_root

    @staticmethod
    def _default_cmake_env(
        build_context: "BuildContext",
    ) -> EnvironmentModification:
        replacements = {}
        if "DEB_PYTHON_INSTALL_LAYOUT" not in os.environ:
            replacements["DEB_PYTHON_INSTALL_LAYOUT"] = "deb"
        if "PKG_CONFIG" not in os.environ:
            replacements["PKG_CONFIG"] = build_context.cross_tool("pkg-config")
        return EnvironmentModification(
            replacements=tuple((k, v) for k, v in replacements.items())
        )

    @classmethod
    def cmake_generator(cls, target_build_system: Literal["make", "ninja"]) -> str:
        cmake_generators = {
            "make": "Unix Makefiles",
            "ninja": "Ninja",
        }
        return cmake_generators[target_build_system]

    @staticmethod
    def _compiler_and_cross_flags(
        context: "BuildContext",
        cmake_flags: List[str],
    ) -> None:

        if "CC" in os.environ:
            cmake_flags.append(f"-DCMAKE_C_COMPILER={os.environ['CC']}")
        elif context.is_cross_compiling:
            cmake_flags.append(f"-DCMAKE_C_COMPILER={context.cross_tool('gcc')}")

        if "CXX" in os.environ:
            cmake_flags.append(f"-DCMAKE_CXX_COMPILER={os.environ['CXX']}")
        elif context.is_cross_compiling:
            cmake_flags.append(f"-DCMAKE_CXX_COMPILER={context.cross_tool('g++')}")

        if context.is_cross_compiling:
            deb_host2cmake_system = {
                "linux": "Linux",
                "kfreebsd": "kFreeBSD",
                "hurd": "GNU",
            }

            gnu_cpu2system_processor = {
                "arm": "armv7l",
                "misp64el": "mips64",
                "powerpc64le": "ppc64le",
            }
            dpkg_architecture_variables = context.dpkg_architecture_variables

            try:
                system_name = deb_host2cmake_system[
                    dpkg_architecture_variables["DEB_HOST_ARCH_OS"]
                ]
            except KeyError as e:
                name = e.args[0]
                _error(
                    f"Cannot cross-compile via cmake: Missing CMAKE_SYSTEM_NAME for the DEB_HOST_ARCH_OS {name}"
                )

            gnu_cpu = dpkg_architecture_variables["DEB_HOST_GNU_CPU"]
            system_processor = gnu_cpu2system_processor.get(gnu_cpu, gnu_cpu)

            cmake_flags.append(f"-DCMAKE_SYSTEM_NAME={system_name}")
            cmake_flags.append(f"-DCMAKE_SYSTEM_PROCESSOR={system_processor}")

            pkg_config = context.cross_tool("pkg-config")
            # Historical uses. Current versions of cmake uses the env variable instead.
            cmake_flags.append(f"-DPKG_CONFIG_EXECUTABLE=/usr/bin/{pkg_config}")
            cmake_flags.append(f"-DPKGCONFIG_EXECUTABLE=/usr/bin/{pkg_config}")
            cmake_flags.append(
                f"-DQMAKE_EXECUTABLE=/usr/bin/{context.cross_tool('qmake')}"
            )

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        cmake_flags = [
            "-DCMAKE_INSTALL_PREFIX=/usr",
            "-DCMAKE_BUILD_TYPE=None",
            "-DCMAKE_INSTALL_SYSCONFDIR=/etc",
            "-DCMAKE_INSTALL_LOCALSTATEDIR=/var",
            "-DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON",
            "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF",
            "-DCMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY=ON",
            "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
            "-DCMAKE_INSTALL_RUNSTATEDIR=/run",
            "-DCMAKE_SKIP_INSTALL_ALL_DEPENDENCY=ON",
            "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON",
            f"-G{self.cmake_generator(self.target_build_system)}",
        ]
        if not context.is_terse_build:
            cmake_flags.append("-DCMAKE_VERBOSE_MAKEFILE=ON")

        self._compiler_and_cross_flags(context, cmake_flags)

        if self.configure_args:
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            cmake_flags.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )

        env_mod = self._default_cmake_env(context)
        if "CPPFLAGS" in os.environ:
            # CMake doesn't respect CPPFLAGS, see #653916.
            cppflags = os.environ["CPPFLAGS"]
            cflags = os.environ.get("CFLAGS", "") + f" {cppflags}".lstrip()
            cxxflags = os.environ.get("CXXFLAGS", "") + f" {cppflags}".lstrip()
            env_mod = env_mod.combine(
                # The debhelper build system never showed this delta, so people might find it annoying.
                EnvironmentModification(
                    replacements=(
                        ("CFLAGS", cflags),
                        ("CXXFLAGS", cxxflags),
                    )
                )
            )
        if "ASMFLAGS" not in os.environ and "ASFLAGS" in os.environ:
            env_mod = env_mod.combine(
                # The debhelper build system never showed this delta, so people might find it annoying.
                EnvironmentModification(
                    replacements=(("ASMFLAGS", os.environ["ASFLAGS"]),),
                )
            )
        self.ensure_build_dir_exists()
        source_dir_from_build_dir = self.relative_from_builddir_to_source()

        with self.dump_logs_on_error(
            "CMakeCache.txt",
            "CMakeFiles/CMakeOutput.log",
            "CMakeFiles/CMakeError.log",
        ):
            run_build_system_command(
                "cmake",
                *cmake_flags,
                source_dir_from_build_dir,
                cwd=self.build_directory,
                env_mod=env_mod,
            )

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        if self.target_build_system == "make":
            make_flags = []
            if not context.is_terse_build:
                make_flags.append("VERBOSE=1")
            self._make_support.run_make(context, *make_flags)
        else:
            self._ninja_support.run_ninja_build(context)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        env_mod = EnvironmentModification(
            replacements=(("CTEST_OUTPUT_ON_FAILURE", "1"),),
        )
        if self.target_build_system == "make":
            # Unlike make, CTest does not have "unlimited parallel" setting (-j implies
            # -j1). Therefore, we do not set "allow zero as unlimited" here.
            make_flags = [f"ARGS+=-j{context.parallelization_limit()}"]
            if not context.is_terse_build:
                make_flags.append("ARGS+=--verbose")
            self._make_support.run_first_existing_target_if_any(
                context,
                ["check", "test"],
                *make_flags,
                env_mod=env_mod,
            )
        else:
            self._ninja_support.run_ninja_test(context, env_mod=env_mod)

        limit = context.parallelization_limit(support_zero_as_unlimited=True)
        testsuite_flags = [f"-j{limit}"] if limit else ["-j"]

        if not context.is_terse_build:
            testsuite_flags.append("--verbose")
        self._make_support.run_first_existing_target_if_any(
            context,
            # Order is deliberately inverse compared to debhelper (#924052)
            ["check", "test"],
            f"TESTSUITEFLAGS={' '.join(testsuite_flags)}",
            "VERBOSE=1",
        )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        env_mod = EnvironmentModification(
            replacements=(
                ("LC_ALL", "C.UTF-8"),
                ("DESTDIR", dest_dir),
            )
        ).combine(self._default_cmake_env(context))
        run_build_system_command(
            "cmake",
            "--install",
            self.build_directory,
            env_mod=env_mod,
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        if self.out_of_source_build:
            return
        if self.target_build_system == "make":
            # Keep it here in case we change the `required` "out of source" to "supported-default"
            self._make_support.run_first_existing_target_if_any(
                context,
                ["distclean", "realclean", "clean"],
            )
        else:
            self._ninja_support.run_ninja_clean(context)


class MesonBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = (
        "configure_args",
        "_ninja_support",
    )

    def __init__(
        self,
        parsed_data: "ParsedMesonBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(parsed_data, attribute_path, parser_context)
        configure_args = [a for a in parsed_data.get("configure_args", [])]
        self.configure_args = configure_args
        self._ninja_support = NinjaBuildSupport.from_build_system(self)

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="required",
        )

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        return "meson.build" in source_root

    @staticmethod
    def _default_meson_env() -> EnvironmentModification:
        replacements = {
            "LC_ALL": "C.UTF-8",
        }
        if "DEB_PYTHON_INSTALL_LAYOUT" not in os.environ:
            replacements["DEB_PYTHON_INSTALL_LAYOUT"] = "deb"
        return EnvironmentModification(
            replacements=tuple((k, v) for k, v in replacements.items())
        )

    @classmethod
    def cmake_generator(cls, target_build_system: Literal["make", "ninja"]) -> str:
        cmake_generators = {
            "make": "Unix Makefiles",
            "ninja": "Ninja",
        }
        return cmake_generators[target_build_system]

    @staticmethod
    def _cross_flags(
        context: "BuildContext",
        meson_flags: List[str],
    ) -> None:
        if not context.is_cross_compiling:
            return
        # Needs a cross-file http://mesonbuild.com/Cross-compilation.html
        cross_files_dir = os.path.abspath(
            generated_content_dir(
                subdir_key="meson-cross-files",
            )
        )
        host_arch = context.dpkg_architecture_variables.current_host_arch
        cross_file = os.path.join(cross_files_dir, f"meson-cross-file-{host_arch}.conf")
        if not os.path.isfile(cross_file):
            env = os.environ
            if env.get("LC_ALL") != "C.UTF-8":
                env = dict(env)
                env["LC_ALL"] = "C.UTF-8"
            else:
                env = None
            subprocess.check_call(
                [
                    "/usr/share/meson/debcrossgen",
                    f"--arch={host_arch}",
                    f"-o{cross_file}",
                ],
                stdout=subprocess.DEVNULL,
                env=env,
            )

        meson_flags.append("--cross-file")
        meson_flags.append(cross_file)

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        meson_version = Version(
            subprocess.check_output(
                ["meson", "--version"],
                encoding="utf-8",
            ).strip()
        )
        dpkg_architecture_variables = context.dpkg_architecture_variables

        meson_flags = [
            "--wrap-mode=odownload",
            "--buildtype=plain",
            "--sysconfdir=/etc",
            "--localstatedir=/var",
            f"--libdir=lib/{dpkg_architecture_variables.current_host_multiarch}",
            "--auto-features=enabled",
        ]
        if meson_version >= Version("1.2.0"):
            # There was a behaviour change in Meson 1.2.0: previously
            # byte-compilation wasn't supported, but since 1.2.0 it is on by
            # default. We can only use this option to turn it off in versions
            # where the option exists.
            meson_flags.append("-Dpython.bytecompile=-1")

        self._cross_flags(context, meson_flags)

        if self.configure_args:
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            meson_flags.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )

        env_mod = self._default_meson_env()

        self.ensure_build_dir_exists()
        source_dir_from_build_dir = self.relative_from_builddir_to_source()

        with self.dump_logs_on_error("meson-logs/meson-log.txt"):
            run_build_system_command(
                "meson",
                "setup",
                source_dir_from_build_dir,
                *meson_flags,
                cwd=self.build_directory,
                env_mod=env_mod,
            )

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._ninja_support.run_ninja_build(context)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        env_mod = EnvironmentModification(
            replacements=(("MESON_TESTTHREDS", f"{context.parallelization_limit()}"),),
        ).combine(self._default_meson_env())
        with self.dump_logs_on_error("meson-logs/testlog.txt"):
            run_build_system_command(
                "meson",
                "test",
                env_mod=env_mod,
                cwd=self.build_directory,
            )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        run_build_system_command(
            "meson",
            "install",
            "--destdir",
            dest_dir,
            env_mod=self._default_meson_env(),
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        # `debputy` will handle all the cleanup for us by virtue of "out of source build"
        assert self.out_of_source_build


def _add_qmake_flag(options: List[str], envvar: str, *, include_cppflags: bool) -> None:
    value = os.environ.get(envvar)
    if value is None:
        return
    if include_cppflags:
        cppflags = os.environ.get("CPPFLAGS")
        if cppflags:
            value = f"{value} {cppflags}"

    options.append(f"QMAKE_{envvar}_RELEASE={value}")
    options.append(f"QMAKE_{envvar}_DEBUG={value}")


class ParsedGenericQmakeBuildRuleDefinition(
    OptionalInstallDirectly,
    OptionalInSourceBuild,
    OptionalBuildDirectory,
):
    configure_args: NotRequired[List[str]]


class AbstractQmakeBuildSystemRule(StepBasedBuildSystemRule):

    __slots__ = ("configure_args", "_make_support")

    def __init__(
        self,
        parsed_data: "ParsedGenericQmakeBuildRuleDefinition",
        attribute_path: AttributePath,
        parser_context: Union[ParserContextData, "HighLevelManifest"],
    ) -> None:
        super().__init__(parsed_data, attribute_path, parser_context)
        configure_args = [a for a in parsed_data.get("configure_args", [])]
        self.configure_args = configure_args
        self._make_support = MakefileSupport.from_build_system(self)

    @classmethod
    def characteristics(cls) -> BuildSystemCharacteristics:
        return BuildSystemCharacteristics(
            out_of_source_builds="supported-and-default",
        )

    @classmethod
    def auto_detect_build_system(
        cls,
        source_root: VirtualPath,
        *args,
        **kwargs,
    ) -> bool:
        return any(p.name.endswith(".pro") for p in source_root.iterdir)

    @classmethod
    def os_mkspec_mapping(cls) -> Mapping[str, str]:
        return {
            "linux": "linux-g++",
            "kfreebsd": "gnukfreebsd-g++",
            "hurd": "hurd-g++",
        }

    def qmake_command(self) -> str:
        raise NotImplementedError

    def configure_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:

        configure_args = [
            "-makefile",
        ]
        qmake_cmd = context.cross_tool(self.qmake_command())

        if context.is_cross_compiling:
            host_os = context.dpkg_architecture_variables["DEB_HOST_ARCH_OS"]
            os2mkspec = self.os_mkspec_mapping()
            try:
                spec = os2mkspec[host_os]
            except KeyError:
                _error(
                    f'Sorry, `debputy` cannot cross build this package for "{host_os}".'
                    f' Missing a "DEB OS -> qmake -spec <VALUE>" mapping.'
                )
            configure_args.append("-spec")
            configure_args.append(spec)

        _add_qmake_flag(configure_args, "CFLAGS", include_cppflags=True)
        _add_qmake_flag(configure_args, "CXXFLAGS", include_cppflags=True)
        _add_qmake_flag(configure_args, "LDFLAGS", include_cppflags=False)

        configure_args.append("QMAKE_STRIP=:")
        configure_args.append("PREFIX=/usr")

        if self.configure_args:
            substitution = self.substitution
            attr_path = self.attribute_path["configure_args"]
            configure_args.extend(
                substitution.substitute(v, attr_path[i].path)
                for i, v in enumerate(self.configure_args)
            )

        self.ensure_build_dir_exists()
        if not self.out_of_source_build:
            configure_args.append(self.relative_from_builddir_to_source())

        with self.dump_logs_on_error("config.log"):
            run_build_system_command(
                qmake_cmd,
                *configure_args,
                cwd=self.build_directory,
            )

    def build_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        self._make_support.run_make(context)

    def test_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        **kwargs,
    ) -> None:
        limit = context.parallelization_limit(support_zero_as_unlimited=True)
        testsuite_flags = [f"-j{limit}"] if limit else ["-j"]

        if not context.is_terse_build:
            testsuite_flags.append("--verbose")
        self._make_support.run_first_existing_target_if_any(
            context,
            # Order is deliberately inverse compared to debhelper (#924052)
            ["check", "test"],
            f"TESTSUITEFLAGS={' '.join(testsuite_flags)}",
            "VERBOSE=1",
        )

    def install_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        dest_dir: str,
        **kwargs,
    ) -> None:
        enable_parallelization = not os.path.lexists(self.build_dir_path("libtool"))
        self._make_support.run_first_existing_target_if_any(
            context,
            ["install"],
            f"DESTDIR={dest_dir}",
            "AM_UPDATE_INFO_DIR=no",
            enable_parallelization=enable_parallelization,
        )

    def clean_impl(
        self,
        context: "BuildContext",
        manifest: "HighLevelManifest",
        clean_helper: "CleanHelper",
        **kwargs,
    ) -> None:
        if self.out_of_source_build:
            return
        self._make_support.run_first_existing_target_if_any(
            context,
            ["distclean", "realclean", "clean"],
        )


class QmakeBuildSystemRule(AbstractQmakeBuildSystemRule):

    def qmake_command(self) -> str:
        return "qmake"


class Qmake6BuildSystemRule(AbstractQmakeBuildSystemRule):

    def qmake_command(self) -> str:
        return "qmake6"


@debputy_build_system(
    "make",
    MakefileBuildSystemRule,
    auto_detection_shadows_build_systems="debhelper",
    online_reference_documentation=reference_documentation(
        title="Make Build System",
        description=textwrap.dedent(
            ""
            """\
        Run a plain `make` file with nothing else.

        This build system will attempt to use `make` to leverage instructions
        in a makefile (such as, `Makefile` or `GNUMakefile`).

        By default, the makefile build system assumes it should use "in-source"
        build semantics. If needed be, an explicit `build-directory` can be
        provided if the `Makefile` is not in the source folder but instead in
        some other directory.
        """
        ),
        attributes=[
            documented_attr(
                "directory",
                textwrap.dedent(
                    """\
                The directory from which to run make if it is not the source root

                This works like using `make -C DIRECTORY ...` (or `cd DIRECTORY && make ...`).
                """
                ),
            ),
            documented_attr(
                "build_target",
                textwrap.dedent(
                    """\
                The target name to use for the "build" step.

                If omitted, `make` will be run without any explicit target leaving it to decide
                the default.
                """
                ),
            ),
            documented_attr(
                "test_target",
                textwrap.dedent(
                    """\
                The target name to use for the "test" step.

                If omitted, `make check` or `make test` will be used if it looks like `make`
                will accept one of those targets. Otherwise, the step will be skipped.
                """
                ),
            ),
            documented_attr(
                "install_target",
                textwrap.dedent(
                    """\
                The target name to use for the "install" step.

                If omitted, `make install` will be used if it looks like `make` will accept that target.
                Otherwise, the step will be skipped.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedMakeBuildRuleDefinition(
    OptionalInstallDirectly,
):
    directory: NotRequired[FileSystemExactMatchRule]
    build_target: NotRequired[str]
    test_target: NotRequired[str]
    install_target: NotRequired[str]


@debputy_build_system(
    "autoconf",
    AutoconfBuildSystemRule,
    auto_detection_shadows_build_systems=["debhelper", "make"],
    online_reference_documentation=reference_documentation(
        title="Autoconf Build System",
        description=textwrap.dedent(
            """\
        Run an autoconf-based build system as the upstream build system.

        This build rule will attempt to use autoreconf to update the `configure`
        script before running the `configure` script if needed. Otherwise, it
        follows the classic `./configure && make && make install` pattern.

        The build rule uses "out of source" builds by default since it is easier
        and more reliable for clean and makes it easier to support multiple
        builds (that is, two or more build systems for the same source package).
        This is in contract to `debhelper`, which defaults to "in source" builds
        for `autoconf`. If you need that behavior, please set
        `perform-in-source-build: true`.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `configure` script.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                OptionalInSourceBuild,
                OptionalBuildDirectory,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedAutoconfBuildRuleDefinition(
    OptionalInstallDirectly,
    OptionalInSourceBuild,
    OptionalBuildDirectory,
):
    configure_args: NotRequired[List[str]]


@debputy_build_system(
    "cmake",
    CMakeBuildSystemRule,
    auto_detection_shadows_build_systems=["debhelper", "make"],
    online_reference_documentation=reference_documentation(
        title="CMake Build System",
        description=textwrap.dedent(
            """\
        Run an cmake-based build system as the upstream build system.

        The build rule uses "out of source" builds.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `cmake` command.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                OptionalBuildDirectory,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedCMakeBuildRuleDefinition(
    OptionalInstallDirectly,
    OptionalBuildDirectory,
):
    configure_args: NotRequired[List[str]]
    target_build_system: Literal["make", "ninja"]


@debputy_build_system(
    "meson",
    MesonBuildSystemRule,
    auto_detection_shadows_build_systems=["debhelper", "make"],
    online_reference_documentation=reference_documentation(
        title="Meson Build System",
        description=textwrap.dedent(
            """\
        Run an meson-based build system as the upstream build system.

        The build rule uses "out of source" builds.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `meson` command.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                OptionalBuildDirectory,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedMesonBuildRuleDefinition(
    OptionalInstallDirectly,
    OptionalBuildDirectory,
):
    configure_args: NotRequired[List[str]]


@debputy_build_system(
    "perl-build",
    PerlBuildBuildSystemRule,
    auto_detection_shadows_build_systems=[
        "debhelper",
        "make",
        "perl-makemaker",
    ],
    online_reference_documentation=reference_documentation(
        title='Perl "Build.PL" Build System',
        description=textwrap.dedent(
            """\
        Build using the `Build.PL` Build system used by some Perl packages.

        This build rule will attempt to use the `Build.PL` script to build the
        upstream code.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `Build.PL` script.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedPerlBuildBuildRuleDefinition(
    OptionalInstallDirectly,
):
    configure_args: NotRequired[List[str]]


@debputy_build_system(
    "debhelper",
    DebhelperBuildSystemRule,
    online_reference_documentation=reference_documentation(
        title="Debhelper Build System",
        description=textwrap.dedent(
            """\
        Delegate to a debhelper provided build system

        This build rule will attempt to use the `dh_auto_*` tools to build the
        upstream code. By default, `dh_auto_*` will use auto-detection to determine
        which build system they will use. This can be overridden by the
        `dh-build-system` attribute.
        """
        ),
        attributes=[
            documented_attr(
                "dh_build_system",
                textwrap.dedent(
                    """\
                    Which debhelper build system to use. This attribute is passed to
                    the `dh_auto_*` commands as the `-S` parameter, so any value valid
                    for that will be accepted.

                    Note that many debhelper build systems require extra build
                    dependencies before they can be used. Please consult the documentation
                    of the relevant debhelper build system for details.
                """
                ),
            ),
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to underlying configuration command
                     (via `dh_auto_configure -- <configure-args`).
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                OptionalBuildDirectory,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedDebhelperBuildRuleDefinition(
    OptionalInstallDirectly,
    OptionalBuildDirectory,
):
    configure_args: NotRequired[List[str]]
    dh_build_system: NotRequired[str]


@debputy_build_system(
    "perl-makemaker",
    PerlMakeMakerBuildSystemRule,
    auto_detection_shadows_build_systems=[
        "debhelper",
        "make",
    ],
    online_reference_documentation=reference_documentation(
        title='Perl "MakeMaker" Build System',
        description=textwrap.dedent(
            """\
        Build using the "MakeMaker" Build system used by some Perl packages.

        This build rule will attempt to use the `Makefile.PL` script to build the
        upstream code.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `Makefile.PL` script.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedPerlMakeMakerBuildRuleDefinition(
    OptionalInstallDirectly,
):
    configure_args: NotRequired[List[str]]


@debputy_build_system(
    "qmake",
    QmakeBuildSystemRule,
    auto_detection_shadows_build_systems=[
        "debhelper",
        "make",
        # Open question, should this shadow "qmake6" and later?
    ],
    online_reference_documentation=reference_documentation(
        title='QT "qmake" Build System',
        description=textwrap.dedent(
            """\
        Build using the "qmake" by QT.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `qmake` command.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                OptionalInSourceBuild,
                OptionalBuildDirectory,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedQmakeBuildRuleDefinition(ParsedGenericQmakeBuildRuleDefinition):
    pass


@debputy_build_system(
    "qmake6",
    Qmake6BuildSystemRule,
    auto_detection_shadows_build_systems=[
        "debhelper",
        "make",
    ],
    online_reference_documentation=reference_documentation(
        title='QT "qmake6" Build System',
        description=textwrap.dedent(
            """\
        Build using the "qmake6" from the `qmake6` package.  This is like the `qmake` system
        but is specifically for QT6.
        """
        ),
        attributes=[
            documented_attr(
                "configure_args",
                textwrap.dedent(
                    """\
                    Arguments to be passed to the `qmake6` command.
                """
                ),
            ),
            *docs_from(
                DebputyParsedContentStandardConditional,
                OptionalInstallDirectly,
                OptionalInSourceBuild,
                OptionalBuildDirectory,
                BuildRuleParsedFormat,
            ),
        ],
    ),
)
class ParsedQmake6BuildRuleDefinition(ParsedGenericQmakeBuildRuleDefinition):
    pass


def _parse_default_environment(
    _name: str,
    parsed_data: EnvironmentSourceFormat,
    attribute_path: AttributePath,
    parser_context: ParserContextData,
) -> ManifestProvidedBuildEnvironment:
    return ManifestProvidedBuildEnvironment.from_environment_definition(
        parsed_data,
        attribute_path,
        parser_context,
        is_default=True,
    )


def _parse_build_environments(
    _name: str,
    parsed_data: List[NamedEnvironmentSourceFormat],
    attribute_path: AttributePath,
    parser_context: ParserContextData,
) -> List[ManifestProvidedBuildEnvironment]:
    return [
        ManifestProvidedBuildEnvironment.from_environment_definition(
            value,
            attribute_path[idx],
            parser_context,
            is_default=False,
        )
        for idx, value in enumerate(parsed_data)
    ]


def _handle_build_rules(
    _name: str,
    parsed_data: List[BuildRule],
    _attribute_path: AttributePath,
    _parser_context: ParserContextData,
) -> List[BuildRule]:
    return parsed_data
