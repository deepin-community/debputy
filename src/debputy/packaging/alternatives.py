import textwrap
from typing import List, Dict, Tuple, Mapping

from debian.deb822 import Deb822

from debputy.maintscript_snippet import MaintscriptSnippetContainer, MaintscriptSnippet
from debputy.packager_provided_files import PackagerProvidedFile
from debputy.packages import BinaryPackage
from debputy.packaging.makeshlibs import resolve_reserved_provided_file
from debputy.plugin.api import VirtualPath
from debputy.util import _error, escape_shell, POSTINST_DEFAULT_CONDITION

# Match debhelper (minus one space in each end, which comes
# via join).
LINE_PREFIX = "\\\n            "


def process_alternatives(
    binary_package: BinaryPackage,
    fs_root: VirtualPath,
    reserved_packager_provided_files: Dict[str, List[PackagerProvidedFile]],
    maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
) -> None:
    if binary_package.is_udeb:
        return

    provided_alternatives_file = resolve_reserved_provided_file(
        "alternatives",
        reserved_packager_provided_files,
    )
    if provided_alternatives_file is None:
        return

    with provided_alternatives_file.open() as fd:
        alternatives = list(Deb822.iter_paragraphs(fd))

    for no, alternative in enumerate(alternatives):
        process_alternative(
            provided_alternatives_file.fs_path,
            fs_root,
            alternative,
            no,
            maintscript_snippets,
        )


def process_alternative(
    provided_alternatives_fs_path: str,
    fs_root: VirtualPath,
    alternative_deb822: Deb822,
    no: int,
    maintscript_snippets: Dict[str, MaintscriptSnippetContainer],
) -> None:
    name = _mandatory_key(
        "Name",
        alternative_deb822,
        provided_alternatives_fs_path,
        f"Stanza number {no}",
    )
    error_context = f"Alternative named {name}"
    link_path = _mandatory_key(
        "Link",
        alternative_deb822,
        provided_alternatives_fs_path,
        error_context,
    )
    impl_path = _mandatory_key(
        "Alternative",
        alternative_deb822,
        provided_alternatives_fs_path,
        error_context,
    )
    priority = _mandatory_key(
        "Priority",
        alternative_deb822,
        provided_alternatives_fs_path,
        error_context,
    )
    if "/" in name:
        _error(
            f'The "Name" ({link_path}) key must be a basename and cannot contain slashes'
            f" ({error_context} in {provided_alternatives_fs_path})"
        )
    if link_path == impl_path:
        _error(
            f'The "Link" key and the "Alternative" key must not have the same value'
            f" ({error_context} in {provided_alternatives_fs_path})"
        )
    impl = fs_root.lookup(impl_path)
    if impl is None or impl.is_dir:
        _error(
            f'The path listed in "Alternative" ("{impl_path}") does not exist'
            f" in the package. ({error_context} in {provided_alternatives_fs_path})"
        )
    for key in ["Slave", "Slaves", "Slave-Links"]:
        if key in alternative_deb822:
            _error(
                f'Please use "Dependents" instead of "{key}".'
                f" ({error_context} in {provided_alternatives_fs_path})"
            )
    dependents = alternative_deb822.get("Dependents")
    install_command = [
        escape_shell(
            "update-alternatives",
            "--install",
            link_path,
            name,
            impl_path,
            priority,
        )
    ]
    remove_command = [
        escape_shell(
            "update-alternatives",
            "--remove",
            name,
            impl_path,
        )
    ]
    if dependents:
        seen_link_path = set()
        for line in dependents.splitlines():
            line = line.strip()
            if not line:  # First line is usually empty
                continue
            dlink_path, dlink_name, dimpl_path = parse_dependent_link(
                line,
                error_context,
                provided_alternatives_fs_path,
            )
            if dlink_path in seen_link_path:
                _error(
                    f'The Dependent link path "{dlink_path}" was used twice.'
                    f" ({error_context} in {provided_alternatives_fs_path})"
                )
            dimpl = fs_root.lookup(dimpl_path)
            if dimpl is None or dimpl.is_dir:
                _error(
                    f'The path listed in "Dependents" ("{dimpl_path}") does not exist'
                    f" in the package. ({error_context} in {provided_alternatives_fs_path})"
                )
            seen_link_path.add(dlink_path)
            install_command.append(LINE_PREFIX)
            install_command.append(
                escape_shell(
                    # update-alternatives still uses this old option name :-/
                    "--slave",
                    dlink_path,
                    dlink_name,
                    dimpl_path,
                )
            )
    postinst = textwrap.dedent(
        """\
    if {CONDITION}; then
        {COMMAND}
    fi
    """
    ).format(
        CONDITION=POSTINST_DEFAULT_CONDITION,
        COMMAND=" ".join(install_command),
    )

    prerm = textwrap.dedent(
        """\
    if [ "$1" = "remove" ]; then
        {COMMAND}
    fi
    """
    ).format(COMMAND=" ".join(remove_command))
    maintscript_snippets["postinst"].append(
        MaintscriptSnippet(
            f"debputy (via {provided_alternatives_fs_path})",
            snippet=postinst,
        )
    )
    maintscript_snippets["prerm"].append(
        MaintscriptSnippet(
            f"debputy (via {provided_alternatives_fs_path})",
            snippet=prerm,
        )
    )


def parse_dependent_link(
    line: str,
    error_context: str,
    provided_alternatives_file: str,
) -> Tuple[str, str, str]:
    parts = line.split()
    if len(parts) != 3:
        if len(parts) > 1:
            pass
        _error(
            f"The each line in Dependents links must have exactly 3 space separated parts."
            f' The "{line}" split into {len(parts)} part(s).'
            f" ({error_context} in {provided_alternatives_file})"
        )

    dlink_path, dlink_name, dimpl_path = parts
    if "/" in dlink_name:
        _error(
            f'The Dependent link name "{dlink_path}" must be a basename and cannot contain slashes'
            f" ({error_context} in {provided_alternatives_file})"
        )
    if dlink_path == dimpl_path:
        _error(
            f'The Dependent Link path and Alternative must not have the same value ["{dlink_path}"]'
            f" ({error_context} in {provided_alternatives_file})"
        )
    return dlink_path, dlink_name, dimpl_path


def _mandatory_key(
    key: str,
    alternative_deb822: Mapping[str, str],
    provided_alternatives_file: str,
    error_context: str,
) -> str:
    try:
        return alternative_deb822[key]
    except KeyError:
        _error(
            f'Missing mandatory key "{key}" in {provided_alternatives_file} ({error_context})'
        )
