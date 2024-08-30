import dataclasses
import os
import time
from typing import (
    Optional,
    List,
    Any,
    Mapping,
    Container,
    TYPE_CHECKING,
    Tuple,
    Literal,
    Set,
)

from debputy.dh.dh_assistant import (
    parse_drules_for_addons,
    DhSequencerData,
    extract_dh_addons_from_control,
)
from debputy.filesystem_scan import FSROOverlay, VirtualPathBase
from debputy.linting.lint_util import (
    LintState,
)
from debputy.lsp.apt_cache import AptCache
from debputy.lsp.maint_prefs import (
    MaintainerPreferenceTable,
    MaintainerPreference,
    determine_effective_preference,
)
from debputy.lsp.text_util import LintCapablePositionCodec
from debputy.lsp.vendoring._deb822_repro import Deb822FileElement, parse_deb822_file
from debputy.lsprotocol.types import MarkupKind
from debputy.packages import (
    SourcePackage,
    BinaryPackage,
    DctrlParser,
)
from debputy.plugin.api.feature_set import PluginProvidedFeatureSet
from debputy.util import _info
from debputy.yaml import MANIFEST_YAML, YAMLError
from debputy.yaml.compat import CommentedMap

if TYPE_CHECKING:
    from pygls.server import LanguageServer
    from pygls.workspace import TextDocument
    from pygls.uris import from_fs_path

else:
    try:
        from pygls.server import LanguageServer
        from pygls.workspace import TextDocument
        from pygls.uris import from_fs_path
    except ImportError as e:

        class LanguageServer:
            def __init__(self, *args, **kwargs) -> None:
                """Placeholder to work if pygls is not installed"""
                # Should not be called
                global e
                raise e  # pragma: no cover


@dataclasses.dataclass(slots=True)
class FileCache:
    doc_uri: str
    path: str
    is_open_in_editor: Optional[bool] = None
    last_doc_version: Optional[int] = None
    last_mtime: Optional[float] = None
    is_valid: bool = False

    def _update_cache(self, doc: "TextDocument", source: str) -> None:
        raise NotImplementedError

    def _clear_cache(self) -> None:
        raise NotImplementedError

    def resolve_cache(self, ls: "DebputyLanguageServer") -> bool:
        doc = ls.workspace.text_documents.get(self.doc_uri)
        if doc is None:
            doc = ls.workspace.get_text_document(self.doc_uri)
            is_open = False
        else:
            is_open = True
        new_content: Optional[str] = None
        if is_open:
            last_doc_version = self.last_doc_version
            dctrl_doc_version = doc.version
            if (
                not self.is_open_in_editor
                or last_doc_version is None
                or dctrl_doc_version is None
                or last_doc_version < dctrl_doc_version
            ):
                new_content = doc.source

            self.last_doc_version = doc.version
        elif doc.uri.startswith("file://"):
            try:
                with open(self.path) as fd:
                    st = os.fstat(fd.fileno())
                    current_mtime = st.st_mtime
                    last_mtime = self.last_mtime or current_mtime - 1
                    if self.is_open_in_editor or current_mtime > last_mtime:
                        new_content = fd.read()
                    self.last_mtime = current_mtime
            except FileNotFoundError:
                self._clear_cache()
                self.is_valid = False
                return False
        if new_content is not None:
            self._update_cache(doc, new_content)
        self.is_valid = True
        return True


@dataclasses.dataclass(slots=True)
class Deb822FileCache(FileCache):
    deb822_file: Optional[Deb822FileElement] = None

    def _update_cache(self, doc: "TextDocument", source: str) -> None:
        deb822_file = parse_deb822_file(
            source.splitlines(keepends=True),
            accept_files_with_error_tokens=True,
            accept_files_with_duplicated_fields=True,
        )
        self.deb822_file = deb822_file

    def _clear_cache(self) -> None:
        self.deb822_file = None


@dataclasses.dataclass(slots=True)
class DctrlFileCache(Deb822FileCache):
    dctrl_parser: Optional[DctrlParser] = None
    source_package: Optional[SourcePackage] = None
    binary_packages: Optional[Mapping[str, BinaryPackage]] = None

    def _update_cache(self, doc: "TextDocument", source: str) -> None:
        deb822_file, source_package, binary_packages = (
            self.dctrl_parser.parse_source_debian_control(
                source.splitlines(keepends=True),
                ignore_errors=True,
            )
        )
        self.deb822_file = deb822_file
        self.source_package = source_package
        self.binary_packages = binary_packages

    def _clear_cache(self) -> None:
        super()._clear_cache()
        self.source_package = None
        self.binary_packages = None


@dataclasses.dataclass(slots=True)
class SalsaCICache(FileCache):
    parsed_content: Optional[CommentedMap] = None

    def _update_cache(self, doc: "TextDocument", source: str) -> None:
        try:
            value = MANIFEST_YAML.load(source)
            if isinstance(value, CommentedMap):
                self.parsed_content = value
        except YAMLError:
            pass

    def _clear_cache(self) -> None:
        self.parsed_content = None


