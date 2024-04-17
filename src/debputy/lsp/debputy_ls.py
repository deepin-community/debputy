import dataclasses
import os
from typing import Optional, List, Any, Mapping

from debputy.linting.lint_util import LintState
from debputy.lsp.text_util import LintCapablePositionCodec
from debputy.packages import (
    SourcePackage,
    BinaryPackage,
    DctrlParser,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet

try:
    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
    from pygls.uris import from_fs_path
except ImportError as e:

    class LanguageServer:
        def __init__(self, *args, **kwargs) -> None:
            """Placeholder to work if pygls is not installed"""
            # Should not be called
            raise e  # pragma: no cover


@dataclasses.dataclass(slots=True)
class DctrlCache:
    doc_uri: str
    path: str
    is_open_in_editor: Optional[bool]
    last_doc_version: Optional[int]
    last_mtime: Optional[float]
    source_package: Optional[SourcePackage]
    binary_packages: Optional[Mapping[str, BinaryPackage]]


class LSProvidedLintState(LintState):
    def __init__(
        self,
        ls: "DebputyLanguageServer",
        doc: "TextDocument",
        debian_dir_path: str,
        dctrl_parser: DctrlParser,
    ) -> None:
        self._ls = ls
        self._doc = doc
        # Cache lines (doc.lines re-splits everytime)
        self._lines = doc.lines
        self._dctrl_parser = dctrl_parser
        dctrl_file = os.path.join(debian_dir_path, "control")
        self._dctrl_cache: DctrlCache = DctrlCache(
            from_fs_path(dctrl_file),
            dctrl_file,
            is_open_in_editor=None,  # Unresolved
            last_doc_version=None,
            last_mtime=None,
            source_package=None,
            binary_packages=None,
        )

    @property
    def plugin_feature_set(self) -> PluginProvidedFeatureSet:
        return self._ls.plugin_feature_set

    @property
    def doc_uri(self) -> str:
        return self._doc.uri

    @property
    def path(self) -> str:
        return self._doc.path

    @property
    def lines(self) -> List[str]:
        return self._lines

    @property
    def position_codec(self) -> LintCapablePositionCodec:
        return self._doc.position_codec

    def _resolve_dctrl(self) -> Optional[DctrlCache]:
        dctrl_cache = self._dctrl_cache
        doc = self._ls.workspace.text_documents.get(dctrl_cache.doc_uri)
        is_open = doc is not None
        dctrl_doc = self._ls.workspace.get_text_document(dctrl_cache.doc_uri)
        re_parse_lines: Optional[List[str]] = None
        if is_open:
            if (
                not dctrl_cache.is_open_in_editor
                or dctrl_cache.last_doc_version is None
                or dctrl_cache.last_doc_version < dctrl_doc.version
            ):
                re_parse_lines = doc.lines

            dctrl_cache.last_doc_version = dctrl_doc.version
        elif self._doc.uri.startswith("file://"):
            try:
                with open(dctrl_cache.path) as fd:
                    st = os.fstat(fd.fileno())
                    current_mtime = st.st_mtime
                    last_mtime = dctrl_cache.last_mtime or current_mtime - 1
                    if dctrl_cache.is_open_in_editor or current_mtime > last_mtime:
                        re_parse_lines = list(fd)
                    dctrl_cache.last_mtime = current_mtime
            except FileNotFoundError:
                return None
        if re_parse_lines is not None:
            source_package, binary_packages = (
                self._dctrl_parser.parse_source_debian_control(
                    re_parse_lines,
                    ignore_errors=True,
                )
            )
            dctrl_cache.source_package = source_package
            dctrl_cache.binary_packages = binary_packages
        return dctrl_cache

    @property
    def source_package(self) -> Optional[SourcePackage]:
        dctrl = self._resolve_dctrl()
        return dctrl.source_package if dctrl is not None else None

    @property
    def binary_packages(self) -> Optional[Mapping[str, BinaryPackage]]:
        dctrl = self._resolve_dctrl()
        return dctrl.binary_packages if dctrl is not None else None


class DebputyLanguageServer(LanguageServer):

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._dctrl_parser: Optional[DctrlParser] = None
        self._plugin_feature_set: Optional[PluginProvidedFeatureSet] = None

    @property
    def plugin_feature_set(self) -> PluginProvidedFeatureSet:
        res = self._plugin_feature_set
        if res is None:
            raise RuntimeError(
                "Initialization error: The plugin feature set has not been initialized before it was needed."
            )
        return res

    @plugin_feature_set.setter
    def plugin_feature_set(self, plugin_feature_set: PluginProvidedFeatureSet) -> None:
        if self._plugin_feature_set is not None:
            raise RuntimeError(
                "The plugin_feature_set attribute cannot be changed once set"
            )
        self._plugin_feature_set = plugin_feature_set

    @property
    def dctrl_parser(self) -> DctrlParser:
        res = self._dctrl_parser
        if res is None:
            raise RuntimeError(
                "Initialization error: The dctrl_parser has not been initialized before it was needed."
            )
        return res

    @dctrl_parser.setter
    def dctrl_parser(self, parser: DctrlParser) -> None:
        if self._dctrl_parser is not None:
            raise RuntimeError("The dctrl_parser attribute cannot be changed once set")
        self._dctrl_parser = parser

    def lint_state(self, doc: "TextDocument") -> LintState:
        dir_path = os.path.dirname(doc.path)

        while dir_path and dir_path != "/" and os.path.basename(dir_path) != "debian":
            dir_path = os.path.dirname(dir_path)

        return LSProvidedLintState(self, doc, dir_path, self.dctrl_parser)
