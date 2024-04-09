import ctypes
import ctypes.util
import functools
import itertools
import textwrap
import time
from datetime import datetime
from typing import (
    cast,
    NotRequired,
    Optional,
    Tuple,
    Union,
    Type,
    TypedDict,
    List,
    Annotated,
    Any,
    Dict,
    Callable,
)

from debian.changelog import Changelog
from debian.deb822 import Deb822

from debputy import DEBPUTY_DOC_ROOT_DIR
from debputy._manifest_constants import (
    MK_CONFFILE_MANAGEMENT_X_OWNING_PACKAGE,
    MK_CONFFILE_MANAGEMENT_X_PRIOR_TO_VERSION,
    MK_INSTALLATIONS_INSTALL_EXAMPLES,
    MK_INSTALLATIONS_INSTALL,
    MK_INSTALLATIONS_INSTALL_DOCS,
    MK_INSTALLATIONS_INSTALL_MAN,
    MK_INSTALLATIONS_DISCARD,
    MK_INSTALLATIONS_MULTI_DEST_INSTALL,
)
from debputy.exceptions import DebputyManifestVariableRequiresDebianDirError
from debputy.installations import InstallRule
from debputy.maintscript_snippet import DpkgMaintscriptHelperCommand
from debputy.manifest_conditions import (
    ManifestCondition,
    BinaryPackageContextArchMatchManifestCondition,
    BuildProfileMatch,
    SourceContextArchMatchManifestCondition,
)
from debputy.manifest_parser.base_types import (
    DebputyParsedContent,
    DebputyParsedContentStandardConditional,
    FileSystemMode,
    StaticFileSystemOwner,
    StaticFileSystemGroup,
    SymlinkTarget,
    FileSystemExactMatchRule,
    FileSystemMatchRule,
    SymbolicMode,
    TypeMapping,
    OctalMode,
    FileSystemExactNonDirMatchRule,
)
from debputy.manifest_parser.declarative_parser import DebputyParseHint
from debputy.manifest_parser.exceptions import ManifestParseException
from debputy.manifest_parser.mapper_code import type_mapper_str2package
from debputy.manifest_parser.parser_data import ParserContextData
from debputy.manifest_parser.util import AttributePath
from debputy.packages import BinaryPackage
from debputy.path_matcher import ExactFileSystemPath
from debputy.plugin.api import (
    DebputyPluginInitializer,
    documented_attr,
    reference_documentation,
    VirtualPath,
    packager_provided_file_reference_documentation,
)
from debputy.plugin.api.impl import DebputyPluginInitializerProvider
from debputy.plugin.api.impl_types import automatic_discard_rule_example, PPFFormatParam
from debputy.plugin.api.spec import (
    type_mapping_reference_documentation,
    type_mapping_example,
)
from debputy.plugin.debputy.binary_package_rules import register_binary_package_rules
from debputy.plugin.debputy.discard_rules import (
    _debputy_discard_pyc_files,
    _debputy_prune_la_files,
    _debputy_prune_doxygen_cruft,
    _debputy_prune_binary_debian_dir,
    _debputy_prune_info_dir_file,
    _debputy_prune_backup_files,
    _debputy_prune_vcs_paths,
)
from debputy.plugin.debputy.manifest_root_rules import register_manifest_root_rules
from debputy.plugin.debputy.package_processors import (
    process_manpages,
    apply_compression,
    clean_la_files,
)
from debputy.plugin.debputy.service_management import (
    detect_systemd_service_files,
    generate_snippets_for_systemd_units,
    detect_sysv_init_service_files,
    generate_snippets_for_init_scripts,
)
from debputy.plugin.debputy.shlib_metadata_detectors import detect_shlibdeps
from debputy.plugin.debputy.strip_non_determinism import strip_non_determinism
from debputy.substitution import VariableContext
from debputy.transformation_rules import (
    CreateSymlinkReplacementRule,
    TransformationRule,
    CreateDirectoryTransformationRule,
    RemoveTransformationRule,
    MoveTransformationRule,
    PathMetadataTransformationRule,
    CreateSymlinkPathTransformationRule,
)
from debputy.util import (
    _normalize_path,
    PKGNAME_REGEX,
    PKGVERSION_REGEX,
    debian_policy_normalize_symlink_target,
    active_profiles_match,
    _error,
    _warn,
    _info,
    assume_not_none,
)

_DOCUMENTED_DPKG_ARCH_TYPES = {
    "HOST": (
        "installed on",
        "The package will be **installed** on this type of machine / system",
    ),
    "BUILD": (
        "compiled on",
        "The compilation of this package will be performed **on** this kind of machine / system",
    ),
    "TARGET": (
        "cross-compiler output",
        "When building a cross-compiler, it will produce output for this kind of machine/system",
    ),
}

_DOCUMENTED_DPKG_ARCH_VARS = {
    "ARCH": "Debian's name for the architecture",
    "ARCH_ABI": "Debian's name for the architecture ABI",
    "ARCH_BITS": "Number of bits in the pointer size",
    "ARCH_CPU": "Debian's name for the CPU type",
    "ARCH_ENDIAN": "Endianness of the architecture (little/big)",
    "ARCH_LIBC": "Debian's name for the libc implementation",
    "ARCH_OS": "Debian name for the OS/kernel",
    "GNU_CPU": "GNU's name for the CPU",
    "GNU_SYSTEM": "GNU's name for the system",
    "GNU_TYPE": "GNU system type (GNU_CPU and GNU_SYSTEM combined)",
    "MULTIARCH": "Multi-arch tuple",
}


def _manifest_format_doc(anchor: str) -> str:
    return f"{DEBPUTY_DOC_ROOT_DIR}/MANIFEST-FORMAT.md#{anchor}"


@functools.lru_cache
def load_libcap() -> Tuple[bool, Optional[str], Callable[[str], bool]]:
    cap_library_path = ctypes.util.find_library("cap.so")
    has_libcap = False
    libcap = None
    if cap_library_path:
        try:
            libcap = ctypes.cdll.LoadLibrary(cap_library_path)
            has_libcap = True
        except OSError:
            pass

    if libcap is None:
        warned = False

        def _is_valid_cap(cap: str) -> bool:
            nonlocal warned
            if not warned:
                _info(
                    "Could not load libcap.so; will not validate capabilities. Use `apt install libcap2` to provide"
                    " checking of capabilities."
                )
                warned = True
            return True

    else:
        # cap_t cap_from_text(const char *path_p)
        libcap.cap_from_text.argtypes = [ctypes.c_char_p]
        libcap.cap_from_text.restype = ctypes.c_char_p

        libcap.cap_free.argtypes = [ctypes.c_void_p]
        libcap.cap_free.restype = None

        def _is_valid_cap(cap: str) -> bool:
            cap_t = libcap.cap_from_text(cap.encode("utf-8"))
            ok = cap_t is not None
            libcap.cap_free(cap_t)
            return ok

    return has_libcap, cap_library_path, _is_valid_cap


def check_cap_checker() -> Callable[[str, str], None]:
    _, libcap_path, is_valid_cap = load_libcap()

    seen_cap = set()

    def _check_cap(cap: str, definition_source: str) -> None:
        if cap not in seen_cap and not is_valid_cap(cap):
            seen_cap.add(cap)
            cap_path = f" ({libcap_path})" if libcap_path is not None else ""
            _warn(
                f'The capabilities "{cap}" provided in {definition_source} were not understood by'
                f" libcap.so{cap_path}. Please verify you provided the correct capabilities."
                f" Note: This warning can be a false-positive if you are targeting a newer libcap.so"
                f" than the one installed on this system."
            )

    return _check_cap


def load_source_variables(variable_context: VariableContext) -> Dict[str, str]:
    try:
        changelog = variable_context.debian_dir.lookup("changelog")
        if changelog is None:
            raise DebputyManifestVariableRequiresDebianDirError(
                "The changelog was not present"
            )
        with changelog.open() as fd:
            dch = Changelog(fd, max_blocks=2)
    except FileNotFoundError as e:
        raise DebputyManifestVariableRequiresDebianDirError(
            "The changelog was not present"
        ) from e
    first_entry = dch[0]
    first_non_binnmu_entry = dch[0]
    if first_non_binnmu_entry.other_pairs.get("binary-only", "no") == "yes":
        first_non_binnmu_entry = dch[1]
        assert first_non_binnmu_entry.other_pairs.get("binary-only", "no") == "no"
    source_version = first_entry.version
    epoch = source_version.epoch
    upstream_version = source_version.upstream_version
    debian_revision = source_version.debian_revision
    epoch_upstream = upstream_version
    upstream_debian_revision = upstream_version
    if epoch is not None and epoch != "":
        epoch_upstream = f"{epoch}:{upstream_version}"
    if debian_revision is not None and debian_revision != "":
        upstream_debian_revision = f"{upstream_version}-{debian_revision}"

    package = first_entry.package
    if package is None:
        _error("Cannot determine the source package name from debian/changelog.")

    date = first_entry.date
    if date is not None:
        local_time = datetime.strptime(date, "%a, %d %b %Y %H:%M:%S %z")
        source_date_epoch = str(int(local_time.timestamp()))
    else:
        _warn(
            "The latest changelog entry does not have a (parsable) date, using current time"
            " for SOURCE_DATE_EPOCH"
        )
        source_date_epoch = str(int(time.time()))

    if first_non_binnmu_entry is not first_entry:
        non_binnmu_date = first_non_binnmu_entry.date
        if non_binnmu_date is not None:
            local_time = datetime.strptime(non_binnmu_date, "%a, %d %b %Y %H:%M:%S %z")
            snd_source_date_epoch = str(int(local_time.timestamp()))
        else:
            _warn(
                "The latest (non-binNMU) changelog entry does not have a (parsable) date, using current time"
                " for SOURCE_DATE_EPOCH (for strip-nondeterminism)"
            )
            snd_source_date_epoch = source_date_epoch = str(int(time.time()))
    else:
        snd_source_date_epoch = source_date_epoch
    return {
        "DEB_SOURCE": package,
        "DEB_VERSION": source_version.full_version,
        "DEB_VERSION_EPOCH_UPSTREAM": epoch_upstream,
        "DEB_VERSION_UPSTREAM_REVISION": upstream_debian_revision,
        "DEB_VERSION_UPSTREAM": upstream_version,
        "SOURCE_DATE_EPOCH": source_date_epoch,
        "_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE": str(first_non_binnmu_entry.version),
        "_DEBPUTY_SND_SOURCE_DATE_EPOCH": snd_source_date_epoch,
    }


def initialize_via_private_api(public_api: DebputyPluginInitializer) -> None:
    api = cast("DebputyPluginInitializerProvider", public_api)

    api.metadata_or_maintscript_detector(
        "dpkg-shlibdeps",
        # Private because detect_shlibdeps expects private API (hench this cast)
        cast("MetadataAutoDetector", detect_shlibdeps),
        package_type={"deb", "udeb"},
    )
    register_type_mappings(api)
    register_variables_via_private_api(api)
    document_builtin_variables(api)
    register_automatic_discard_rules(api)
    register_special_ppfs(api)
    register_install_rules(api)
    register_transformation_rules(api)
    register_manifest_condition_rules(api)
    register_dpkg_conffile_rules(api)
    register_processing_steps(api)
    register_service_managers(api)
    register_manifest_root_rules(api)
    register_binary_package_rules(api)