@dataclasses.dataclass(slots=True)
class DebianRulesCache(FileCache):
    sequences: Optional[Set[str]] = None
    saw_dh: bool = False

    def _update_cache(self, doc: "TextDocument", source: str) -> None:
        sequences = set()
        self.saw_dh = parse_drules_for_addons(
            source.splitlines(),
            sequences,
        )
        self.sequences = sequences

    def _clear_cache(self) -> None:
        self.sequences = None
        self.saw_dh = False


class LSProvidedLintState(LintState):
    def __init__(
        self,
        ls: "DebputyLanguageServer",
        doc: "TextDocument",
        source_root: str,
        debian_dir_path: str,
        dctrl_parser: DctrlParser,
    ) -> None:
        self._ls = ls
        self._doc = doc
        # Cache lines (doc.lines re-splits everytime)
        self._lines = doc.lines
        self._source_root = FSROOverlay.create_root_dir(".", source_root)
        debian_dir = self._source_root.get("debian")
        if debian_dir is not None and not debian_dir.is_dir:
            debian_dir = None
        self._debian_dir = debian_dir
        dctrl_file = os.path.join(debian_dir_path, "control")

        if dctrl_file != doc.path:
            self._dctrl_cache: DctrlFileCache = DctrlFileCache(
                from_fs_path(dctrl_file),
                dctrl_file,
                dctrl_parser=dctrl_parser,
            )
            self._deb822_file: Deb822FileCache = Deb822FileCache(
                doc.uri,
                doc.path,
            )
        else:
            self._dctrl_cache: DctrlFileCache = DctrlFileCache(
                doc.uri,
                doc.path,
                dctrl_parser=dctrl_parser,
            )
            self._deb822_file = self._dctrl_cache

        self._salsa_ci_caches = [
            SalsaCICache(
                from_fs_path(os.path.join(debian_dir_path, p)),
                os.path.join(debian_dir_path, p),
            )
            for p in ("salsa-ci.yml", os.path.join("..", ".gitlab-ci.yml"))
        ]
        drules_path = os.path.join(debian_dir_path, "rules")
        self._drules_cache = DebianRulesCache(
            from_fs_path(drules_path) if doc.path != drules_path else doc.uri,
            drules_path,
        )

    @property
    def plugin_feature_set(self) -> PluginProvidedFeatureSet:
        return self._ls.plugin_feature_set

    @property
    def doc_uri(self) -> str:
        return self._doc.uri

    @property
    def source_root(self) -> Optional[VirtualPathBase]:
        return self._source_root

    @property
    def debian_dir(self) -> Optional[VirtualPathBase]:
        return self._debian_dir

    @property
    def path(self) -> str:
        return self._doc.path

    @property
    def content(self) -> str:
        return self._doc.source

    @property
    def lines(self) -> List[str]:
        return self._lines

    @property
    def position_codec(self) -> LintCapablePositionCodec:
        return self._doc.position_codec

    def _resolve_dctrl(self) -> Optional[DctrlFileCache]:
        dctrl_cache = self._dctrl_cache
        dctrl_cache.resolve_cache(self._ls)
        return dctrl_cache

    @property
    def parsed_deb822_file_content(self) -> Optional[Deb822FileElement]:
        cache = self._deb822_file
        cache.resolve_cache(self._ls)
        return cache.deb822_file

    @property
    def source_package(self) -> Optional[SourcePackage]:
        return self._resolve_dctrl().source_package

    @property
    def binary_packages(self) -> Optional[Mapping[str, BinaryPackage]]:
        return self._resolve_dctrl().binary_packages

    def _resolve_salsa_ci(self) -> Optional[CommentedMap]:
        for salsa_ci_cache in self._salsa_ci_caches:
            if salsa_ci_cache.resolve_cache(self._ls):
                return salsa_ci_cache.parsed_content
        return None

    @property
    def effective_preference(self) -> Optional[MaintainerPreference]:
        source_package = self.source_package
        salsa_ci = self._resolve_salsa_ci()
        if source_package is None and salsa_ci is None:
            return None
        style, _, _ = determine_effective_preference(
            self.maint_preference_table,
            source_package,
            salsa_ci,
        )
        return style

    @property
    def maint_preference_table(self) -> MaintainerPreferenceTable:
        return self._ls.maint_preferences

    @property
    def salsa_ci(self) -> Optional[CommentedMap]:
        return None

    @property
    def dh_sequencer_data(self) -> DhSequencerData:
        dh_sequences: Set[str] = set()
        saw_dh = False
        src_pkg = self.source_package
        drules_cache = self._drules_cache
        if drules_cache.resolve_cache(self._ls):
            saw_dh = drules_cache.saw_dh
            if drules_cache.sequences:
                dh_sequences.update(drules_cache.sequences)
        if src_pkg:
            extract_dh_addons_from_control(src_pkg.fields, dh_sequences)

        return DhSequencerData(
            frozenset(dh_sequences),
            saw_dh,
        )


