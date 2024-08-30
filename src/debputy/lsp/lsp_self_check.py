import dataclasses
import os.path
import subprocess
from typing import Callable, Sequence, List, Optional, TypeVar

from debian.debian_support import Version

from debputy.util import _error


@dataclasses.dataclass(slots=True, frozen=True)
class LSPSelfCheck:
    feature: str
    test: Callable[[], bool]
    problem: str
    how_to_fix: str
    is_mandatory: bool = False


LSP_CHECKS: List[LSPSelfCheck] = []

C = TypeVar("C", bound="Callable")


def lsp_import_check(
    packages: Sequence[str],
    *,
    feature_name: Optional[str] = None,
    is_mandatory: bool = False,
) -> Callable[[C], C]:

    def _wrapper(func: C) -> C:

        def _impl():
            try:
                r = func()
            except ImportError:
                return False
            return r is None or bool(r)

        suffix = "fix this issue" if is_mandatory else "enable this feature"

        LSP_CHECKS.append(
            LSPSelfCheck(
                _feature_name(feature_name, func),
                _impl,
                "Missing dependencies",
                f"Run `apt satisfy '{', '.join(packages)}'` to {suffix}",
                is_mandatory=is_mandatory,
            )
        )
        return func

    return _wrapper


def lsp_generic_check(
    problem: str,
    how_to_fix: str,
    *,
    feature_name: Optional[str] = None,
    is_mandatory: bool = False,
) -> Callable[[C], C]:

    def _wrapper(func: C) -> C:
        LSP_CHECKS.append(
            LSPSelfCheck(
                _feature_name(feature_name, func),
                func,
                problem,
                how_to_fix,
                is_mandatory=is_mandatory,
            )
        )
        return func

    return _wrapper


def _feature_name(feature: Optional[str], func: Callable[[], None]) -> str:
    if feature is not None:
        return feature
    return func.__name__.replace("_", " ")


@lsp_import_check(["python3-lsprotocol", "python3-pygls"], is_mandatory=True)
def minimum_requirements() -> bool:
    import pygls.server

    # The hasattr is irrelevant; but it avoids the import being flagged as redundant.
    return hasattr(pygls.server, "LanguageServer")


@lsp_import_check(["python3-levenshtein"])
def typo_detection() -> bool:
    import Levenshtein

    # The hasattr is irrelevant; but it avoids the import being flagged as redundant.
    return hasattr(Levenshtein, "distance")


@lsp_import_check(["hunspell-en-us", "python3-hunspell"])
def spell_checking() -> bool:
    import Levenshtein

    # The hasattr is irrelevant; but it avoids the import being flagged as redundant.
    return hasattr(Levenshtein, "distance") and os.path.exists(
        "/usr/share/hunspell/en_US.dic"
    )


@lsp_generic_check(
    feature_name="extra dh support",
    problem="Missing dependencies",
    how_to_fix="Run `apt satisfy debhelper (>= 13.16~)` to enable this feature",
)
def check_dh_version() -> bool:
    try:
        output = subprocess.check_output(
            [
                "dpkg-query",
                "-W",
                "--showformat=${Version} ${db:Status-Status}\n",
                "debhelper",
            ]
        ).decode("utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    else:
        parts = output.split()
        if len(parts) != 2:
            return False
        if parts[1] != "installed":
            return False
        return Version(parts[0]) >= Version("13.16~")


@lsp_generic_check(
    feature_name="apt cache packages",
    problem="Missing apt or empty apt cache",
    how_to_fix="",
)
def check_apt_cache() -> bool:
    try:
        output = subprocess.check_output(
            [
                "apt-get",
                "indextargets",
                "--format",
                "$(IDENTIFIER)",
            ]
        ).decode("utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    for line in output.splitlines():
        if line.strip() == "Packages":
            return True

    return False


def assert_can_start_lsp() -> None:
    for self_check in LSP_CHECKS:
        if self_check.is_mandatory and not self_check.test():
            _error(
                f"Cannot start the language server. {self_check.problem}. {self_check.how_to_fix}"
            )