def register_type_mappings(api: DebputyPluginInitializerProvider) -> None:
    api.register_mapped_type(
        TypeMapping(
            FileSystemMatchRule,
            str,
            FileSystemMatchRule.parse_path_match,
        ),
        reference_documentation=type_mapping_reference_documentation(
            description=textwrap.dedent(
                """\
                A generic file system path match with globs.

                Manifest variable substitution will be applied and glob expansion will be performed.

                The match will be read as one of the following cases:

                  - Exact path match if there is no globs characters like `usr/bin/debputy`
                  - A basename glob like `*.txt` or `**/foo`
                  - A generic path glob otherwise like `usr/lib/*.so*`

                Except for basename globs, all matches are always relative to the root directory of
                the match, which is typically the package root directory or a search directory.

                For basename globs, any path matching that basename beneath the package root directory
                or relevant search directories will match.

                Please keep in mind that:

                  * glob patterns often have to be quoted as YAML interpret the glob metacharacter as
                    an anchor reference.

                  * Directories can be matched via this type. Whether the rule using this type
                    recurse into the directory depends on the usage and not this type. Related, if
                    value for this rule ends with a literal "/", then the definition can *only* match
                    directories (similar to the shell).

                  * path matches involving glob expansion are often subject to different rules than
                    path matches without them. As an example, automatic discard rules does not apply
                    to exact path matches, but they will filter out glob matches.
            """,
            ),
            examples=[
                type_mapping_example("usr/bin/debputy"),
                type_mapping_example("*.txt"),
                type_mapping_example("**/foo"),
                type_mapping_example("usr/lib/*.so*"),
                type_mapping_example("usr/share/foo/data-*/"),
            ],
        ),
    )

    api.register_mapped_type(
        TypeMapping(
            FileSystemExactMatchRule,
            str,
            FileSystemExactMatchRule.parse_path_match,
        ),
        reference_documentation=type_mapping_reference_documentation(
            description=textwrap.dedent(
                """\
                A file system match that does **not** expand globs.

                Manifest variable substitution will be applied. However, globs will not be expanded.
                Any glob metacharacters will be interpreted as a literal part of path.

                Note that a directory can be matched via this type. Whether the rule using this type
                recurse into the directory depends on the usage and is not defined by this type.
                Related, if value for this rule ends with a literal "/", then the definition can
                *only* match directories (similar to the shell).
            """,
            ),
            examples=[
                type_mapping_example("usr/bin/dpkg"),
                type_mapping_example("usr/share/foo/"),
                type_mapping_example("usr/share/foo/data.txt"),
            ],
        ),
    )

    api.register_mapped_type(
        TypeMapping(
            FileSystemExactNonDirMatchRule,
            str,
            FileSystemExactNonDirMatchRule.parse_path_match,
        ),
        reference_documentation=type_mapping_reference_documentation(
            description=textwrap.dedent(
                f"""\
                A file system match that does **not** expand globs and must not match a directory.

                Manifest variable substitution will be applied. However, globs will not be expanded.
                Any glob metacharacters will be interpreted as a literal part of path.

                This is like {FileSystemExactMatchRule.__name__} except that the match will fail if the
                provided path matches a directory. Since a directory cannot be matched, it is an error
                for any input to end with a "/" as only directories can be matched if the path ends
                with a "/".
            """,
            ),
            examples=[
                type_mapping_example("usr/bin/dh_debputy"),
                type_mapping_example("usr/share/foo/data.txt"),
            ],
        ),
    )

    api.register_mapped_type(
        TypeMapping(
            SymlinkTarget,
            str,
            lambda v, ap, pc: SymlinkTarget.parse_symlink_target(
                v, ap, assume_not_none(pc).substitution
            ),
        ),
        reference_documentation=type_mapping_reference_documentation(
            description=textwrap.dedent(
                """\
                A symlink target.

                Manifest variable substitution will be applied. This is distinct from an exact file
                system match in that a symlink target is not relative to the package root by default
                (explicitly prefix for "/" for absolute path targets)

                Note that `debputy` will policy normalize symlinks when assembling the deb, so
                use of relative or absolute symlinks comes down to preference.
            """,
            ),
            examples=[
                type_mapping_example("../foo"),
                type_mapping_example("/usr/share/doc/bar"),
            ],
        ),
    )

    api.register_mapped_type(
        TypeMapping(
            StaticFileSystemOwner,
            Union[int, str],
            lambda v, ap, _: StaticFileSystemOwner.from_manifest_value(v, ap),
        ),
        reference_documentation=type_mapping_reference_documentation(
            description=textwrap.dedent(
                """\
            File system owner reference that is part of the passwd base data (such as "root").

            The group can be provided in either of the following three forms:

             * A name (recommended), such as "root"
             * The UID in the form of an integer (that is, no quoting), such as 0 (for "root")
             * The name and the UID separated by colon such as "root:0" (for "root").

            Note in the last case, the `debputy` will validate that the name and the UID match.

            Some owners (such as "nobody") are deliberately disallowed.
            """
            ),
            examples=[
                type_mapping_example("root"),
                type_mapping_example(0),
                type_mapping_example("root:0"),
                type_mapping_example("bin"),
            ],
        ),
    )
    api.register_mapped_type(
        TypeMapping(
            StaticFileSystemGroup,
            Union[int, str],
            lambda v, ap, _: StaticFileSystemGroup.from_manifest_value(v, ap),
        ),
        reference_documentation=type_mapping_reference_documentation(
            description=textwrap.dedent(
                """\
            File system group reference that is part of the passwd base data (such as "root").

            The group can be provided in either of the following three forms:

             * A name (recommended), such as "root"
             * The GID in the form of an integer (that is, no quoting), such as 0 (for "root")
             * The name and the GID separated by colon such as "root:0" (for "root").

            Note in the last case, the `debputy` will validate that the name and the GID match.

            Some owners (such as "nobody") are deliberately disallowed.
            """
            ),
            examples=[
                type_mapping_example("root"),
                type_mapping_example(0),
                type_mapping_example("root:0"),
                type_mapping_example("tty"),
            ],
        ),
    )

    api.register_mapped_type(
        TypeMapping(
            BinaryPackage,
            str,
            type_mapper_str2package,
        ),
        reference_documentation=type_mapping_reference_documentation(
            description="Name of a package in debian/control",
        ),
    )

    api.register_mapped_type(
        TypeMapping(
            FileSystemMode,
            str,
            lambda v, ap, _: FileSystemMode.parse_filesystem_mode(v, ap),
        ),
        reference_documentation=type_mapping_reference_documentation(
            description="Either an octal mode or symbolic mode",
            examples=[
                type_mapping_example("a+x"),
                type_mapping_example("u=rwX,go=rX"),
                type_mapping_example("0755"),
            ],
        ),
    )
    api.register_mapped_type(
        TypeMapping(
            OctalMode,
            str,
            lambda v, ap, _: OctalMode.parse_filesystem_mode(v, ap),
        ),
        reference_documentation=type_mapping_reference_documentation(
            description="An octal mode. Must always be a string.",
            examples=[
                type_mapping_example("0644"),
                type_mapping_example("0755"),
            ],
        ),
    )


def register_service_managers(
    api: DebputyPluginInitializerProvider,
) -> None:
    api.service_provider(
        "systemd",
        detect_systemd_service_files,
        generate_snippets_for_systemd_units,
    )
    api.service_provider(
        "sysvinit",
        detect_sysv_init_service_files,
        generate_snippets_for_init_scripts,
    )


def register_automatic_discard_rules(
    api: DebputyPluginInitializerProvider,
) -> None:
    api.automatic_discard_rule(
        "python-cache-files",
        _debputy_discard_pyc_files,
        rule_reference_documentation="Discards any *.pyc, *.pyo files and any __pycache__ directories",
        examples=automatic_discard_rule_example(
            (".../foo.py", False),
            ".../__pycache__/",
            ".../__pycache__/...",
            ".../foo.pyc",
            ".../foo.pyo",
        ),
    )
    api.automatic_discard_rule(
        "la-files",
        _debputy_prune_la_files,
        rule_reference_documentation="Discards any file with the extension .la beneath the directory /usr/lib",
        examples=automatic_discard_rule_example(
            "usr/lib/libfoo.la",
            ("usr/lib/libfoo.so.1.0.0", False),
        ),
    )
    api.automatic_discard_rule(
        "backup-files",
        _debputy_prune_backup_files,
        rule_reference_documentation="Discards common back up files such as foo~, foo.bak or foo.orig",
        examples=(
            automatic_discard_rule_example(
                ".../foo~",
                ".../foo.orig",
                ".../foo.rej",
                ".../DEADJOE",
                ".../.foo.sw.",
            ),
        ),
    )
    api.automatic_discard_rule(
        "version-control-paths",
        _debputy_prune_vcs_paths,
        rule_reference_documentation="Discards common version control paths such as .git, .gitignore, CVS, etc.",
        examples=automatic_discard_rule_example(
            ("tools/foo", False),
            ".../CVS/",
            ".../CVS/...",
            ".../.gitignore",
            ".../.gitattributes",
            ".../.git/",
            ".../.git/...",
        ),
    )
    api.automatic_discard_rule(
        "gnu-info-dir-file",
        _debputy_prune_info_dir_file,
        rule_reference_documentation="Discards the /usr/share/info/dir file (causes package file conflicts)",
        examples=automatic_discard_rule_example(
            "usr/share/info/dir",
            ("usr/share/info/foo.info", False),
            ("usr/share/info/dir.info", False),
            ("usr/share/random/case/dir", False),
        ),
    )
    api.automatic_discard_rule(
        "debian-dir",
        _debputy_prune_binary_debian_dir,
        rule_reference_documentation="(Implementation detail) Discards any DEBIAN directory to avoid it from appearing"
        " literally in the file listing",
        examples=(
            automatic_discard_rule_example(
                "DEBIAN/",
                "DEBIAN/control",
                ("usr/bin/foo", False),
                ("usr/share/DEBIAN/foo", False),
            ),
        ),
    )
    api.automatic_discard_rule(
        "doxygen-cruft-files",
        _debputy_prune_doxygen_cruft,
        rule_reference_documentation="Discards cruft files generated by doxygen",
        examples=automatic_discard_rule_example(
            ("usr/share/doc/foo/api/doxygen.css", False),
            ("usr/share/doc/foo/api/doxygen.svg", False),
            ("usr/share/doc/foo/api/index.html", False),
            "usr/share/doc/foo/api/.../cruft.map",
            "usr/share/doc/foo/api/.../cruft.md5",
        ),
    )


def register_processing_steps(api: DebputyPluginInitializerProvider) -> None:
    api.package_processor("manpages", process_manpages)
    api.package_processor("clean-la-files", clean_la_files)
    # strip-non-determinism makes assumptions about the PackageProcessingContext implementation
    api.package_processor(
        "strip-nondeterminism",
        cast("Any", strip_non_determinism),
        depends_on_processor=["manpages"],
    )
    api.package_processor(
        "compression",
        apply_compression,
        depends_on_processor=["manpages", "strip-nondeterminism"],
    )


def register_variables_via_private_api(api: DebputyPluginInitializerProvider) -> None:
    api.manifest_variable_provider(
        load_source_variables,
        {
            "DEB_SOURCE": "Name of the source package (`dpkg-parsechangelog -SSource`)",
            "DEB_VERSION": "Version from the top most changelog entry (`dpkg-parsechangelog -SVersion`)",
            "DEB_VERSION_EPOCH_UPSTREAM": "Version from the top most changelog entry *without* the Debian revision",
            "DEB_VERSION_UPSTREAM_REVISION": "Version from the top most changelog entry *without* the epoch",
            "DEB_VERSION_UPSTREAM": "Upstream version from the top most changelog entry (that is, *without* epoch and Debian revision)",
            "SOURCE_DATE_EPOCH": textwrap.dedent(
                """\
            Timestamp from the top most changelog entry (`dpkg-parsechangelog -STimestamp`)
            Please see https://reproducible-builds.org/docs/source-date-epoch/ for the full definition of
            this variable.
            """
            ),
            "_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE": None,
            "_DEBPUTY_SND_SOURCE_DATE_EPOCH": None,
        },
    )