def _preference(
    client_preference: Optional[List[MarkupKind]],
    options: Container[MarkupKind],
    fallback_kind: MarkupKind,
) -> MarkupKind:
    if not client_preference:
        return fallback_kind
    for markdown_kind in client_preference:
        if markdown_kind in options:
            return markdown_kind
    return fallback_kind


class DebputyLanguageServer(LanguageServer):

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._dctrl_parser: Optional[DctrlParser] = None
        self._plugin_feature_set: Optional[PluginProvidedFeatureSet] = None
        self._trust_language_ids: Optional[bool] = None
        self._finished_initialization = False
        self.maint_preferences = MaintainerPreferenceTable({}, {})
        self.apt_cache = AptCache()
        self.background_tasks = set()

    def finish_startup_initialization(self) -> None:
        if self._finished_initialization:
            return
        assert self._dctrl_parser is not None
        assert self._plugin_feature_set is not None
        assert self._trust_language_ids is not None
        self.maint_preferences = self.maint_preferences.load_preferences()
        _info(
            f"Loaded style preferences: {len(self.maint_preferences.maintainer_preferences)} unique maintainer preferences recorded"
        )
        self._finished_initialization = True

    async def on_initialize(self) -> None:
        task = self.loop.create_task(self._load_apt_cache(), name="Index apt cache")
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    def shutdown(self) -> None:
        for task in self.background_tasks:
            _info(f"Cancelling task: {task.get_name()}")
            self.loop.call_soon_threadsafe(task.cancel)
        return super().shutdown()

    async def _load_apt_cache(self) -> None:
        _info("Starting load of apt cache data")
        start = time.time()
        await self.apt_cache.load()
        end = time.time()
        _info(
            f"Loading apt cache finished after {end-start} seconds and is now in state {self.apt_cache.state}"
        )

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

        source_root = os.path.dirname(dir_path)

        return LSProvidedLintState(self, doc, source_root, dir_path, self.dctrl_parser)

    @property
    def _client_hover_markup_formats(self) -> Optional[List[MarkupKind]]:
        try:
            return (
                self.client_capabilities.text_document.hover.content_format
            )  # type : ignore
        except AttributeError:
            return None

    def hover_markup_format(
        self,
        *options: MarkupKind,
        fallback_kind: MarkupKind = MarkupKind.PlainText,
    ) -> MarkupKind:
        """Pick the client preferred hover markup format from a set of options

        :param options: The markup kinds possible.
        :param fallback_kind: If no overlapping option was found in the client preferences
          (or client did not announce a value at all), this parameter is returned instead.
        :returns: The client's preferred markup format from the provided options, or,
          (if there is no overlap), the `fallback_kind` value is returned.
        """
        client_preference = self._client_hover_markup_formats
        return _preference(client_preference, frozenset(options), fallback_kind)

    @property
    def _client_completion_item_document_markup_formats(
        self,
    ) -> Optional[List[MarkupKind]]:
        try:
            return (
                self.client_capabilities.text_document.completion.completion_item.documentation_format  # type : ignore
            )
        except AttributeError:
            return None

    def completion_item_document_markup(
        self,
        *options: MarkupKind,
        fallback_kind: MarkupKind = MarkupKind.PlainText,
    ) -> MarkupKind:
        """Pick the client preferred completion item documentation markup format from a set of options

        :param options: The markup kinds possible.
        :param fallback_kind: If no overlapping option was found in the client preferences
          (or client did not announce a value at all), this parameter is returned instead.
        :returns: The client's preferred markup format from the provided options, or,
          (if there is no overlap), the `fallback_kind` value is returned.
        """

        client_preference = self._client_completion_item_document_markup_formats
        return _preference(client_preference, frozenset(options), fallback_kind)

    @property
    def trust_language_ids(self) -> bool:
        v = self._trust_language_ids
        if v is None:
            return True
        return v

    @trust_language_ids.setter
    def trust_language_ids(self, new_value: bool) -> None:
        self._trust_language_ids = new_value

    def determine_language_id(
        self,
        doc: "TextDocument",
    ) -> Tuple[Literal["editor-provided", "filename"], str, str]:
        lang_id = doc.language_id
        path = doc.path
        try:
            last_idx = path.rindex("debian/")
        except ValueError:
            cleaned_filename = os.path.basename(path)
        else:
            cleaned_filename = path[last_idx:]

        if self.trust_language_ids and lang_id and not lang_id.isspace():
            if lang_id not in ("fundamental",):
                return "editor-provided", lang_id, cleaned_filename
            _info(
                f"Ignoring editor provided language ID: {lang_id} (reverting to filename based detection instead)"
            )

        return "filename", cleaned_filename, cleaned_filename
