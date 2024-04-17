import dataclasses
from typing import Sequence, Optional, List, Literal, Iterable, Dict, Self

from debputy.manifest_parser.base_types import DebputyDispatchableType
from debputy.manifest_parser.util import AttributePath

STD_CONTROL_SCRIPTS = frozenset(
    {
        "preinst",
        "prerm",
        "postinst",
        "postrm",
    }
)
UDEB_CONTROL_SCRIPTS = frozenset(
    {
        "postinst",
        "menutest",
        "isinstallable",
    }
)
ALL_CONTROL_SCRIPTS = STD_CONTROL_SCRIPTS | UDEB_CONTROL_SCRIPTS | {"config"}


@dataclasses.dataclass(slots=True, frozen=True)
class MaintscriptSnippet:
    definition_source: str
    snippet: str
    snippet_order: Optional[Literal["service"]] = None

    def script_content(self) -> str:
        lines = [
            f"# Snippet source: {self.definition_source}\n",
            self.snippet,
        ]
        if not self.snippet.endswith("\n"):
            lines.append("\n")
        return "".join(lines)


class MaintscriptSnippetContainer:
    def __init__(self) -> None:
        self._generic_snippets: List[MaintscriptSnippet] = []
        self._snippets_by_order: Dict[Literal["service"], List[MaintscriptSnippet]] = {}

    def copy(self) -> "MaintscriptSnippetContainer":
        instance = self.__class__()
        instance._generic_snippets = self._generic_snippets.copy()
        instance._snippets_by_order = self._snippets_by_order.copy()
        return instance

    def append(self, maintscript_snippet: MaintscriptSnippet) -> None:
        if maintscript_snippet.snippet_order is None:
            self._generic_snippets.append(maintscript_snippet)
        else:
            if maintscript_snippet.snippet_order not in self._snippets_by_order:
                self._snippets_by_order[maintscript_snippet.snippet_order] = []
            self._snippets_by_order[maintscript_snippet.snippet_order].append(
                maintscript_snippet
            )

    def has_content(self, snippet_order: Optional[Literal["service"]] = None) -> bool:
        if snippet_order is None:
            return bool(self._generic_snippets)
        if snippet_order not in self._snippets_by_order:
            return False
        return bool(self._snippets_by_order[snippet_order])

    def all_snippets(self) -> Iterable[MaintscriptSnippet]:
        yield from self._generic_snippets
        for snippets in self._snippets_by_order.values():
            yield from snippets

    def generate_snippet(
        self,
        tool_with_version: Optional[str] = None,
        snippet_order: Optional[Literal["service"]] = None,
        reverse: bool = False,
    ) -> Optional[str]:
        inner_content = ""
        if snippet_order is None:
            snippets = (
                reversed(self._generic_snippets) if reverse else self._generic_snippets
            )
            inner_content = "".join(s.script_content() for s in snippets)
        elif snippet_order in self._snippets_by_order:
            snippets = self._snippets_by_order[snippet_order]
            if reverse:
                snippets = reversed(snippets)
            inner_content = "".join(s.script_content() for s in snippets)

        if not inner_content:
            return None

        if tool_with_version:
            return (
                f"# Automatically added by {tool_with_version}\n"
                + inner_content
                + "# End automatically added section"
            )
        return inner_content


class DpkgMaintscriptHelperCommand(DebputyDispatchableType):
    __slots__ = ("cmdline", "definition_source")

    def __init__(self, cmdline: Sequence[str], definition_source: str):
        self.cmdline = cmdline
        self.definition_source = definition_source

    @classmethod
    def _finish_cmd(
        cls,
        definition_source: str,
        cmdline: List[str],
        prior_version: Optional[str],
        owning_package: Optional[str],
    ) -> Self:
        if prior_version is not None:
            cmdline.append(prior_version)
        if owning_package is not None:
            if prior_version is None:
                # Empty is allowed according to `man dpkg-maintscript-helper`
                cmdline.append("")
            cmdline.append(owning_package)
        return cls(
            tuple(cmdline),
            definition_source,
        )

    @classmethod
    def rm_conffile(
        cls,
        definition_source: AttributePath,
        conffile: str,
        prior_version: Optional[str] = None,
        owning_package: Optional[str] = None,
    ) -> Self:
        cmdline = ["rm_conffile", conffile]
        return cls._finish_cmd(
            definition_source.path, cmdline, prior_version, owning_package
        )

    @classmethod
    def mv_conffile(
        cls,
        definition_source: AttributePath,
        old_conffile: str,
        new_confile: str,
        prior_version: Optional[str] = None,
        owning_package: Optional[str] = None,
    ) -> Self:
        cmdline = ["mv_conffile", old_conffile, new_confile]
        return cls._finish_cmd(
            definition_source.path, cmdline, prior_version, owning_package
        )

    @classmethod
    def symlink_to_dir(
        cls,
        definition_source: AttributePath,
        pathname: str,
        old_target: str,
        prior_version: Optional[str] = None,
        owning_package: Optional[str] = None,
    ) -> Self:
        cmdline = ["symlink_to_dir", pathname, old_target]
        return cls._finish_cmd(
            definition_source.path, cmdline, prior_version, owning_package
        )

    @classmethod
    def dir_to_symlink(
        cls,
        definition_source: AttributePath,
        pathname: str,
        new_target: str,
        prior_version: Optional[str] = None,
        owning_package: Optional[str] = None,
    ) -> Self:
        cmdline = ["dir_to_symlink", pathname, new_target]
        return cls._finish_cmd(
            definition_source.path, cmdline, prior_version, owning_package
        )
