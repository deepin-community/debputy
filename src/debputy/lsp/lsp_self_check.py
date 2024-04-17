import dataclasses
import os.path
from typing import Callable, Sequence, List, Optional, TypeVar

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
):

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


def assert_can_start_lsp():
    for self_check in LSP_CHECKS:
        if self_check.is_mandatory and not self_check.test():
            _error(
                f"Cannot start the language server. {self_check.problem}. {self_check.how_to_fix}"
            )