def document_builtin_variables(api: DebputyPluginInitializerProvider) -> None:
    api.document_builtin_variable(
        "PACKAGE",
        "Name of the binary package (only available in binary context)",
        is_context_specific=True,
    )

    arch_types = _DOCUMENTED_DPKG_ARCH_TYPES

    for arch_type, (arch_type_tag, arch_type_doc) in arch_types.items():
        for arch_var, arch_var_doc in _DOCUMENTED_DPKG_ARCH_VARS.items():
            full_var = f"DEB_{arch_type}_{arch_var}"
            documentation = textwrap.dedent(
                f"""\
            {arch_var_doc} ({arch_type_tag})
            This variable describes machine information used when the package is compiled and assembled.
             * Machine type: {arch_type_doc}
             * Value description: {arch_var_doc}

            The value is the output of: `dpkg-architecture -q{full_var}`
            """
            )
            api.document_builtin_variable(
                full_var,
                documentation,
                is_for_special_case=arch_type != "HOST",
            )


def _format_docbase_filename(
    path_format: str,
    format_param: PPFFormatParam,
    docbase_file: VirtualPath,
) -> str:
    with docbase_file.open() as fd:
        content = Deb822(fd)
        proper_name = content["Document"]
        if proper_name is not None:
            format_param["name"] = proper_name
        else:
            _warn(
                f"The docbase file {docbase_file.fs_path} is missing the Document field"
            )
    return path_format.format(**format_param)


def register_special_ppfs(api: DebputyPluginInitializerProvider) -> None:
    api.packager_provided_file(
        "doc-base",
        "/usr/share/doc-base/{owning_package}.{name}",
        format_callback=_format_docbase_filename,
    )

    api.packager_provided_file(
        "shlibs",
        "DEBIAN/shlibs",
        allow_name_segment=False,
        reservation_only=True,
        reference_documentation=packager_provided_file_reference_documentation(
            format_documentation_uris=["man:deb-shlibs(5)"],
        ),
    )
    api.packager_provided_file(
        "symbols",
        "DEBIAN/symbols",
        allow_name_segment=False,
        allow_architecture_segment=True,
        reservation_only=True,
        reference_documentation=packager_provided_file_reference_documentation(
            format_documentation_uris=["man:deb-symbols(5)"],
        ),
    )
    api.packager_provided_file(
        "templates",
        "DEBIAN/templates",
        allow_name_segment=False,
        allow_architecture_segment=False,
        reservation_only=True,
    )
    api.packager_provided_file(
        "alternatives",
        "DEBIAN/alternatives",
        allow_name_segment=False,
        allow_architecture_segment=True,
        reservation_only=True,
    )


def register_install_rules(api: DebputyPluginInitializerProvider) -> None:
    api.pluggable_manifest_rule(
        InstallRule,
        MK_INSTALLATIONS_INSTALL,
        ParsedInstallRule,
        _install_rule_handler,
        source_format=_with_alt_form(ParsedInstallRuleSourceFormat),
        inline_reference_documentation=reference_documentation(
            title="Generic install (`install`)",
            description=textwrap.dedent(
                """\
                The generic `install` rule can be used to install arbitrary paths into packages
                and is *similar* to how `dh_install` from debhelper works.  It is a two "primary" uses.

                  1) The classic "install into directory" similar to the standard `dh_install`
                  2) The "install as" similar to `dh-exec`'s `foo => bar` feature.

                The `install` rule installs a path exactly once into each package it acts on. In
                the rare case that you want to install the same source *multiple* times into the
                *same* packages, please have a look at `{MULTI_DEST_INSTALL}`.
            """.format(
                    MULTI_DEST_INSTALL=MK_INSTALLATIONS_MULTI_DEST_INSTALL
                )
            ),
            non_mapping_description=textwrap.dedent(
                """\
                When the input is a string or a list of string, then that value is used as shorthand
                for `source` or `sources` (respectively).  This form can only be used when `into` is
                not required.
            """
            ),
            attributes=[
                documented_attr(
                    ["source", "sources"],
                    textwrap.dedent(
                        """\
                        A path match (`source`) or a list of path matches (`sources`) defining the
                        source path(s) to be installed. The path match(es) can use globs.  Each match
                        is tried against default search directories.
                         - When a symlink is matched, then the symlink (not its target) is installed
                           as-is.  When a directory is matched, then the directory is installed along
                           with all the contents that have not already been installed somewhere.
                """
                    ),
                ),
                documented_attr(
                    "dest_dir",
                    textwrap.dedent(
                        """\
                        A path defining the destination *directory*.  The value *cannot* use globs, but can
                        use substitution.  If neither `as` nor `dest-dir` is given, then `dest-dir` defaults
                        to the directory name of the `source`.
                """
                    ),
                ),
                documented_attr(
                    "into",
                    textwrap.dedent(
                        """\
                    Either a package name or a list of package names for which these paths should be
                    installed.  This key is conditional on whether there are multiple binary packages listed
                    in `debian/control`.  When there is only one binary package, then that binary is the
                    default for `into`. Otherwise, the key is required.
                    """
                    ),
                ),
                documented_attr(
                    "install_as",
                    textwrap.dedent(
                        """\
                                A path defining the path to install the source as. This is a full path.  This option
                                is mutually exclusive with `dest-dir` and `sources` (but not `source`).  When `as` is
                                given, then `source` must match exactly one "not yet matched" path.
                            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                    A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
                """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc("generic-install-install"),
        ),
    )
    api.pluggable_manifest_rule(
        InstallRule,
        [
            MK_INSTALLATIONS_INSTALL_DOCS,
            "install-doc",
        ],
        ParsedInstallRule,
        _install_docs_rule_handler,
        source_format=_with_alt_form(ParsedInstallDocRuleSourceFormat),
        inline_reference_documentation=reference_documentation(
            title="Install documentation (`install-docs`)",
            description=textwrap.dedent(
                """\
            This install rule resemble that of `dh_installdocs`.  It is a shorthand over the generic
            `install` rule with the following key features:

             1) The default `dest-dir` is to use the package's documentation directory (usually something
                like `/usr/share/doc/{{PACKAGE}}`, though it respects the "main documentation package"
                recommendation from Debian Policy). The `dest-dir` or `as` can be set in case the
                documentation in question goes into another directory or with a concrete path.  In this
                case, it is still "better" than `install` due to the remaining benefits.
             2) The rule comes with pre-defined conditional logic for skipping the rule under
                `DEB_BUILD_OPTIONS=nodoc`, so you do not have to write that conditional yourself.
             3) The `into` parameter can be omitted as long as there is a exactly one non-`udeb`
                package listed in `debian/control`.

            With these two things in mind, it behaves just like the `install` rule.

            Note: It is often worth considering to use a more specialized version of the `install-docs`
            rule when one such is available. If you are looking to install an example or a man page,
            consider whether `install-examples` or `install-man` might be a better fit for your
            use-case.
        """
            ),
            non_mapping_description=textwrap.dedent(
                """\
            When the input is a string or a list of string, then that value is used as shorthand
            for `source` or `sources` (respectively).  This form can only be used when `into` is
            not required.
        """
            ),
            attributes=[
                documented_attr(
                    ["source", "sources"],
                    textwrap.dedent(
                        """\
                    A path match (`source`) or a list of path matches (`sources`) defining the
                    source path(s) to be installed. The path match(es) can use globs.  Each match
                    is tried against default search directories.
                     - When a symlink is matched, then the symlink (not its target) is installed
                       as-is.  When a directory is matched, then the directory is installed along
                       with all the contents that have not already been installed somewhere.

                     - **CAVEAT**: Specifying `source: examples` where `examples` resolves to a
                       directory for `install-examples` will give you an `examples/examples`
                       directory in the package, which is rarely what you want. Often, you
                       can solve this by using `examples/*` instead. Similar for `install-docs`
                       and a `doc` or `docs` directory.
            """
                    ),
                ),
                documented_attr(
                    "dest_dir",
                    textwrap.dedent(
                        """\
                        A path defining the destination *directory*.  The value *cannot* use globs, but can
                        use substitution.  If neither `as` nor `dest-dir` is given, then `dest-dir` defaults
                        to the relevant package documentation directory (a la `/usr/share/doc/{{PACKAGE}}`).
                """
                    ),
                ),
                documented_attr(
                    "into",
                    textwrap.dedent(
                        """\
                    Either a package name or a list of package names for which these paths should be
                    installed as documentation.  This key is conditional on whether there are multiple
                    (non-`udeb`) binary packages listed in `debian/control`.  When there is only one
                    (non-`udeb`) binary package, then that binary is the default for `into`. Otherwise,
                    the key is required.
                """
                    ),
                ),
                documented_attr(
                    "install_as",
                    textwrap.dedent(
                        """\
                                A path defining the path to install the source as. This is a full path.  This option
                                is mutually exclusive with `dest-dir` and `sources` (but not `source`).  When `as` is
                                given, then `source` must match exactly one "not yet matched" path.
                            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
                This condition will be combined with the built-in condition provided by these rules
                (rather than replacing it).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "install-documentation-install-docs"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        InstallRule,
        [
            MK_INSTALLATIONS_INSTALL_EXAMPLES,
            "install-example",
        ],
        ParsedInstallExamplesRule,
        _install_examples_rule_handler,
        source_format=_with_alt_form(ParsedInstallExamplesRuleSourceFormat),
        inline_reference_documentation=reference_documentation(
            title="Install examples (`install-examples`)",
            description=textwrap.dedent(
                """\
            This install rule resemble that of `dh_installexamples`.  It is a shorthand over the generic `
            install` rule with the following key features:

             1) It pre-defines the `dest-dir` that respects the "main documentation package" recommendation from
                Debian Policy. The `install-examples` will use the `examples` subdir for the package documentation
                dir.
             2) The rule comes with pre-defined conditional logic for skipping the rule under
                `DEB_BUILD_OPTIONS=nodoc`, so you do not have to write that conditional yourself.
             3) The `into` parameter can be omitted as long as there is a exactly one non-`udeb`
                package listed in `debian/control`.

            With these two things in mind, it behaves just like the `install` rule.
        """
            ),
            non_mapping_description=textwrap.dedent(
                """\
            When the input is a string or a list of string, then that value is used as shorthand
            for `source` or `sources` (respectively).  This form can only be used when `into` is
            not required.
        """
            ),
            attributes=[
                documented_attr(
                    ["source", "sources"],
                    textwrap.dedent(
                        """\
                    A path match (`source`) or a list of path matches (`sources`) defining the
                    source path(s) to be installed. The path match(es) can use globs.  Each match
                    is tried against default search directories.
                     - When a symlink is matched, then the symlink (not its target) is installed
                       as-is.  When a directory is matched, then the directory is installed along
                       with all the contents that have not already been installed somewhere.

                     - **CAVEAT**: Specifying `source: examples` where `examples` resolves to a
                       directory for `install-examples` will give you an `examples/examples`
                       directory in the package, which is rarely what you want. Often, you
                       can solve this by using `examples/*` instead. Similar for `install-docs`
                       and a `doc` or `docs` directory.
            """
                    ),
                ),
                documented_attr(
                    "into",
                    textwrap.dedent(
                        """\
                    Either a package name or a list of package names for which these paths should be
                    installed as examples.  This key is conditional on whether there are (non-`udeb`)
                    multiple binary packages listed in `debian/control`.  When there is only one
                    (non-`udeb`) binary package, then that binary is the default for `into`.
                    Otherwise, the key is required.
                """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
                This condition will be combined with the built-in condition provided by these rules
                (rather than replacing it).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "install-examples-install-examples"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        InstallRule,
        MK_INSTALLATIONS_INSTALL_MAN,
        ParsedInstallManpageRule,
        _install_man_rule_handler,
        source_format=_with_alt_form(ParsedInstallManpageRuleSourceFormat),
        inline_reference_documentation=reference_documentation(
            title="Install man pages (`install-man`)",
            description=textwrap.dedent(
                """\
                Install rule for installing man pages similar to `dh_installman`. It is a shorthand
                over the generic `install` rule with the following key features:

                 1) The rule can only match files (notably, symlinks cannot be matched by this rule).
                 2) The `dest-dir` is computed per source file based on the man page's section and
                    language.
                 3) The `into` parameter can be omitted as long as there is a exactly one non-`udeb`
                    package listed in `debian/control`.
                 4) The rule comes with man page specific attributes such as `language` and `section`
                    for when the auto-detection is insufficient.
                 5) The rule comes with pre-defined conditional logic for skipping the rule under
                    `DEB_BUILD_OPTIONS=nodoc`, so you do not have to write that conditional yourself.

                With these things in mind, the rule behaves similar to the `install` rule.
            """
            ),
            non_mapping_description=textwrap.dedent(
                """\
                When the input is a string or a list of string, then that value is used as shorthand
                for `source` or `sources` (respectively).  This form can only be used when `into` is
                not required.
            """
            ),
            attributes=[
                documented_attr(
                    ["source", "sources"],
                    textwrap.dedent(
                        """\
                        A path match (`source`) or a list of path matches (`sources`) defining the
                        source path(s) to be installed. The path match(es) can use globs.  Each match
                        is tried against default search directories.
                         - When a symlink is matched, then the symlink (not its target) is installed
                           as-is.  When a directory is matched, then the directory is installed along
                           with all the contents that have not already been installed somewhere.
                """
                    ),
                ),
                documented_attr(
                    "into",
                    textwrap.dedent(
                        """\
                    Either a package name or a list of package names for which these paths should be
                    installed as man pages.  This key is conditional on whether there are multiple (non-`udeb`)
                    binary packages listed in `debian/control`.  When there is only one (non-`udeb`) binary
                    package, then that binary is the default for `into`. Otherwise, the key is required.
                    """
                    ),
                ),
                documented_attr(
                    "section",
                    textwrap.dedent(
                        """\
                        If provided, it must be an integer between 1 and 9 (both inclusive), defining the
                        section the man pages belong overriding any auto-detection that `debputy` would
                        have performed.
                """
                    ),
                ),
                documented_attr(
                    "language",
                    textwrap.dedent(
                        """\
                        If provided, it must be either a 2 letter language code (such as `de`), a 5 letter
                        language + dialect code (such as `pt_BR`), or one of the special keywords `C`,
                        `derive-from-path`, or `derive-from-basename`.  The default is `derive-from-path`.
                           - When `language` is `C`, then the man pages are assumed to be "untranslated".
                           - When `language` is a language code (with or without dialect), then all man pages
                             matched will be assumed to be translated to that concrete language / dialect.
                           - When `language` is `derive-from-path`, then `debputy` attempts to derive the
                             language from the path (`man/<language>/man<section>`).  This matches the
                             default of `dh_installman`. When no language can be found for a given source,
                             `debputy` behaves like language was `C`.
                           - When `language` is `derive-from-basename`, then `debputy` attempts to derive
                             the language from the basename (`foo.<language>.1`) similar to `dh_installman`
                             previous default.  When no language can be found for a given source, `debputy`
                             behaves like language was `C`.  Note this is prone to false positives where
                             `.pl`, `.so` or similar two-letter extensions gets mistaken for a language code
                             (`.pl` can both be "Polish" or "Perl Script", `.so` can both be "Somali" and
                             "Shared Object" documentation).  In this configuration, such extensions are
                             always assumed to be a language.
                            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                    A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
                """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "install-manpages-install-man"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        InstallRule,
        MK_INSTALLATIONS_DISCARD,
        ParsedInstallDiscardRule,
        _install_discard_rule_handler,
        source_format=_with_alt_form(ParsedInstallDiscardRuleSourceFormat),
        inline_reference_documentation=reference_documentation(
            title="Discard (or exclude) upstream provided paths (`discard`)",
            description=textwrap.dedent(
                """\
                    When installing paths from `debian/tmp` into packages, it might be useful to ignore
                    some paths that you never need installed.  This can be done with the `discard` rule.

                    Once a path is discarded, it cannot be matched by any other install rules.  A path
                    that is discarded, is considered handled when `debputy` checks for paths you might
                    have forgotten to install.  The `discard` feature is therefore *also* replaces the
                    `debian/not-installed` file used by `debhelper` and `cdbs`.
        """
            ),
            non_mapping_description=textwrap.dedent(
                """\
            When the input is a string or a list of string, then that value is used as shorthand
            for `path` or `paths` (respectively).
        """
            ),
            attributes=[
                documented_attr(
                    ["path", "paths"],
                    textwrap.dedent(
                        """\
                    A path match (`path`) or a list of path matches (`paths`) defining the source
                    path(s) that should not be installed anywhere. The path match(es) can use globs.
                    - When a symlink is matched, then the symlink (not its target) is discarded as-is.
                      When a directory is matched, then the directory is discarded along with all the
                      contents that have not already been installed somewhere.
            """
                    ),
                ),
                documented_attr(
                    ["search_dir", "search_dirs"],
                    textwrap.dedent(
                        """\
                         A path (`search-dir`) or a list to paths (`search-dirs`) that defines
                         which search directories apply to. This attribute is primarily useful
                         for source packages that uses "per package search dirs", and you want
                         to restrict a discard rule to a subset of the relevant search dirs.
                         Note all listed search directories must be either an explicit search
                         requested by the packager or a search directory that `debputy`
                         provided automatically (such as `debian/tmp`). Listing other paths
                         will make `debputy` report an error.
                         - Note that the `path` or `paths` must match at least one entry in
                           any of the search directories unless *none* of the search directories
                           exist (or the condition in `required-when` evaluates to false). When
                           none of the search directories exist, the discard rule is silently
                           skipped. This special-case enables you to have discard rules only
                           applicable to certain builds that are only performed conditionally.
            """
                    ),
                ),
                documented_attr(
                    "required_when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules](#conditional-rules). The discard
                rule is always applied. When the conditional is present and evaluates to false,
                the discard rule can silently match nothing.When the condition is absent, *or*
                it evaluates to true, then each pattern provided must match at least one path.
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "discard-or-exclude-upstream-provided-paths-discard"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        InstallRule,
        MK_INSTALLATIONS_MULTI_DEST_INSTALL,
        ParsedMultiDestInstallRule,
        _multi_dest_install_rule_handler,
        source_format=ParsedMultiDestInstallRuleSourceFormat,
        inline_reference_documentation=reference_documentation(
            title=f"Multi destination install (`{MK_INSTALLATIONS_MULTI_DEST_INSTALL}`)",
            description=textwrap.dedent(
                """\
                The `{RULE_NAME}` is a variant of the generic `install` rule that installs sources
                into multiple destination paths. This is needed for the rare case where you want a
                path to be installed *twice* (or more) into the *same* package. The rule is a two
                "primary" uses.

                  1) The classic "install into directory" similar to the standard `dh_install`,
                     except you list 2+ destination directories.
                  2) The "install as" similar to `dh-exec`'s `foo => bar` feature, except you list
                     2+ `as` names.
            """.format(
                    RULE_NAME=MK_INSTALLATIONS_MULTI_DEST_INSTALL
                )
            ),
            attributes=[
                documented_attr(
                    ["source", "sources"],
                    textwrap.dedent(
                        """\
                        A path match (`source`) or a list of path matches (`sources`) defining the
                        source path(s) to be installed. The path match(es) can use globs.  Each match
                        is tried against default search directories.
                         - When a symlink is matched, then the symlink (not its target) is installed
                           as-is.  When a directory is matched, then the directory is installed along
                           with all the contents that have not already been installed somewhere.
                """
                    ),
                ),
                documented_attr(
                    "dest_dirs",
                    textwrap.dedent(
                        """\
                        A list of paths defining the destination *directories*.  The value *cannot* use
                        globs, but can use substitution. It is mutually exclusive with `as` but must be
                        provided if `as` is not provided. The attribute must contain at least two paths
                        (if you do not have two paths, you want `install`).
                """
                    ),
                ),
                documented_attr(
                    "into",
                    textwrap.dedent(
                        """\
                    Either a package name or a list of package names for which these paths should be
                    installed.  This key is conditional on whether there are multiple binary packages listed
                    in `debian/control`.  When there is only one binary package, then that binary is the
                    default for `into`. Otherwise, the key is required.
                    """
                    ),
                ),
                documented_attr(
                    "install_as",
                    textwrap.dedent(
                        """\
                                A list of paths, which defines all the places the source will be installed.
                                Each path must be a full path without globs (but can use substitution).
                                This option is mutually exclusive with `dest-dirs` and `sources` (but not
                                `source`).  When `as` is given, then `source` must match exactly one
                                "not yet matched" path. The attribute must contain at least two paths
                                (if you do not have two paths, you want `install`).
                            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                    A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
                """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc("generic-install-install"),
        ),
    )


def register_transformation_rules(api: DebputyPluginInitializerProvider) -> None:
    api.pluggable_manifest_rule(
        TransformationRule,
        "move",
        TransformationMoveRuleSpec,
        _transformation_move_handler,
        inline_reference_documentation=reference_documentation(
            title="Move transformation rule (`move`)",
            description=textwrap.dedent(
                """\
                The move transformation rule is mostly only useful for single binary source packages,
                where everything from upstream's build system is installed automatically into the package.
                In those case, you might find yourself with some files that need to be renamed to match
                Debian specific requirements.

                This can be done with the `move` transformation rule, which is a rough emulation of the
                `mv` command line tool.
        """
            ),
            attributes=[
                documented_attr(
                    "source",
                    textwrap.dedent(
                        """\
                        A path match defining the source path(s) to be renamed.  The value can use globs
                        and substitutions.
            """
                    ),
                ),
                documented_attr(
                    "target",
                    textwrap.dedent(
                        """\
                        A path defining the target path.  The value *cannot* use globs, but can use
                        substitution. If the target ends with a literal `/` (prior to substitution),
                        the target will *always* be a directory.
            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "move-transformation-rule-move"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        TransformationRule,
        "remove",
        TransformationRemoveRuleSpec,
        _transformation_remove_handler,
        source_format=_with_alt_form(TransformationRemoveRuleInputFormat),
        inline_reference_documentation=reference_documentation(
            title="Remove transformation rule (`remove`)",
            description=textwrap.dedent(
                """\
                The remove transformation rule is mostly only useful for single binary source packages,
                where everything from upstream's build system is installed automatically into the package.
                In those case, you might find yourself with some files that are _not_ relevant for the
                Debian package (but would be relevant for other distros or for non-distro local builds).
                Common examples include `INSTALL` files or `LICENSE` files (when they are just a subset
                of `debian/copyright`).

                In the manifest, you can ask `debputy` to remove paths from the debian package by using
                the `remove` transformation rule.

                Note that `remove` removes paths from future glob matches and transformation rules.
        """
            ),
            non_mapping_description=textwrap.dedent(
                """\
            When the input is a string or a list of string, then that value is used as shorthand
            for `path` or `paths` (respectively).
        """
            ),
            attributes=[
                documented_attr(
                    ["path", "paths"],
                    textwrap.dedent(
                        """\
                        A path match (`path`) or a list of path matches (`paths`) defining the
                        path(s) inside the package that should be removed. The path match(es)
                        can use globs.
                        - When a symlink is matched, then the symlink (not its target) is removed
                          as-is.  When a directory is matched, then the directory is removed
                          along with all the contents.
            """
                    ),
                ),
                documented_attr(
                    "keep_empty_parent_dirs",
                    textwrap.dedent(
                        """\
                        A boolean determining whether to prune parent directories that become
                        empty as a consequence of this rule.  When provided and `true`, this
                        rule will leave empty directories behind. Otherwise, if this rule
                        causes a directory to become empty that directory will be removed.
            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
                This condition will be combined with the built-in condition provided by these rules
                (rather than replacing it).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "remove-transformation-rule-remove"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        TransformationRule,
        "create-symlink",
        CreateSymlinkRule,
        _transformation_create_symlink,
        inline_reference_documentation=reference_documentation(
            title="Create symlinks transformation rule (`create-symlink`)",
            description=textwrap.dedent(
                """\
                Often, the upstream build system will provide the symlinks for you.  However,
                in some cases, it is useful for the packager to define distribution specific
                symlinks. This can be done via the `create-symlink` transformation rule.
        """
            ),
            attributes=[
                documented_attr(
                    "path",
                    textwrap.dedent(
                        """\
                         The path that should be a symlink.  The path may contain substitution
                         variables such as `{{DEB_HOST_MULTIARCH}}` but _cannot_ use globs.
                         Parent directories are implicitly created as necessary.
                         * Note that if `path` already exists, the behaviour of this
                           transformation depends on the value of `replacement-rule`.
            """
                    ),
                ),
                documented_attr(
                    "target",
                    textwrap.dedent(
                        """\
                        Where the symlink should point to. The target may contain substitution
                        variables such as `{{DEB_HOST_MULTIARCH}}` but _cannot_ use globs.
                        The link target is _not_ required to exist inside the package.
                        * The `debputy` tool will normalize the target according to the rules
                          of the Debian Policy.  Use absolute or relative target at your own
                          preference.
            """
                    ),
                ),
                documented_attr(
                    "replacement_rule",
                    textwrap.dedent(
                        """\
                        This attribute defines how to handle if `path` already exists. It can
                        be set to one of the following values:
                           - `error-if-exists`: When `path` already exists, `debputy` will
                              stop with an error.  This is similar to `ln -s` semantics.
                           - `error-if-directory`: When `path` already exists, **and** it is
                              a directory, `debputy` will stop with an error. Otherwise,
                              remove the `path` first and then create the symlink.  This is
                              similar to `ln -sf` semantics.
                           - `abort-on-non-empty-directory` (default): When `path` already
                              exists, then it will be removed provided it is a non-directory
                              **or** an *empty* directory and the symlink will then be
                              created.  If the path is a *non-empty* directory, `debputy`
                              will stop with an error.
                           - `discard-existing`: When `path` already exists, it will be
                              removed. If the `path` is a directory, all its contents will
                              be removed recursively along with the directory. Finally,
                              the symlink is created. This is similar to having an explicit
                              `remove` rule just prior to the `create-symlink` that is
                              conditional on `path` existing (plus the condition defined in
                              `when` if any).

                       Keep in mind, that `replacement-rule` only applies if `path` exists.
                       If the symlink cannot be created, because a part of `path` exist and
                       is *not* a directory, then `create-symlink` will fail regardless of
                       the value in `replacement-rule`.
            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "create-symlinks-transformation-rule-create-symlink"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        TransformationRule,
        "path-metadata",
        PathManifestRule,
        _transformation_path_metadata,
        source_format=PathManifestSourceDictFormat,
        inline_reference_documentation=reference_documentation(
            title="Change path owner/group or mode (`path-metadata`)",
            description=textwrap.dedent(
                """\
                The `debputy` command normalizes the path metadata (such as ownership and mode) similar
                to `dh_fixperms`.  For most packages, the default is what you want.  However, in some
                cases, the package has a special case or two that `debputy` does not cover.  In that
                case, you can tell `debputy` to use the metadata you want by using the `path-metadata`
                transformation.

                Common use-cases include setuid/setgid binaries (such `usr/bin/sudo`) or/and static
                ownership (such as /usr/bin/write).
        """
            ),
            attributes=[
                documented_attr(
                    ["path", "paths"],
                    textwrap.dedent(
                        """\
                         A path match (`path`) or a list of path matches (`paths`) defining the path(s)
                         inside the package that should be affected. The path match(es) can use globs
                         and substitution variables. Special-rules for matches:
                         - Symlinks are never followed and will never be matched by this rule.
                         - Directory handling depends on the `recursive` attribute.
            """
                    ),
                ),
                documented_attr(
                    "owner",
                    textwrap.dedent(
                        """\
                         Denotes the owner of the paths matched by `path` or `paths`. When omitted,
                         no change of owner is done.
            """
                    ),
                ),
                documented_attr(
                    "group",
                    textwrap.dedent(
                        """\
                         Denotes the group of the paths matched by `path` or `paths`. When omitted,
                         no change of group is done.
            """
                    ),
                ),
                documented_attr(
                    "mode",
                    textwrap.dedent(
                        """\
                         Denotes the mode of the paths matched by `path` or `paths`. When omitted,
                         no change in mode is done. Note that numeric mode must always be given as
                         a string (i.e., with quotes).  Symbolic mode can be used as well. If
                         symbolic mode uses a relative definition (e.g., `o-rx`), then it is
                         relative to the matched path's current mode.
            """
                    ),
                ),
                documented_attr(
                    "capabilities",
                    textwrap.dedent(
                        """\
                         Denotes a Linux capability that should be applied to the path. When provided,
                         `debputy` will cause the capability to be applied to all *files* denoted by
                         the `path`/`paths` attribute on install (via `postinst configure`) provided
                         that `setcap` is installed on the system when the `postinst configure` is
                         run.
                         - If any non-file paths are matched, the `capabilities` will *not* be applied
                           to those paths.

            """
                    ),
                ),
                documented_attr(
                    "capability_mode",
                    textwrap.dedent(
                        """\
                        Denotes the mode to apply to the path *if* the Linux capability denoted in
                       `capabilities` was successfully applied. If omitted, it defaults to `a-s` as
                       generally capabilities are used to avoid "setuid"/"setgid" binaries. The
                       `capability-mode` is relative to the *final* path mode (the mode of the path
                       in the produced `.deb`). The `capability-mode` attribute cannot be used if
                       `capabilities` is omitted.
            """
                    ),
                ),
                documented_attr(
                    "recursive",
                    textwrap.dedent(
                        """\
                        When a directory is matched, then the metadata changes are applied to the
                        directory itself. When `recursive` is `true`, then the transformation is
                        *also* applied to all paths beneath the directory. The default value for
                        this attribute is `false`.
            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "change-path-ownergroup-or-mode-path-metadata"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        TransformationRule,
        "create-directories",
        EnsureDirectoryRule,
        _transformation_mkdirs,
        source_format=_with_alt_form(EnsureDirectorySourceFormat),
        inline_reference_documentation=reference_documentation(
            title="Create directories transformation rule (`create-directories`)",
            description=textwrap.dedent(
                """\
                NOTE: This transformation is only really needed if you need to create an empty
                directory somewhere in your package as an integration point.  All `debputy`
                transformations will create directories as required.

                In most cases, upstream build systems and `debputy` will create all the relevant
                directories.  However, in some rare cases you may want to explicitly define a path
                to be a directory.  Maybe to silence a linter that is warning you about a directory
                being empty, or maybe you need an empty directory that nothing else is creating for
                you. This can be done via the `create-directories` transformation rule.

                Unless you have a specific need for the mapping form, you are recommended to use the
                shorthand form of just listing the directories you want created.
        """
            ),
            non_mapping_description=textwrap.dedent(
                """\
            When the input is a string or a list of string, then that value is used as shorthand
            for `path` or `paths` (respectively).
        """
            ),
            attributes=[
                documented_attr(
                    ["path", "paths"],
                    textwrap.dedent(
                        """\
                        A path (`path`) or a list of path (`paths`) defining the path(s) inside the
                        package that should be created as directories. The path(es) _cannot_ use globs
                        but can use substitution variables.  Parent directories are implicitly created
                        (with owner `root:root` and mode `0755` - only explicitly listed directories
                        are affected by the owner/mode options)
            """
                    ),
                ),
                documented_attr(
                    "owner",
                    textwrap.dedent(
                        """\
                         Denotes the owner of the directory (but _not_ what is inside the directory).
                         Default is "root".
            """
                    ),
                ),
                documented_attr(
                    "group",
                    textwrap.dedent(
                        """\
                        Denotes the group of the directory (but _not_ what is inside the directory).
                        Default is "root".
            """
                    ),
                ),
                documented_attr(
                    "mode",
                    textwrap.dedent(
                        """\
                         Denotes the mode of the directory (but _not_ what is inside the directory).
                         Note that numeric mode must always be given as a string (i.e., with quotes).
                         Symbolic mode can be used as well. If symbolic mode uses a relative
                         definition (e.g., `o-rx`), then it is relative to the directory's current mode
                         (if it already exists) or `0755` if the directory is created by this
                         transformation.  The default is "0755".
            """
                    ),
                ),
                documented_attr(
                    "when",
                    textwrap.dedent(
                        """\
                A condition as defined in [Conditional rules]({MANIFEST_FORMAT_DOC}#Conditional rules).
            """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "create-directories-transformation-rule-directories"
            ),
        ),
    )


def register_manifest_condition_rules(api: DebputyPluginInitializerProvider) -> None:
    api.provide_manifest_keyword(
        ManifestCondition,
        "cross-compiling",
        lambda *_: ManifestCondition.is_cross_building(),
        inline_reference_documentation=reference_documentation(
            title="Cross-Compiling condition `cross-compiling`",
            description=textwrap.dedent(
                """\
                The `cross-compiling` condition is used to determine if the current build is
                performing a cross build (i.e., `DEB_BUILD_GNU_TYPE` != `DEB_HOST_GNU_TYPE`).
                Often this has consequences for what is possible to do.

                Note if you specifically want to know:

                 * whether build-time tests should be run, then please use the
                   `run-build-time-tests` condition.
                 * whether compiled binaries can be run as if it was a native binary, please
                   use the `can-execute-compiled-binaries` condition instead.  That condition
                   accounts for cross-building in its evaluation.
                """
            ),
            reference_documentation_url=_manifest_format_doc(
                "cross-compiling-condition-cross-compiling-string"
            ),
        ),
    )
    api.provide_manifest_keyword(
        ManifestCondition,
        "can-execute-compiled-binaries",
        lambda *_: ManifestCondition.can_execute_compiled_binaries(),
        inline_reference_documentation=reference_documentation(
            title="Can run produced binaries `can-execute-compiled-binaries`",
            description=textwrap.dedent(
                """\
                The `can-execute-compiled-binaries` condition is used to assert the build
                can assume that all compiled binaries can be run as-if they were native
                binaries. For native builds, this condition always evaluates to `true`.
                For cross builds, the condition is generally evaluates to `false`.  However,
                there are special-cases where binaries can be run during cross-building.
                Accordingly, this condition is subtly different from the `cross-compiling`
                condition.

                Note this condition should *not* be used when you know the binary has been
                built for the build architecture (`DEB_BUILD_ARCH`) or for determining
                whether build-time tests should be run (for build-time tests, please use
                the `run-build-time-tests` condition instead). Some upstream build systems
                are advanced enough to distinguish building a final product vs. building
                a helper tool that needs to run during build.  The latter will often be
                compiled by a separate compiler (often using `$(CC_FOR_BUILD)`,
                `cc_for_build` or similar variable names in upstream build systems for
                that compiler).
                """
            ),
            reference_documentation_url=_manifest_format_doc(
                "can-run-produced-binaries-can-execute-compiled-binaries-string"
            ),
        ),
    )
    api.provide_manifest_keyword(
        ManifestCondition,
        "run-build-time-tests",
        lambda *_: ManifestCondition.run_build_time_tests(),
        inline_reference_documentation=reference_documentation(
            title="Whether build time tests should be run `run-build-time-tests`",
            description=textwrap.dedent(
                """\
                The `run-build-time-tests` condition is used to determine whether (build
                time) tests should be run for this build.  This condition roughly
                translates into whether `nocheck` is present in `DEB_BUILD_OPTIONS`.

                In general, the manifest *should not* prevent build time tests from being
                run during cross-builds.
                """
            ),
            reference_documentation_url=_manifest_format_doc(
                "whether-build-time-tests-should-be-run-run-build-time-tests-string"
            ),
        ),
    )

    api.pluggable_manifest_rule(
        ManifestCondition,
        "not",
        MCNot,
        _mc_not,
        inline_reference_documentation=reference_documentation(
            title="Negated condition `not` (mapping)",
            description=textwrap.dedent(
                """\
                    It is possible to negate a condition via the `not` condition.

                    As an example:

                        packages:
                            util-linux:
                                transformations:
                                - create-symlink
                                      path: sbin/getty
                                      target: /sbin/agetty
                                      when:
                                          # On Hurd, the package "hurd" ships "sbin/getty".
                                          # This example happens to also be alternative to `arch-marches: '!hurd-any`
                                          not:
                                              arch-matches: 'hurd-any'

                    The `not` condition is specified as a mapping, where the key is `not` and the
                    value is a nested condition.
                """
            ),
            attributes=[
                documented_attr(
                    "negated_condition",
                    textwrap.dedent(
                        """\
                        The condition to be negated.
                        """
                    ),
                ),
            ],
            reference_documentation_url=_manifest_format_doc(
                "whether-build-time-tests-should-be-run-run-build-time-tests-string"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        ManifestCondition,
        ["any-of", "all-of"],
        MCAnyOfAllOf,
        _mc_any_of,
        source_format=List[ManifestCondition],
        inline_reference_documentation=reference_documentation(
            title="All or any of a list of conditions `all-of`/`any-of`",
            description=textwrap.dedent(
                """\
                It is possible to aggregate conditions using the `all-of` or `any-of`
                condition. This provide `X and Y` and `X or Y` semantics (respectively).
                """
            ),
            reference_documentation_url=_manifest_format_doc(
                "all-or-any-of-a-list-of-conditions-all-ofany-of-list"
            ),
        ),
    )
    api.pluggable_manifest_rule(
        ManifestCondition,
        "arch-matches",
        MCArchMatches,
        _mc_arch_matches,
        source_format=str,
        inline_reference_documentation=reference_documentation(
            title="Architecture match condition `arch-matches`",
            description=textwrap.dedent(
                """\
                Sometimes, a rule needs to be conditional on the architecture.
                This can be done by using the `arch-matches` rule. In 99.99%
                of the cases, `arch-matches` will be form you are looking for
                and practically behaves like a comparison against
                `dpkg-architecture -qDEB_HOST_ARCH`.

                For the cross-compiling specialists or curious people: The
                `arch-matches` rule behaves like a `package-context-arch-matches`
                in the context of a binary package and like
                `source-context-arch-matches` otherwise. The details of those
                are covered in their own keywords.
                """
            ),
            non_mapping_description=textwrap.dedent(
                """\
                The value must be a string in the form of a space separated list
                architecture names or architecture wildcards (same syntax as the
                architecture restriction in Build-Depends in debian/control except
                there is no enclosing `[]` brackets). The names/wildcards can
                optionally be prefixed by `!` to negate them.  However, either
                *all* names / wildcards must have negation or *none* of them may
                have it.
                """
            ),
            reference_documentation_url=_manifest_format_doc(
                "architecture-match-condition-arch-matches-mapping"
            ),
        ),
    )

    context_arch_doc = reference_documentation(
        title="Explicit source or binary package context architecture match condition"
        " `source-context-arch-matches`, `package-context-arch-matches` (mapping)",
        description=textwrap.dedent(
            """\
            **These are special-case conditions**. Unless you know that you have a very special-case,
            you should probably use `arch-matches` instead. These conditions are aimed at people with
            corner-case special architecture needs. It also assumes the reader is familiar with the
            `arch-matches` condition.

            To understand these rules, here is a quick primer on `debputy`'s concept of "source context"
            vs "(binary) package context" architecture.  For a native build, these two contexts are the
            same except that in the package context an `Architecture: all` package always resolve to
            `all` rather than `DEB_HOST_ARCH`. As a consequence, `debputy` forbids `arch-matches` and
            `package-context-arch-matches` in the context of an `Architecture: all` package as a warning
            to the packager that condition does not make sense.

            In the very rare case that you need an architecture condition for an `Architecture: all` package,
            you can use `source-context-arch-matches`. However, this means your `Architecture: all` package
            is not reproducible between different build hosts (which has known to be relevant for some
            very special cases).

            Additionally, for the 0.0001% case you are building a cross-compiling compiler (that is,
            `DEB_HOST_ARCH != DEB_TARGET_ARCH` and you are working with `gcc` or similar) `debputy` can be
            instructed (opt-in) to use `DEB_TARGET_ARCH` rather than `DEB_HOST_ARCH` for certain packages when
            evaluating an architecture condition in context of a binary package. This can be useful if the
            compiler produces supporting libraries that need to be built for the `DEB_TARGET_ARCH` rather than
            the `DEB_HOST_ARCH`.  This is where `arch-matches` or `package-context-arch-matches` can differ
            subtly from `source-context-arch-matches` in how they evaluate the condition.  This opt-in currently
            relies on setting `X-DH-Build-For-Type: target` for each of the relevant packages in
            `debian/control`.  However, unless you are a cross-compiling specialist, you will probably never
            need to care about nor use any of this.

            Accordingly, the possible conditions are:

             * `arch-matches`: This is the form recommended to laymen and as the default use-case. This
               conditional acts `package-context-arch-matches` if the condition is used in the context
               of a binary package. Otherwise, it acts as `source-context-arch-matches`.

             * `source-context-arch-matches`: With this conditional, the provided architecture constraint is compared
               against the build time provided host architecture (`dpkg-architecture -qDEB_HOST_ARCH`). This can
               be useful when an `Architecture: all` package needs an architecture condition for some reason.

             * `package-context-arch-matches`: With this conditional, the provided architecture constraint is compared
               against the package's resolved architecture. This condition can only be used in the context of a binary
               package (usually, under `packages.<name>.`).  If the package is an `Architecture: all` package, the
               condition will fail with an error as the condition always have the same outcome. For all other
               packages, the package's resolved architecture is the same as the build time provided host architecture
               (`dpkg-architecture -qDEB_HOST_ARCH`).

               - However, as noted above there is a special case for when compiling a cross-compiling compiler, where
                 this behaves subtly different from `source-context-arch-matches`.

            All conditions are used the same way as `arch-matches`. Simply replace `arch-matches` with the other
            condition. See the `arch-matches` description for an example.
            """
        ),
        non_mapping_description=textwrap.dedent(
            """\
            The value must be a string in the form of a space separated list
            architecture names or architecture wildcards (same syntax as the
            architecture restriction in Build-Depends in debian/control except
            there is no enclosing `[]` brackets). The names/wildcards can
            optionally be prefixed by `!` to negate them.  However, either
            *all* names / wildcards must have negation or *none* of them may
            have it.
            """
        ),
    )

    api.pluggable_manifest_rule(
        ManifestCondition,
        "source-context-arch-matches",
        MCArchMatches,
        _mc_source_context_arch_matches,
        source_format=str,
        inline_reference_documentation=context_arch_doc,
    )
    api.pluggable_manifest_rule(
        ManifestCondition,
        "package-context-arch-matches",
        MCArchMatches,
        _mc_arch_matches,
        source_format=str,
        inline_reference_documentation=context_arch_doc,
    )
    api.pluggable_manifest_rule(
        ManifestCondition,
        "build-profiles-matches",
        MCBuildProfileMatches,
        _mc_build_profile_matches,
        source_format=str,
        inline_reference_documentation=reference_documentation(
            title="Active build profile match condition `build-profiles-matches`",
            description=textwrap.dedent(
                """\
                The `build-profiles-matches` condition is used to assert whether the
                active build profiles (`DEB_BUILD_PROFILES` / `dpkg-buildpackage -P`)
                matches a given build profile restriction.
                """
            ),
            non_mapping_description=textwrap.dedent(
                """\
                The value is a string using the same syntax as the `Build-Profiles`
                field from `debian/control` (i.e., a space separated list of
                `<[!]profile ...>` groups).
                """
            ),
            reference_documentation_url=_manifest_format_doc(
                "active-build-profile-match-condition-build-profiles-matches-mapping"
            ),
        ),
    )


def register_dpkg_conffile_rules(api: DebputyPluginInitializerProvider) -> None:
    api.pluggable_manifest_rule(
        DpkgMaintscriptHelperCommand,
        "remove",
        DpkgRemoveConffileRule,
        _dpkg_conffile_remove,
        inline_reference_documentation=None,  # TODO: write and add
    )

    api.pluggable_manifest_rule(
        DpkgMaintscriptHelperCommand,
        "rename",
        DpkgRenameConffileRule,
        _dpkg_conffile_rename,
        inline_reference_documentation=None,  # TODO: write and add
    )


class _ModeOwnerBase(DebputyParsedContentStandardConditional):
    mode: NotRequired[FileSystemMode]
    owner: NotRequired[StaticFileSystemOwner]
    group: NotRequired[StaticFileSystemGroup]


class PathManifestSourceDictFormat(_ModeOwnerBase):
    path: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("paths")]
    ]
    paths: NotRequired[List[FileSystemMatchRule]]
    recursive: NotRequired[bool]
    capabilities: NotRequired[str]
    capability_mode: NotRequired[FileSystemMode]


class PathManifestRule(_ModeOwnerBase):
    paths: List[FileSystemMatchRule]
    recursive: NotRequired[bool]
    capabilities: NotRequired[str]
    capability_mode: NotRequired[FileSystemMode]


class EnsureDirectorySourceFormat(_ModeOwnerBase):
    path: NotRequired[
        Annotated[FileSystemExactMatchRule, DebputyParseHint.target_attribute("paths")]
    ]
    paths: NotRequired[List[FileSystemExactMatchRule]]


class EnsureDirectoryRule(_ModeOwnerBase):
    paths: List[FileSystemExactMatchRule]


class CreateSymlinkRule(DebputyParsedContentStandardConditional):
    path: FileSystemExactMatchRule
    target: Annotated[SymlinkTarget, DebputyParseHint.not_path_error_hint()]
    replacement_rule: NotRequired[CreateSymlinkReplacementRule]


class TransformationMoveRuleSpec(DebputyParsedContentStandardConditional):
    source: FileSystemMatchRule
    target: FileSystemExactMatchRule


class TransformationRemoveRuleSpec(DebputyParsedContentStandardConditional):
    paths: List[FileSystemMatchRule]
    keep_empty_parent_dirs: NotRequired[bool]


class TransformationRemoveRuleInputFormat(DebputyParsedContentStandardConditional):
    path: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("paths")]
    ]
    paths: NotRequired[List[FileSystemMatchRule]]
    keep_empty_parent_dirs: NotRequired[bool]


class ParsedInstallRuleSourceFormat(DebputyParsedContentStandardConditional):
    sources: NotRequired[List[FileSystemMatchRule]]
    source: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("sources")]
    ]
    into: NotRequired[
        Annotated[
            Union[str, List[str]],
            DebputyParseHint.required_when_multi_binary(),
        ]
    ]
    dest_dir: NotRequired[
        Annotated[FileSystemExactMatchRule, DebputyParseHint.not_path_error_hint()]
    ]
    install_as: NotRequired[
        Annotated[
            FileSystemExactMatchRule,
            DebputyParseHint.conflicts_with_source_attributes("sources", "dest_dir"),
            DebputyParseHint.manifest_attribute("as"),
            DebputyParseHint.not_path_error_hint(),
        ]
    ]


class ParsedInstallDocRuleSourceFormat(DebputyParsedContentStandardConditional):
    sources: NotRequired[List[FileSystemMatchRule]]
    source: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("sources")]
    ]
    into: NotRequired[
        Annotated[
            Union[str, List[str]],
            DebputyParseHint.required_when_multi_binary(package_type="deb"),
        ]
    ]
    dest_dir: NotRequired[
        Annotated[FileSystemExactMatchRule, DebputyParseHint.not_path_error_hint()]
    ]
    install_as: NotRequired[
        Annotated[
            FileSystemExactMatchRule,
            DebputyParseHint.conflicts_with_source_attributes("sources", "dest_dir"),
            DebputyParseHint.manifest_attribute("as"),
            DebputyParseHint.not_path_error_hint(),
        ]
    ]


class ParsedInstallRule(DebputyParsedContentStandardConditional):
    sources: List[FileSystemMatchRule]
    into: NotRequired[List[BinaryPackage]]
    dest_dir: NotRequired[FileSystemExactMatchRule]
    install_as: NotRequired[FileSystemExactMatchRule]


class ParsedMultiDestInstallRuleSourceFormat(DebputyParsedContentStandardConditional):
    sources: NotRequired[List[FileSystemMatchRule]]
    source: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("sources")]
    ]
    into: NotRequired[
        Annotated[
            Union[str, List[str]],
            DebputyParseHint.required_when_multi_binary(),
        ]
    ]
    dest_dirs: NotRequired[
        Annotated[
            List[FileSystemExactMatchRule], DebputyParseHint.not_path_error_hint()
        ]
    ]
    install_as: NotRequired[
        Annotated[
            List[FileSystemExactMatchRule],
            DebputyParseHint.conflicts_with_source_attributes("sources", "dest_dirs"),
            DebputyParseHint.not_path_error_hint(),
            DebputyParseHint.manifest_attribute("as"),
        ]
    ]


class ParsedMultiDestInstallRule(DebputyParsedContentStandardConditional):
    sources: List[FileSystemMatchRule]
    into: NotRequired[List[BinaryPackage]]
    dest_dirs: NotRequired[List[FileSystemExactMatchRule]]
    install_as: NotRequired[List[FileSystemExactMatchRule]]


class ParsedInstallExamplesRule(DebputyParsedContentStandardConditional):
    sources: List[FileSystemMatchRule]
    into: NotRequired[List[BinaryPackage]]


class ParsedInstallExamplesRuleSourceFormat(DebputyParsedContentStandardConditional):
    sources: NotRequired[List[FileSystemMatchRule]]
    source: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("sources")]
    ]
    into: NotRequired[
        Annotated[
            Union[str, List[str]],
            DebputyParseHint.required_when_multi_binary(package_type="deb"),
        ]
    ]


class ParsedInstallManpageRule(DebputyParsedContentStandardConditional):
    sources: List[FileSystemMatchRule]
    language: NotRequired[str]
    section: NotRequired[int]
    into: NotRequired[List[BinaryPackage]]


class ParsedInstallManpageRuleSourceFormat(DebputyParsedContentStandardConditional):
    sources: NotRequired[List[FileSystemMatchRule]]
    source: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("sources")]
    ]
    language: NotRequired[str]
    section: NotRequired[int]
    into: NotRequired[
        Annotated[
            Union[str, List[str]],
            DebputyParseHint.required_when_multi_binary(package_type="deb"),
        ]
    ]


class ParsedInstallDiscardRuleSourceFormat(DebputyParsedContent):
    paths: NotRequired[List[FileSystemMatchRule]]
    path: NotRequired[
        Annotated[FileSystemMatchRule, DebputyParseHint.target_attribute("paths")]
    ]
    search_dir: NotRequired[
        Annotated[
            FileSystemExactMatchRule, DebputyParseHint.target_attribute("search_dirs")
        ]
    ]
    search_dirs: NotRequired[List[FileSystemExactMatchRule]]
    required_when: NotRequired[ManifestCondition]


class ParsedInstallDiscardRule(DebputyParsedContent):
    paths: List[FileSystemMatchRule]
    search_dirs: NotRequired[List[FileSystemExactMatchRule]]
    required_when: NotRequired[ManifestCondition]


class DpkgConffileManagementRuleBase(DebputyParsedContent):
    prior_to_version: NotRequired[str]
    owning_package: NotRequired[str]


class DpkgRenameConffileRule(DpkgConffileManagementRuleBase):
    source: str
    target: str


class DpkgRemoveConffileRule(DpkgConffileManagementRuleBase):
    path: str


class MCAnyOfAllOf(DebputyParsedContent):
    conditions: List[ManifestCondition]


class MCNot(DebputyParsedContent):
    negated_condition: Annotated[
        ManifestCondition, DebputyParseHint.manifest_attribute("not")
    ]


class MCArchMatches(DebputyParsedContent):
    arch_matches: str


class MCBuildProfileMatches(DebputyParsedContent):
    build_profile_matches: str


def _parse_filename(
    filename: str,
    attribute_path: AttributePath,
    *,
    allow_directories: bool = True,
) -> str:
    try:
        normalized_path = _normalize_path(filename, with_prefix=False)
    except ValueError as e:
        raise ManifestParseException(
            f'Error parsing the path "{filename}" defined in {attribute_path.path}: {e.args[0]}'
        ) from None
    if not allow_directories and filename.endswith("/"):
        raise ManifestParseException(
            f'The path "{filename}" in {attribute_path.path} ends with "/" implying it is a directory,'
            f" but this feature can only be used for files"
        )
    if normalized_path == ".":
        raise ManifestParseException(
            f'The path "{filename}" in {attribute_path.path} looks like the root directory,'
            f" but this feature does not allow the root directory here."
        )
    return normalized_path


def _with_alt_form(t: Type[TypedDict]):
    return Union[
        t,
        List[str],
        str,
    ]


def _dpkg_conffile_rename(
    _name: str,
    parsed_data: DpkgRenameConffileRule,
    path: AttributePath,
    _context: ParserContextData,
) -> DpkgMaintscriptHelperCommand:
    source_file = parsed_data["source"]
    target_file = parsed_data["target"]
    normalized_source = _parse_filename(
        source_file,
        path["source"],
        allow_directories=False,
    )
    path.path_hint = source_file

    normalized_target = _parse_filename(
        target_file,
        path["target"],
        allow_directories=False,
    )
    normalized_source = "/" + normalized_source
    normalized_target = "/" + normalized_target

    if normalized_source == normalized_target:
        raise ManifestParseException(
            f"Invalid rename defined in {path.path}: The source and target path are the same!"
        )

    version, owning_package = _parse_conffile_prior_version_and_owning_package(
        parsed_data, path
    )
    return DpkgMaintscriptHelperCommand.mv_conffile(
        path,
        normalized_source,
        normalized_target,
        version,
        owning_package,
    )


def _dpkg_conffile_remove(
    _name: str,
    parsed_data: DpkgRemoveConffileRule,
    path: AttributePath,
    _context: ParserContextData,
) -> DpkgMaintscriptHelperCommand:
    source_file = parsed_data["path"]
    normalized_source = _parse_filename(
        source_file,
        path["path"],
        allow_directories=False,
    )
    path.path_hint = source_file

    normalized_source = "/" + normalized_source

    version, owning_package = _parse_conffile_prior_version_and_owning_package(
        parsed_data, path
    )
    return DpkgMaintscriptHelperCommand.rm_conffile(
        path,
        normalized_source,
        version,
        owning_package,
    )


def _parse_conffile_prior_version_and_owning_package(
    d: DpkgConffileManagementRuleBase,
    attribute_path: AttributePath,
) -> Tuple[Optional[str], Optional[str]]:
    prior_version = d.get("prior_to_version")
    owning_package = d.get("owning_package")

    if prior_version is not None and not PKGVERSION_REGEX.match(prior_version):
        p = attribute_path["prior_to_version"]
        raise ManifestParseException(
            f"The {MK_CONFFILE_MANAGEMENT_X_PRIOR_TO_VERSION} parameter in {p.path} must be a"
            r" valid package version (i.e., match (?:\d+:)?\d[0-9A-Za-z.+:~]*(?:-[0-9A-Za-z.+:~]+)*)."
        )

    if owning_package is not None and not PKGNAME_REGEX.match(owning_package):
        p = attribute_path["owning_package"]
        raise ManifestParseException(
            f"The {MK_CONFFILE_MANAGEMENT_X_OWNING_PACKAGE} parameter in {p.path} must be a valid"
            f" package name (i.e., match {PKGNAME_REGEX.pattern})."
        )

    return prior_version, owning_package


def _install_rule_handler(
    _name: str,
    parsed_data: ParsedInstallRule,
    path: AttributePath,
    context: ParserContextData,
) -> InstallRule:
    sources = parsed_data["sources"]
    install_as = parsed_data.get("install_as")
    into = parsed_data.get("into")
    dest_dir = parsed_data.get("dest_dir")
    condition = parsed_data.get("when")
    if not into:
        into = [context.single_binary_package(path, package_attribute="into")]
    into = frozenset(into)
    if install_as is not None:
        assert len(sources) == 1
        assert dest_dir is None
        return InstallRule.install_as(
            sources[0],
            install_as.match_rule.path,
            into,
            path.path,
            condition,
        )
    return InstallRule.install_dest(
        sources,
        dest_dir.match_rule.path if dest_dir is not None else None,
        into,
        path.path,
        condition,
    )


def _multi_dest_install_rule_handler(
    _name: str,
    parsed_data: ParsedMultiDestInstallRule,
    path: AttributePath,
    context: ParserContextData,
) -> InstallRule:
    sources = parsed_data["sources"]
    install_as = parsed_data.get("install_as")
    into = parsed_data.get("into")
    dest_dirs = parsed_data.get("dest_dirs")
    condition = parsed_data.get("when")
    if not into:
        into = [context.single_binary_package(path, package_attribute="into")]
    into = frozenset(into)
    if install_as is not None:
        assert len(sources) == 1
        assert dest_dirs is None
        if len(install_as) < 2:
            raise ManifestParseException(
                f"The {path['install_as'].path} attribute must contain at least two paths."
            )
        return InstallRule.install_multi_as(
            sources[0],
            [p.match_rule.path for p in install_as],
            into,
            path.path,
            condition,
        )
    if dest_dirs is None:
        raise ManifestParseException(
            f"Either the `as` or the `dest-dirs` key must be provided at {path.path}"
        )
    if len(dest_dirs) < 2:
        raise ManifestParseException(
            f"The {path['dest_dirs'].path} attribute must contain at least two paths."
        )
    return InstallRule.install_multi_dest(
        sources,
        [dd.match_rule.path for dd in dest_dirs],
        into,
        path.path,
        condition,
    )


def _install_docs_rule_handler(
    _name: str,
    parsed_data: ParsedInstallRule,
    path: AttributePath,
    context: ParserContextData,
) -> InstallRule:
    sources = parsed_data["sources"]
    install_as = parsed_data.get("install_as")
    into = parsed_data.get("into")
    dest_dir = parsed_data.get("dest_dir")
    condition = parsed_data.get("when")
    if not into:
        into = [
            context.single_binary_package(
                path, package_type="deb", package_attribute="into"
            )
        ]
    into = frozenset(into)
    if install_as is not None:
        assert len(sources) == 1
        assert dest_dir is None
        return InstallRule.install_doc_as(
            sources[0],
            install_as.match_rule.path,
            into,
            path.path,
            condition,
        )
    return InstallRule.install_doc(
        sources,
        dest_dir,
        into,
        path.path,
        condition,
    )


def _install_examples_rule_handler(
    _name: str,
    parsed_data: ParsedInstallExamplesRule,
    path: AttributePath,
    context: ParserContextData,
) -> InstallRule:
    sources = parsed_data["sources"]
    into = parsed_data.get("into")
    if not into:
        into = [
            context.single_binary_package(
                path, package_type="deb", package_attribute="into"
            )
        ]
    condition = parsed_data.get("when")
    into = frozenset(into)
    return InstallRule.install_examples(
        sources,
        into,
        path.path,
        condition,
    )


def _install_man_rule_handler(
    _name: str,
    parsed_data: ParsedInstallManpageRule,
    attribute_path: AttributePath,
    context: ParserContextData,
) -> InstallRule:
    sources = parsed_data["sources"]
    language = parsed_data.get("language")
    section = parsed_data.get("section")

    if language is not None:
        is_lang_ok = language in (
            "C",
            "derive-from-basename",
            "derive-from-path",
        )

        if not is_lang_ok and len(language) == 2 and language.islower():
            is_lang_ok = True

        if (
            not is_lang_ok
            and len(language) == 5
            and language[2] == "_"
            and language[:2].islower()
            and language[3:].isupper()
        ):
            is_lang_ok = True

        if not is_lang_ok:
            raise ManifestParseException(
                f'The language attribute must in a 2-letter language code ("de"), a 5-letter language + dialect'
                f' code ("pt_BR"), "derive-from-basename", "derive-from-path", or omitted.  The problematic'
                f' definition is {attribute_path["language"]}'
            )

    if section is not None and (section < 1 or section > 10):
        raise ManifestParseException(
            f"The section attribute must in the range [1-9] or omitted.  The problematic definition is"
            f' {attribute_path["section"]}'
        )
    if section is None and any(s.raw_match_rule.endswith(".gz") for s in sources):
        raise ManifestParseException(
            "Sorry, compressed man pages are not supported without an explicit `section` definition at the moment."
            " This limitation may be removed in the future.  Problematic definition from"
            f' {attribute_path["sources"]}'
        )
    if any(s.raw_match_rule.endswith("/") for s in sources):
        raise ManifestParseException(
            'The install-man rule can only match non-directories.  Therefore, none of the sources can end with "/".'
            " as that implies the source is for a directory.  Problematic definition from"
            f' {attribute_path["sources"]}'
        )
    into = parsed_data.get("into")
    if not into:
        into = [
            context.single_binary_package(
                attribute_path, package_type="deb", package_attribute="into"
            )
        ]
    condition = parsed_data.get("when")
    into = frozenset(into)
    return InstallRule.install_man(
        sources,
        into,
        section,
        language,
        attribute_path.path,
        condition,
    )


def _install_discard_rule_handler(
    _name: str,
    parsed_data: ParsedInstallDiscardRule,
    path: AttributePath,
    _context: ParserContextData,
) -> InstallRule:
    limit_to = parsed_data.get("search_dirs")
    if limit_to is not None and not limit_to:
        p = path["search_dirs"]
        raise ManifestParseException(f"The {p.path} attribute must not be empty.")
    condition = parsed_data.get("required_when")
    return InstallRule.discard_paths(
        parsed_data["paths"],
        path.path,
        condition,
        limit_to=limit_to,
    )


def _transformation_move_handler(
    _name: str,
    parsed_data: TransformationMoveRuleSpec,
    path: AttributePath,
    _context: ParserContextData,
) -> TransformationRule:
    source_match = parsed_data["source"]
    target_path = parsed_data["target"].match_rule.path
    condition = parsed_data.get("when")

    if (
        isinstance(source_match, ExactFileSystemPath)
        and source_match.path == target_path
    ):
        raise ManifestParseException(
            f"The transformation rule {path.path} requests a move of {source_match} to"
            f" {target_path}, which is the same path"
        )
    return MoveTransformationRule(
        source_match.match_rule,
        target_path,
        target_path.endswith("/"),
        path,
        condition,
    )


def _transformation_remove_handler(
    _name: str,
    parsed_data: TransformationRemoveRuleSpec,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> TransformationRule:
    paths = parsed_data["paths"]
    keep_empty_parent_dirs = parsed_data.get("keep_empty_parent_dirs", False)

    return RemoveTransformationRule(
        [m.match_rule for m in paths],
        keep_empty_parent_dirs,
        attribute_path,
    )


def _transformation_create_symlink(
    _name: str,
    parsed_data: CreateSymlinkRule,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> TransformationRule:
    link_dest = parsed_data["path"].match_rule.path
    replacement_rule: CreateSymlinkReplacementRule = parsed_data.get(
        "replacement_rule",
        "abort-on-non-empty-directory",
    )
    try:
        link_target = debian_policy_normalize_symlink_target(
            link_dest,
            parsed_data["target"].symlink_target,
        )
    except ValueError as e:  # pragma: no cover
        raise AssertionError(
            "Debian Policy normalization should not raise ValueError here"
        ) from e

    condition = parsed_data.get("when")

    return CreateSymlinkPathTransformationRule(
        link_target,
        link_dest,
        replacement_rule,
        attribute_path,
        condition,
    )


def _transformation_path_metadata(
    _name: str,
    parsed_data: PathManifestRule,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> TransformationRule:
    match_rules = parsed_data["paths"]
    owner = parsed_data.get("owner")
    group = parsed_data.get("group")
    mode = parsed_data.get("mode")
    recursive = parsed_data.get("recursive", False)
    capabilities = parsed_data.get("capabilities")
    capability_mode = parsed_data.get("capability_mode")

    if capabilities is not None:
        if capability_mode is None:
            capability_mode = SymbolicMode.parse_filesystem_mode(
                "a-s",
                attribute_path["capability-mode"],
            )
        validate_cap = check_cap_checker()
        validate_cap(capabilities, attribute_path["capabilities"].path)
    elif capability_mode is not None and capabilities is None:
        raise ManifestParseException(
            "The attribute capability-mode cannot be provided without capabilities"
            f" in {attribute_path.path}"
        )
    if owner is None and group is None and mode is None and capabilities is None:
        raise ManifestParseException(
            "At least one of owner, group, mode, or capabilities must be provided"
            f" in {attribute_path.path}"
        )
    condition = parsed_data.get("when")

    return PathMetadataTransformationRule(
        [m.match_rule for m in match_rules],
        owner,
        group,
        mode,
        recursive,
        capabilities,
        capability_mode,
        attribute_path.path,
        condition,
    )


def _transformation_mkdirs(
    _name: str,
    parsed_data: EnsureDirectoryRule,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> TransformationRule:
    provided_paths = parsed_data["paths"]
    owner = parsed_data.get("owner")
    group = parsed_data.get("group")
    mode = parsed_data.get("mode")

    condition = parsed_data.get("when")

    return CreateDirectoryTransformationRule(
        [p.match_rule.path for p in provided_paths],
        owner,
        group,
        mode,
        attribute_path.path,
        condition,
    )


def _at_least_two(
    content: List[Any],
    attribute_path: AttributePath,
    attribute_name: str,
) -> None:
    if len(content) < 2:
        raise ManifestParseException(
            f"Must have at least two conditions in {attribute_path[attribute_name].path}"
        )


def _mc_any_of(
    name: str,
    parsed_data: MCAnyOfAllOf,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> ManifestCondition:
    conditions = parsed_data["conditions"]
    _at_least_two(conditions, attribute_path, "conditions")
    if name == "any-of":
        return ManifestCondition.any_of(conditions)
    assert name == "all-of"
    return ManifestCondition.all_of(conditions)


def _mc_not(
    _name: str,
    parsed_data: MCNot,
    _attribute_path: AttributePath,
    _context: ParserContextData,
) -> ManifestCondition:
    condition = parsed_data["negated_condition"]
    return condition.negated()


def _extract_arch_matches(
    parsed_data: MCArchMatches,
    attribute_path: AttributePath,
) -> List[str]:
    arch_matches_as_str = parsed_data["arch_matches"]
    # Can we check arch list for typos? If we do, it must be tight in how close matches it does.
    # Consider "arm" vs. "armel" (edit distance 2, but both are valid).  Likewise, names often
    # include a bit indicator "foo", "foo32", "foo64" - all of these have an edit distance of 2
    # of each other.
    arch_matches_as_list = arch_matches_as_str.split()
    attr_path = attribute_path["arch_matches"]
    if not arch_matches_as_list:
        raise ManifestParseException(
            f"The condition at {attr_path.path} must not be empty"
        )

    if arch_matches_as_list[0].startswith("[") or arch_matches_as_list[-1].endswith(
        "]"
    ):
        raise ManifestParseException(
            f"The architecture match at {attr_path.path} must be defined without enclosing it with "
            '"[" or/and "]" brackets'
        )
    return arch_matches_as_list


def _mc_source_context_arch_matches(
    _name: str,
    parsed_data: MCArchMatches,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> ManifestCondition:
    arch_matches = _extract_arch_matches(parsed_data, attribute_path)
    return SourceContextArchMatchManifestCondition(arch_matches)


def _mc_package_context_arch_matches(
    name: str,
    parsed_data: MCArchMatches,
    attribute_path: AttributePath,
    context: ParserContextData,
) -> ManifestCondition:
    arch_matches = _extract_arch_matches(parsed_data, attribute_path)

    if not context.is_in_binary_package_state:
        raise ManifestParseException(
            f'The condition "{name}" at {attribute_path.path} can only be used in the context of a binary package.'
        )

    package_state = context.current_binary_package_state
    if package_state.binary_package.is_arch_all:
        result = context.dpkg_arch_query_table.architecture_is_concerned(
            "all", arch_matches
        )
        attr_path = attribute_path["arch_matches"]
        raise ManifestParseException(
            f"The package architecture restriction at {attr_path.path} is applied to the"
            f' "Architecture: all" package {package_state.binary_package.name}, which does not make sense'
            f" as the condition will always resolves to `{str(result).lower()}`."
            f" If you **really** need an architecture specific constraint for this rule, consider using"
            f' "source-context-arch-matches" instead. However, this is a very rare use-case!'
        )
    return BinaryPackageContextArchMatchManifestCondition(arch_matches)


def _mc_arch_matches(
    name: str,
    parsed_data: MCArchMatches,
    attribute_path: AttributePath,
    context: ParserContextData,
) -> ManifestCondition:
    if context.is_in_binary_package_state:
        return _mc_package_context_arch_matches(
            name, parsed_data, attribute_path, context
        )
    return _mc_source_context_arch_matches(name, parsed_data, attribute_path, context)


def _mc_build_profile_matches(
    _name: str,
    parsed_data: MCBuildProfileMatches,
    attribute_path: AttributePath,
    _context: ParserContextData,
) -> ManifestCondition:
    build_profile_spec = parsed_data["build_profile_matches"].strip()
    attr_path = attribute_path["build_profile_matches"]
    if not build_profile_spec:
        raise ManifestParseException(
            f"The condition at {attr_path.path} must not be empty"
        )
    try:
        active_profiles_match(build_profile_spec, frozenset())
    except ValueError as e:
        raise ManifestParseException(
            f"Could not parse the build specification at {attr_path.path}: {e.args[0]}"
        )
    return BuildProfileMatch(build_profile_spec)
