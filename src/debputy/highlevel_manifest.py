import dataclasses
import functools
import os
import textwrap
from contextlib import suppress
from dataclasses import dataclass, field
from typing import (
    List,
    Dict,
    Iterable,
    Mapping,
    Any,
    Union,
    Optional,
    TypeVar,
    Generic,
    cast,
    Set,
    Tuple,
    Sequence,
    FrozenSet,
)

from debian.debian_support import DpkgArchTable
from ._deb_options_profiles import DebBuildOptionsAndProfiles
from ._manifest_constants import *
from .architecture_support import DpkgArchitectureBuildProcessValuesTable
from .builtin_manifest_rules import builtin_mode_normalization_rules
from debputy.dh.debhelper_emulation import (
    dhe_dbgsym_root_dir,
    assert_no_dbgsym_migration,
    read_dbgsym_file,
)
from .exceptions import DebputySubstitutionError
from .filesystem_scan import FSPath, FSRootDir, FSROOverlay
from .installations import (
    InstallRule,
    SourcePathMatcher,
    PathAlreadyInstalledOrDiscardedError,
    NoMatchForInstallPatternError,
    InstallRuleContext,
    BinaryPackageInstallRuleContext,
    InstallSearchDirContext,
    SearchDir,
)
from .intermediate_manifest import TarMember, PathType, IntermediateManifest
from .maintscript_snippet import (
    DpkgMaintscriptHelperCommand,
    MaintscriptSnippetContainer,
)
from .manifest_conditions import ConditionContext
from .manifest_parser.base_types import (
    FileSystemMatchRule,
    FileSystemExactMatchRule,
    BuildEnvironments,
)
from .manifest_parser.util import AttributePath
from .packager_provided_files import PackagerProvidedFile
from .packages import BinaryPackage, SourcePackage
from .plugin.api.feature_set import PluginProvidedFeatureSet
from .plugin.api.impl import BinaryCtrlAccessorProviderCreator
from .plugin.api.impl_types import (
    PackageProcessingContextProvider,
    PackageDataTable,
)
from .plugin.api.spec import (
    FlushableSubstvars,
    VirtualPath,
    DebputyIntegrationMode,
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
)
from .plugin.debputy.binary_package_rules import ServiceRule
from .plugin.debputy.to_be_api_types import BuildRule
from .plugin.plugin_state import run_in_context_of_plugin
from .substitution import Substitution
from .transformation_rules import (
    TransformationRule,
    ModeNormalizationTransformationRule,
    NormalizeShebangLineTransformation,
)
from .util import (
    _error,
    _warn,
    debian_policy_normalize_symlink_target,
    generated_content_dir,
    _info,
)
from .yaml import MANIFEST_YAML
from .yaml.compat import CommentedMap, CommentedSeq


@dataclass(slots=True)
class DbgsymInfo:
    dbgsym_fs_root: FSPath
    dbgsym_ids: List[str]


@dataclass(slots=True, frozen=True)
class BinaryPackageData:
    source_package: SourcePackage
    binary_package: BinaryPackage
    binary_staging_root_dir: str
    control_output_dir: Optional[str]
    fs_root: FSPath
    substvars: FlushableSubstvars
    package_metadata_context: PackageProcessingContextProvider
    ctrl_creator: BinaryCtrlAccessorProviderCreator
    dbgsym_info: DbgsymInfo


@dataclass(slots=True)
class PackageTransformationDefinition:
    binary_package: BinaryPackage
    substitution: Substitution
    is_auto_generated_package: bool
    binary_version: Optional[str] = None
    search_dirs: Optional[List[FileSystemExactMatchRule]] = None
    dpkg_maintscript_helper_snippets: List[DpkgMaintscriptHelperCommand] = field(
        default_factory=list
    )
    maintscript_snippets: Dict[str, MaintscriptSnippetContainer] = field(
        default_factory=dict
    )
    transformations: List[TransformationRule] = field(default_factory=list)
    reserved_packager_provided_files: Dict[str, List[PackagerProvidedFile]] = field(
        default_factory=dict
    )
    install_rules: List[InstallRule] = field(default_factory=list)
    requested_service_rules: List[ServiceRule] = field(default_factory=list)


def _path_to_tar_member(
    path: FSPath,
    clamp_mtime_to: int,
) -> TarMember:
    mtime = float(clamp_mtime_to)
    owner, uid, group, gid = path.tar_owner_info
    mode = path.mode

    if path.has_fs_path:
        mtime = min(mtime, path.mtime)

    if path.is_dir:
        path_type = PathType.DIRECTORY
    elif path.is_file:
        # TODO: someday we will need to deal with hardlinks and it might appear here.
        path_type = PathType.FILE
    elif path.is_symlink:
        # Special-case that we resolve immediately (since we need to normalize the target anyway)
        link_target = debian_policy_normalize_symlink_target(
            path.path,
            path.readlink(),
        )
        return TarMember.virtual_path(
            path.tar_path,
            PathType.SYMLINK,
            mtime,
            link_target=link_target,
            # Force mode to be 0777 as that is the mode we see in the data.tar.  In theory, tar lets you set
            # it to whatever. However, for reproducibility, we have to be well-behaved - and that is 0777.
            mode=0o0777,
            owner=owner,
            uid=uid,
            group=group,
            gid=gid,
        )
    else:
        assert not path.is_symlink
        raise AssertionError(
            f"Unsupported file type: {path.path}  - not a file, dir nor a symlink!"
        )

    if not path.has_fs_path:
        assert not path.is_file
        return TarMember.virtual_path(
            path.tar_path,
            path_type,
            mtime,
            mode=mode,
            owner=owner,
            uid=uid,
            group=group,
            gid=gid,
        )
    may_steal_fs_path = path._can_replace_inline
    return TarMember.from_file(
        path.tar_path,
        path.fs_path,
        mode=mode,
        uid=uid,
        owner=owner,
        gid=gid,
        group=group,
        path_type=path_type,
        path_mtime=mtime,
        clamp_mtime_to=clamp_mtime_to,
        may_steal_fs_path=may_steal_fs_path,
    )


def _generate_intermediate_manifest(
    fs_root: FSPath,
    clamp_mtime_to: int,
) -> Iterable[TarMember]:
    symlinks = []
    for path in fs_root.all_paths():
        tar_member = _path_to_tar_member(path, clamp_mtime_to)
        if tar_member.path_type == PathType.SYMLINK:
            symlinks.append(tar_member)
            continue
        yield tar_member
    yield from symlinks


ST = TypeVar("ST")
T = TypeVar("T")


class AbstractYAMLSubStore(Generic[ST]):
    def __init__(
        self,
        parent_store: Any,
        parent_key: Optional[Union[int, str]],
        store: Optional[ST] = None,
    ) -> None:
        if parent_store is not None and parent_key is not None:
            try:
                from_parent_store = parent_store[parent_key]
            except (KeyError, IndexError):
                from_parent_store = None
            if (
                store is not None
                and from_parent_store is not None
                and store is not parent_store
            ):
                raise ValueError(
                    "Store is provided but is not the one already in the parent store"
                )
            if store is None:
                store = from_parent_store
        self._parent_store = parent_store
        self._parent_key = parent_key
        self._is_detached = (
            parent_key is None or parent_store is None or parent_key not in parent_store
        )
        assert self._is_detached or store is not None
        if store is None:
            store = self._create_new_instance()
        self._store: ST = store

    def _create_new_instance(self) -> ST:
        raise NotImplementedError

    def create_definition_if_missing(self) -> None:
        if self._is_detached:
            self.create_definition()

    def create_definition(self) -> None:
        if not self._is_detached:
            raise RuntimeError("Definition is already present")
        parent_store = self._parent_store
        if parent_store is None:
            raise RuntimeError(
                f"Definition is not attached to any parent!? ({self.__class__.__name__})"
            )
        if isinstance(parent_store, list):
            assert self._parent_key is None
            self._parent_key = len(parent_store)
            self._parent_store.append(self._store)
        else:
            parent_store[self._parent_key] = self._store
        self._is_detached = False

    def remove_definition(self) -> None:
        self._ensure_attached()
        del self._parent_store[self._parent_key]
        if isinstance(self._parent_store, list):
            self._parent_key = None
        self._is_detached = True

    def _ensure_attached(self) -> None:
        if self._is_detached:
            raise RuntimeError("The definition has been removed!")


class AbstractYAMLListSubStore(Generic[T], AbstractYAMLSubStore[List[T]]):
    def _create_new_instance(self) -> List[T]:
        return CommentedSeq()


class AbstractYAMLDictSubStore(Generic[T], AbstractYAMLSubStore[Dict[str, T]]):
    def _create_new_instance(self) -> Dict[str, T]:
        return CommentedMap()


class MutableCondition:
    @classmethod
    def arch_matches(cls, arch_filter: str) -> CommentedMap:
        return CommentedMap({MK_CONDITION_ARCH_MATCHES: arch_filter})

    @classmethod
    def build_profiles_matches(cls, build_profiles_matches: str) -> CommentedMap:
        return CommentedMap(
            {MK_CONDITION_BUILD_PROFILES_MATCHES: build_profiles_matches}
        )


class MutableYAMLSymlink(AbstractYAMLDictSubStore[Any]):
    @classmethod
    def new_symlink(
        cls, link_path: str, link_target: str, condition: Optional[Any]
    ) -> "MutableYAMLSymlink":
        inner = {
            MK_TRANSFORMATIONS_CREATE_SYMLINK_LINK_PATH: link_path,
            MK_TRANSFORMATIONS_CREATE_SYMLINK_LINK_TARGET: link_target,
        }
        content = {MK_TRANSFORMATIONS_CREATE_SYMLINK: inner}
        if condition is not None:
            inner["when"] = condition
        return cls(None, None, store=CommentedMap(content))

    @property
    def symlink_path(self) -> str:
        return self._store[MK_TRANSFORMATIONS_CREATE_SYMLINK][
            MK_TRANSFORMATIONS_CREATE_SYMLINK_LINK_PATH
        ]

    @symlink_path.setter
    def symlink_path(self, path: str) -> None:
        self._store[MK_TRANSFORMATIONS_CREATE_SYMLINK][
            MK_TRANSFORMATIONS_CREATE_SYMLINK_LINK_PATH
        ] = path

    @property
    def symlink_target(self) -> Optional[str]:
        return self._store[MK_TRANSFORMATIONS_CREATE_SYMLINK][
            MK_TRANSFORMATIONS_CREATE_SYMLINK_LINK_TARGET
        ]

    @symlink_target.setter
    def symlink_target(self, target: str) -> None:
        self._store[MK_TRANSFORMATIONS_CREATE_SYMLINK][
            MK_TRANSFORMATIONS_CREATE_SYMLINK_LINK_TARGET
        ] = target


class MutableYAMLConffileManagementItem(AbstractYAMLDictSubStore[Any]):
    @classmethod
    def rm_conffile(
        cls,
        conffile: str,
        prior_to_version: Optional[str],
        owning_package: Optional[str],
    ) -> "MutableYAMLConffileManagementItem":
        r = cls(
            None,
            None,
            store=CommentedMap(
                {
                    MK_CONFFILE_MANAGEMENT_REMOVE: CommentedMap(
                        {MK_CONFFILE_MANAGEMENT_REMOVE_PATH: conffile}
                    )
                }
            ),
        )
        r.prior_to_version = prior_to_version
        r.owning_package = owning_package
        return r

    @classmethod
    def mv_conffile(
        cls,
        old_conffile: str,
        new_conffile: str,
        prior_to_version: Optional[str],
        owning_package: Optional[str],
    ) -> "MutableYAMLConffileManagementItem":
        r = cls(
            None,
            None,
            store=CommentedMap(
                {
                    MK_CONFFILE_MANAGEMENT_RENAME: CommentedMap(
                        {
                            MK_CONFFILE_MANAGEMENT_RENAME_SOURCE: old_conffile,
                            MK_CONFFILE_MANAGEMENT_RENAME_TARGET: new_conffile,
                        }
                    )
                }
            ),
        )
        r.prior_to_version = prior_to_version
        r.owning_package = owning_package
        return r

    @property
    def _container(self) -> Dict[str, Any]:
        assert len(self._store) == 1
        return next(iter(self._store.values()))

    @property
    def command(self) -> str:
        assert len(self._store) == 1
        return next(iter(self._store))

    @property
    def obsolete_conffile(self) -> str:
        if self.command == MK_CONFFILE_MANAGEMENT_REMOVE:
            return self._container[MK_CONFFILE_MANAGEMENT_REMOVE_PATH]
        assert self.command == MK_CONFFILE_MANAGEMENT_RENAME
        return self._container[MK_CONFFILE_MANAGEMENT_RENAME_SOURCE]

    @obsolete_conffile.setter
    def obsolete_conffile(self, value: str) -> None:
        if self.command == MK_CONFFILE_MANAGEMENT_REMOVE:
            self._container[MK_CONFFILE_MANAGEMENT_REMOVE_PATH] = value
        else:
            assert self.command == MK_CONFFILE_MANAGEMENT_RENAME
            self._container[MK_CONFFILE_MANAGEMENT_RENAME_SOURCE] = value

    @property
    def new_conffile(self) -> str:
        if self.command != MK_CONFFILE_MANAGEMENT_RENAME:
            raise TypeError(
                f"The new_conffile attribute is only applicable to command {MK_CONFFILE_MANAGEMENT_RENAME}."
                f" This is a {self.command}"
            )
        return self._container[MK_CONFFILE_MANAGEMENT_RENAME_TARGET]

    @new_conffile.setter
    def new_conffile(self, value: str) -> None:
        if self.command != MK_CONFFILE_MANAGEMENT_RENAME:
            raise TypeError(
                f"The new_conffile attribute is only applicable to command {MK_CONFFILE_MANAGEMENT_RENAME}."
                f" This is a {self.command}"
            )
        self._container[MK_CONFFILE_MANAGEMENT_RENAME_TARGET] = value

    @property
    def prior_to_version(self) -> Optional[str]:
        return self._container.get(MK_CONFFILE_MANAGEMENT_X_PRIOR_TO_VERSION)

    @prior_to_version.setter
    def prior_to_version(self, value: Optional[str]) -> None:
        if value is None:
            try:
                del self._container[MK_CONFFILE_MANAGEMENT_X_PRIOR_TO_VERSION]
            except KeyError:
                pass
        else:
            self._container[MK_CONFFILE_MANAGEMENT_X_PRIOR_TO_VERSION] = value

    @property
    def owning_package(self) -> Optional[str]:
        return self._container[MK_CONFFILE_MANAGEMENT_X_PRIOR_TO_VERSION]

    @owning_package.setter
    def owning_package(self, value: Optional[str]) -> None:
        if value is None:
            try:
                del self._container[MK_CONFFILE_MANAGEMENT_X_OWNING_PACKAGE]
            except KeyError:
                pass
        else:
            self._container[MK_CONFFILE_MANAGEMENT_X_OWNING_PACKAGE] = value


class MutableYAMLPackageDefinition(AbstractYAMLDictSubStore):
    def _list_store(
        self, key, *, create_if_absent: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        if self._is_detached or key not in self._store:
            if create_if_absent:
                return None
            self.create_definition_if_missing()
            self._store[key] = []
        return self._store[key]

    def _insert_item(self, key: str, item: AbstractYAMLDictSubStore) -> None:
        parent_store = self._list_store(key, create_if_absent=True)
        assert parent_store is not None
        if not item._is_detached or (
            item._parent_store is not None and item._parent_store is not parent_store
        ):
            raise RuntimeError(
                "Item is already attached or associated with a different container"
            )
        item._parent_store = parent_store
        item.create_definition()

    def add_symlink(self, symlink: MutableYAMLSymlink) -> None:
        self._insert_item(MK_TRANSFORMATIONS, symlink)

    def symlinks(self) -> Iterable[MutableYAMLSymlink]:
        store = self._list_store(MK_TRANSFORMATIONS)
        if store is None:
            return
        for i in range(len(store)):
            d = store[i]
            if d and isinstance(d, dict) and len(d) == 1 and "symlink" in d:
                yield MutableYAMLSymlink(store, i)

    def conffile_management_items(self) -> Iterable[MutableYAMLConffileManagementItem]:
        store = self._list_store(MK_CONFFILE_MANAGEMENT)
        if store is None:
            return
        yield from (
            MutableYAMLConffileManagementItem(store, i) for i in range(len(store))
        )

    def add_conffile_management(
        self, conffile_management_item: MutableYAMLConffileManagementItem
    ) -> None:
        self._insert_item(MK_CONFFILE_MANAGEMENT, conffile_management_item)


class AbstractMutableYAMLInstallRule(AbstractYAMLDictSubStore):
    @property
    def _container(self) -> Dict[str, Any]:
        assert len(self._store) == 1
        return next(iter(self._store.values()))

    @property
    def into(self) -> Optional[List[str]]:
        v = self._container[MK_INSTALLATIONS_INSTALL_INTO]
        if v is None:
            return None
        if isinstance(v, str):
            return [v]
        return v

    @into.setter
    def into(self, new_value: Optional[Union[str, List[str]]]) -> None:
        if new_value is None:
            with suppress(KeyError):
                del self._container[MK_INSTALLATIONS_INSTALL_INTO]
            return
        if isinstance(new_value, str):
            self._container[MK_INSTALLATIONS_INSTALL_INTO] = new_value
            return
        new_list = CommentedSeq(new_value)
        self._container[MK_INSTALLATIONS_INSTALL_INTO] = new_list

    @property
    def when(self) -> Optional[Union[str, Mapping[str, Any]]]:
        return self._container[MK_CONDITION_WHEN]

    @when.setter
    def when(self, new_value: Optional[Union[str, Mapping[str, Any]]]) -> None:
        if new_value is None:
            with suppress(KeyError):
                del self._container[MK_CONDITION_WHEN]
            return
        if isinstance(new_value, str):
            self._container[MK_CONDITION_WHEN] = new_value
            return
        new_map = CommentedMap(new_value)
        self._container[MK_CONDITION_WHEN] = new_map

    @classmethod
    def install_dest(
        cls,
        sources: Union[str, List[str]],
        into: Optional[Union[str, List[str]]],
        *,
        dest_dir: Optional[str] = None,
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleInstall":
        k = MK_INSTALLATIONS_INSTALL_SOURCES
        if isinstance(sources, str):
            k = MK_INSTALLATIONS_INSTALL_SOURCE
        r = MutableYAMLInstallRuleInstall(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_INSTALL: CommentedMap(
                        {
                            k: sources,
                        }
                    )
                }
            ),
        )
        r.dest_dir = dest_dir
        r.into = into
        if when is not None:
            r.when = when
        return r

    @classmethod
    def multi_dest_install(
        cls,
        sources: Union[str, List[str]],
        dest_dirs: Sequence[str],
        into: Optional[Union[str, List[str]]],
        *,
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleInstall":
        k = MK_INSTALLATIONS_INSTALL_SOURCES
        if isinstance(sources, str):
            k = MK_INSTALLATIONS_INSTALL_SOURCE
        r = MutableYAMLInstallRuleInstall(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_MULTI_DEST_INSTALL: CommentedMap(
                        {
                            k: sources,
                            "dest-dirs": dest_dirs,
                        }
                    )
                }
            ),
        )
        r.into = into
        if when is not None:
            r.when = when
        return r

    @classmethod
    def install_as(
        cls,
        source: str,
        install_as: str,
        into: Optional[Union[str, List[str]]],
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleInstall":
        r = MutableYAMLInstallRuleInstall(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_INSTALL: CommentedMap(
                        {
                            MK_INSTALLATIONS_INSTALL_SOURCE: source,
                            MK_INSTALLATIONS_INSTALL_AS: install_as,
                        }
                    )
                }
            ),
        )
        r.into = into
        if when is not None:
            r.when = when
        return r

    @classmethod
    def install_doc_as(
        cls,
        source: str,
        install_as: str,
        into: Optional[Union[str, List[str]]],
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleInstall":
        r = MutableYAMLInstallRuleInstall(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_INSTALL_DOCS: CommentedMap(
                        {
                            MK_INSTALLATIONS_INSTALL_SOURCE: source,
                            MK_INSTALLATIONS_INSTALL_AS: install_as,
                        }
                    )
                }
            ),
        )
        r.into = into
        if when is not None:
            r.when = when
        return r

    @classmethod
    def install_docs(
        cls,
        sources: Union[str, List[str]],
        into: Optional[Union[str, List[str]]],
        *,
        dest_dir: Optional[str] = None,
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleInstall":
        k = MK_INSTALLATIONS_INSTALL_SOURCES
        if isinstance(sources, str):
            k = MK_INSTALLATIONS_INSTALL_SOURCE
        r = MutableYAMLInstallRuleInstall(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_INSTALL_DOCS: CommentedMap(
                        {
                            k: sources,
                        }
                    )
                }
            ),
        )
        r.into = into
        r.dest_dir = dest_dir
        if when is not None:
            r.when = when
        return r

    @classmethod
    def install_examples(
        cls,
        sources: Union[str, List[str]],
        into: Optional[Union[str, List[str]]],
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleInstallExamples":
        k = MK_INSTALLATIONS_INSTALL_SOURCES
        if isinstance(sources, str):
            k = MK_INSTALLATIONS_INSTALL_SOURCE
        r = MutableYAMLInstallRuleInstallExamples(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_INSTALL_EXAMPLES: CommentedMap(
                        {
                            k: sources,
                        }
                    )
                }
            ),
        )
        r.into = into
        if when is not None:
            r.when = when
        return r

    @classmethod
    def install_man(
        cls,
        sources: Union[str, List[str]],
        into: Optional[Union[str, List[str]]],
        language: Optional[str],
        when: Optional[Union[str, Mapping[str, Any]]] = None,
    ) -> "MutableYAMLInstallRuleMan":
        k = MK_INSTALLATIONS_INSTALL_SOURCES
        if isinstance(sources, str):
            k = MK_INSTALLATIONS_INSTALL_SOURCE
        r = MutableYAMLInstallRuleMan(
            None,
            None,
            store=CommentedMap(
                {
                    MK_INSTALLATIONS_INSTALL_MAN: CommentedMap(
                        {
                            k: sources,
                        }
                    )
                }
            ),
        )
        r.language = language
        r.into = into
        if when is not None:
            r.when = when
        return r

    @classmethod
    def discard(
        cls,
        sources: Union[str, List[str]],
    ) -> "MutableYAMLInstallRuleDiscard":
        return MutableYAMLInstallRuleDiscard(
            None,
            None,
            store=CommentedMap({MK_INSTALLATIONS_DISCARD: sources}),
        )


class MutableYAMLInstallRuleInstallExamples(AbstractMutableYAMLInstallRule):
    pass


class MutableYAMLInstallRuleMan(AbstractMutableYAMLInstallRule):
    @property
    def language(self) -> Optional[str]:
        return self._container[MK_INSTALLATIONS_INSTALL_MAN_LANGUAGE]

    @language.setter
    def language(self, new_value: Optional[str]) -> None:
        if new_value is not None:
            self._container[MK_INSTALLATIONS_INSTALL_MAN_LANGUAGE] = new_value
            return
        with suppress(KeyError):
            del self._container[MK_INSTALLATIONS_INSTALL_MAN_LANGUAGE]


class MutableYAMLInstallRuleDiscard(AbstractMutableYAMLInstallRule):
    pass


class MutableYAMLInstallRuleInstall(AbstractMutableYAMLInstallRule):
    @property
    def sources(self) -> List[str]:
        v = self._container[MK_INSTALLATIONS_INSTALL_SOURCES]
        if isinstance(v, str):
            return [v]
        return v

    @sources.setter
    def sources(self, new_value: Union[str, List[str]]) -> None:
        if isinstance(new_value, str):
            self._container[MK_INSTALLATIONS_INSTALL_SOURCES] = new_value
            return
        new_list = CommentedSeq(new_value)
        self._container[MK_INSTALLATIONS_INSTALL_SOURCES] = new_list

    @property
    def dest_dir(self) -> Optional[str]:
        return self._container.get(MK_INSTALLATIONS_INSTALL_DEST_DIR)

    @dest_dir.setter
    def dest_dir(self, new_value: Optional[str]) -> None:
        if new_value is not None and self.dest_as is not None:
            raise ValueError(
                f'Cannot both have a "{MK_INSTALLATIONS_INSTALL_DEST_DIR}" and'
                f' "{MK_INSTALLATIONS_INSTALL_AS}"'
            )
        if new_value is not None:
            self._container[MK_INSTALLATIONS_INSTALL_DEST_DIR] = new_value
        else:
            with suppress(KeyError):
                del self._container[MK_INSTALLATIONS_INSTALL_DEST_DIR]

    @property
    def dest_as(self) -> Optional[str]:
        return self._container.get(MK_INSTALLATIONS_INSTALL_AS)

    @dest_as.setter
    def dest_as(self, new_value: Optional[str]) -> None:
        if new_value is not None:
            if self.dest_dir is not None:
                raise ValueError(
                    f'Cannot both have a "{MK_INSTALLATIONS_INSTALL_DEST_DIR}" and'
                    f' "{MK_INSTALLATIONS_INSTALL_AS}"'
                )

            sources = self._container[MK_INSTALLATIONS_INSTALL_SOURCES]
            if isinstance(sources, list):
                if len(sources) != 1:
                    raise ValueError(
                        f'Cannot have "{MK_INSTALLATIONS_INSTALL_AS}" when'
                        f' "{MK_INSTALLATIONS_INSTALL_SOURCES}" is not exactly one item'
                    )
                self.sources = sources[0]
            self._container[MK_INSTALLATIONS_INSTALL_AS] = new_value
        else:
            with suppress(KeyError):
                del self._container[MK_INSTALLATIONS_INSTALL_AS]


class MutableYAMLInstallationsDefinition(AbstractYAMLListSubStore[Any]):
    def append(self, install_rule: AbstractMutableYAMLInstallRule) -> None:
        parent_store = self._store
        if not install_rule._is_detached or (
            install_rule._parent_store is not None
            and install_rule._parent_store is not parent_store
        ):
            raise RuntimeError(
                "Item is already attached or associated with a different container"
            )
        self.create_definition_if_missing()
        install_rule._parent_store = parent_store
        install_rule.create_definition()

    def extend(self, install_rules: Iterable[AbstractMutableYAMLInstallRule]) -> None:
        parent_store = self._store
        for install_rule in install_rules:
            if not install_rule._is_detached or (
                install_rule._parent_store is not None
                and install_rule._parent_store is not parent_store
            ):
                raise RuntimeError(
                    "Item is already attached or associated with a different container"
                )
            self.create_definition_if_missing()
            install_rule._parent_store = parent_store
            install_rule.create_definition()


class MutableYAMLManifestVariables(AbstractYAMLDictSubStore):
    @property
    def variables(self) -> Dict[str, Any]:
        return self._store

    def __setitem__(self, key: str, value: Any) -> None:
        self._store[key] = value
        self.create_definition_if_missing()


class MutableYAMLManifestDefinitions(AbstractYAMLDictSubStore):
    def manifest_variables(
        self, *, create_if_absent: bool = True
    ) -> MutableYAMLManifestVariables:
        d = MutableYAMLManifestVariables(self._store, MK_MANIFEST_VARIABLES)
        if create_if_absent:
            d.create_definition_if_missing()
        return d


class MutableYAMLManifest:
    def __init__(self, store: Any) -> None:
        self._store = store

    @classmethod
    def empty_manifest(cls) -> "MutableYAMLManifest":
        return cls(CommentedMap({MK_MANIFEST_VERSION: DEFAULT_MANIFEST_VERSION}))

    @property
    def manifest_version(self) -> str:
        return self._store[MK_MANIFEST_VERSION]

    @manifest_version.setter
    def manifest_version(self, version: str) -> None:
        if version not in SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError("Unsupported version")
        self._store[MK_MANIFEST_VERSION] = version

    def installations(
        self,
        *,
        create_if_absent: bool = True,
    ) -> MutableYAMLInstallationsDefinition:
        d = MutableYAMLInstallationsDefinition(self._store, MK_INSTALLATIONS)
        if create_if_absent:
            d.create_definition_if_missing()
        return d

    def manifest_definitions(
        self,
        *,
        create_if_absent: bool = True,
    ) -> MutableYAMLManifestDefinitions:
        d = MutableYAMLManifestDefinitions(self._store, MK_MANIFEST_DEFINITIONS)
        if create_if_absent:
            d.create_definition_if_missing()
        return d

    def package(
        self, name: str, *, create_if_absent: bool = True
    ) -> MutableYAMLPackageDefinition:
        if MK_PACKAGES not in self._store:
            self._store[MK_PACKAGES] = CommentedMap()
        packages_store = self._store[MK_PACKAGES]
        package = packages_store.get(name)
        if package is None:
            if not create_if_absent:
                raise KeyError(name)
            assert packages_store is not None
            d = MutableYAMLPackageDefinition(packages_store, name)
            d.create_definition()
        else:
            d = MutableYAMLPackageDefinition(packages_store, name)
        return d

    def write_to(self, fd) -> None:
        MANIFEST_YAML.dump(self._store, fd)


def _describe_missing_path(entry: VirtualPath) -> str:
    if entry.is_dir:
        return f"{entry.fs_path}/ (empty directory; possible integration point)"
    if entry.is_symlink:
        target = os.readlink(entry.fs_path)
        return f"{entry.fs_path} (symlink; links to {target})"
    if entry.is_file:
        return f"{entry.fs_path} (file)"
    return f"{entry.fs_path} (other!? Probably not supported by debputy and may need a `remove`)"


def _detect_missing_installations(
    path_matcher: SourcePathMatcher,
    search_dir: VirtualPath,
) -> None:
    if not os.path.isdir(search_dir.fs_path):
        return
    missing = list(path_matcher.detect_missing(search_dir))
    if not missing:
        return

    _warn(
        f"The following paths were present in {search_dir.fs_path}, but not installed (nor explicitly discarded)."
    )
    _warn("")
    for entry in missing:
        desc = _describe_missing_path(entry)
        _warn(f" * {desc}")
    _warn("")

    excl = textwrap.dedent(
        """\
        - discard: "*"
    """
    )

    _error(
        "Please review the list and add either install rules or exclusions to `installations` in"
        " debian/debputy.manifest.  If you do not need any of these paths, add the following to the"
        f" end of your 'installations`:\n\n{excl}\n"
    )


def _list_automatic_discard_rules(path_matcher: SourcePathMatcher) -> None:
    used_discard_rules = path_matcher.used_auto_discard_rules
    # Discard rules can match and then be overridden.  In that case, they appear
    # but have 0 matches.
    if not sum((len(v) for v in used_discard_rules.values()), 0):
        return
    _info("The following automatic discard rules were triggered:")
    example_path: Optional[str] = None
    for rule in sorted(used_discard_rules):
        for fs_path in sorted(used_discard_rules[rule]):
            if example_path is None:
                example_path = fs_path
            _info(f" * {rule} -> {fs_path}")
    assert example_path is not None
    _info("")
    _info(
        "Note that some of these may have been overruled. The overrule detection logic is not"
    )
    _info("100% reliable.")
    _info("")
    _info(
        "You can overrule an automatic discard rule by explicitly listing the path. As an example:"
    )
    _info("    installations:")
    _info("    - install:")
    _info(f"        source: {example_path}")


def _install_everything_from_source_dir_if_present(
    dctrl_bin: BinaryPackage,
    substitution: Substitution,
    path_matcher: SourcePathMatcher,
    install_rule_context: InstallRuleContext,
    source_condition_context: ConditionContext,
    source_dir: VirtualPath,
    *,
    into_dir: Optional[VirtualPath] = None,
) -> None:
    attribute_path = AttributePath.builtin_path()[f"installing {source_dir.fs_path}"]
    pkg_set = frozenset([dctrl_bin])
    install_rule = run_in_context_of_plugin(
        "debputy",
        InstallRule.install_dest,
        [FileSystemMatchRule.from_path_match("*", attribute_path, substitution)],
        None,
        pkg_set,
        f"Built-in; install everything from {source_dir.fs_path} into {dctrl_bin.name}",
        None,
    )
    pkg_search_dir: Tuple[SearchDir] = (
        SearchDir(
            source_dir,
            pkg_set,
        ),
    )
    replacements = {
        "search_dirs": pkg_search_dir,
    }
    if into_dir is not None:
        binary_package_contexts = dict(install_rule_context.binary_package_contexts)
        updated = binary_package_contexts[dctrl_bin.name].replace(fs_root=into_dir)
        binary_package_contexts[dctrl_bin.name] = updated
        replacements["binary_package_contexts"] = binary_package_contexts

    fake_install_rule_context = install_rule_context.replace(**replacements)
    try:
        install_rule.perform_install(
            path_matcher,
            fake_install_rule_context,
            source_condition_context,
        )
    except (
        NoMatchForInstallPatternError,
        PathAlreadyInstalledOrDiscardedError,
    ):
        # Empty directory or everything excluded by default; ignore the error
        pass


class HighLevelManifest:
    def __init__(
        self,
        manifest_path: str,
        mutable_manifest: Optional[MutableYAMLManifest],
        install_rules: Optional[List[InstallRule]],
        source_package: SourcePackage,
        binary_packages: Mapping[str, BinaryPackage],
        substitution: Substitution,
        package_transformations: Mapping[str, PackageTransformationDefinition],
        dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
        dpkg_arch_query_table: DpkgArchTable,
        build_env: DebBuildOptionsAndProfiles,
        build_environments: BuildEnvironments,
        build_rules: Optional[List[BuildRule]],
        plugin_provided_feature_set: PluginProvidedFeatureSet,
        debian_dir: VirtualPath,
    ) -> None:
        self.manifest_path = manifest_path
        self.mutable_manifest = mutable_manifest
        self._install_rules = install_rules
        self._source_package = source_package
        self._binary_packages = binary_packages
        self.substitution = substitution
        self.package_transformations = package_transformations
        self._dpkg_architecture_variables = dpkg_architecture_variables
        self._dpkg_arch_query_table = dpkg_arch_query_table
        self._build_env = build_env
        self._used_for: Set[str] = set()
        self.build_environments = build_environments
        self.build_rules = build_rules
        self._plugin_provided_feature_set = plugin_provided_feature_set
        self._debian_dir = debian_dir
        self._source_condition_context = ConditionContext(
            binary_package=None,
            substitution=self.substitution,
            deb_options_and_profiles=self._build_env,
            dpkg_architecture_variables=self._dpkg_architecture_variables,
            dpkg_arch_query_table=self._dpkg_arch_query_table,
        )

    def source_version(self, include_binnmu_version: bool = True) -> str:
        # TODO: There should an easier way to determine the source version; really.
        version_var = "{{DEB_VERSION}}"
        if not include_binnmu_version:
            version_var = "{{_DEBPUTY_INTERNAL_NON_BINNMU_SOURCE}}"
        try:
            return self.substitution.substitute(
                version_var, "internal (resolve version)"
            )
        except DebputySubstitutionError as e:
            raise AssertionError(f"Could not resolve {version_var}") from e

    @property
    def source_condition_context(self) -> ConditionContext:
        return self._source_condition_context

    @property
    def debian_dir(self) -> VirtualPath:
        return self._debian_dir

    @property
    def dpkg_architecture_variables(self) -> DpkgArchitectureBuildProcessValuesTable:
        return self._dpkg_architecture_variables

    @property
    def deb_options_and_profiles(self) -> DebBuildOptionsAndProfiles:
        return self._build_env

    @property
    def plugin_provided_feature_set(self) -> PluginProvidedFeatureSet:
        return self._plugin_provided_feature_set

    @property
    def active_packages(self) -> Iterable[BinaryPackage]:
        yield from (p for p in self._binary_packages.values() if p.should_be_acted_on)

    @property
    def all_packages(self) -> Iterable[BinaryPackage]:
        yield from self._binary_packages.values()

    def package_state_for(self, package: str) -> PackageTransformationDefinition:
        return self.package_transformations[package]

    def _detect_doc_main_package_for(self, package: BinaryPackage) -> BinaryPackage:
        name = package.name
        # If it is not a -doc package, then docs should be installed
        # under its own package name.
        if not name.endswith("-doc"):
            return package
        name = name[:-4]
        main_package = self._binary_packages.get(name)
        if main_package:
            return main_package
        if name.startswith("lib"):
            dev_pkg = self._binary_packages.get(f"{name}-dev")
            if dev_pkg:
                return dev_pkg

        # If we found no better match; default to the doc package itself.
        return package

    def perform_installations(
        self,
        integration_mode: DebputyIntegrationMode,
        *,
        install_request_context: Optional[InstallSearchDirContext] = None,
    ) -> PackageDataTable:
        package_data_dict = {}
        package_data_table = PackageDataTable(package_data_dict)
        enable_manifest_installation_feature = (
            integration_mode != INTEGRATION_MODE_DH_DEBPUTY_RRR
        )
        if install_request_context is None:

            @functools.lru_cache(None)
            def _as_path(fs_path: str) -> VirtualPath:
                return FSROOverlay.create_root_dir(".", fs_path)

            dtmp_dir = _as_path("debian/tmp")
            source_root_dir = _as_path(".")
            into = frozenset(self._binary_packages.values())
            default_search_dirs = [dtmp_dir]
            # TODO: In integration-mode full use build systems to define the per_package_search_dirs
            per_package_search_dirs = {
                t.binary_package: [_as_path(f.match_rule.path) for f in t.search_dirs]
                for t in self.package_transformations.values()
                if t.search_dirs is not None
            }
            search_dirs = _determine_search_dir_order(
                per_package_search_dirs,
                into,
                default_search_dirs,
                source_root_dir,
            )
            check_for_uninstalled_dirs = tuple(
                s.search_dir
                for s in search_dirs
                if s.search_dir.fs_path != source_root_dir.fs_path
            )
            if enable_manifest_installation_feature:
                _present_installation_dirs(
                    search_dirs, check_for_uninstalled_dirs, into
                )
        else:
            dtmp_dir = None
            search_dirs = install_request_context.search_dirs
            into = frozenset(self._binary_packages.values())
            seen: Set[BinaryPackage] = set()
            for search_dir in search_dirs:
                seen.update(search_dir.applies_to)

            missing = into - seen
            if missing:
                names = ", ".join(p.name for p in missing)
                raise ValueError(
                    f"The following package(s) had no search dirs: {names}."
                    " (Generally, the source root would be applicable to all packages)"
                )
            extra_names = seen - into
            if extra_names:
                names = ", ".join(p.name for p in extra_names)
                raise ValueError(
                    f"The install_request_context referenced the following unknown package(s): {names}"
                )

            check_for_uninstalled_dirs = (
                install_request_context.check_for_uninstalled_dirs
            )

        install_rule_context = InstallRuleContext(search_dirs)

        if (
            enable_manifest_installation_feature
            and self._install_rules is None
            and dtmp_dir is not None
            and os.path.isdir(dtmp_dir.fs_path)
        ):
            msg = (
                "The build system appears to have provided the output of upstream build system's"
                " install in debian/tmp.  However, these are no provisions for debputy to install"
                " any of that into any of the debian packages listed in debian/control."
                " To avoid accidentally creating empty packages, debputy will insist that you "
                " explicitly define an empty installation definition if you did not want to "
                " install any of those files even though they have been provided."
                ' Example: "installations: []"'
            )
            _error(msg)
        elif (
            not enable_manifest_installation_feature and self._install_rules is not None
        ):
            _error(
                f"The `installations` feature cannot be used in {self.manifest_path} with this integration mode."
                f" Please remove or comment out the `installations` keyword."
            )

        for dctrl_bin in self.all_packages:
            package = dctrl_bin.name
            doc_main_package = self._detect_doc_main_package_for(dctrl_bin)

            install_rule_context[package] = BinaryPackageInstallRuleContext(
                dctrl_bin,
                FSRootDir(),
                doc_main_package,
            )

        if enable_manifest_installation_feature:
            discard_rules = list(
                self.plugin_provided_feature_set.auto_discard_rules.values()
            )
        else:
            discard_rules = [
                self.plugin_provided_feature_set.auto_discard_rules["debian-dir"]
            ]
        path_matcher = SourcePathMatcher(discard_rules)

        source_condition_context = self._source_condition_context

        for dctrl_bin in self.active_packages:
            package = dctrl_bin.name
            if install_request_context:
                build_system_staging_dir = install_request_context.debian_pkg_dirs.get(
                    package
                )
            else:
                build_system_staging_dir_fs_path = os.path.join("debian", package)
                if os.path.isdir(build_system_staging_dir_fs_path):
                    build_system_staging_dir = FSROOverlay.create_root_dir(
                        ".",
                        build_system_staging_dir_fs_path,
                    )
                else:
                    build_system_staging_dir = None

            if build_system_staging_dir is not None:
                _install_everything_from_source_dir_if_present(
                    dctrl_bin,
                    self.substitution,
                    path_matcher,
                    install_rule_context,
                    source_condition_context,
                    build_system_staging_dir,
                )

        if self._install_rules:
            # FIXME: Check that every install rule remains used after transformations have run.
            # What we want to check is transformations do not exclude everything from an install
            # rule. The hard part here is that renaming (etc.) is fine, so we cannot 1:1 string
            # match.
            for install_rule in self._install_rules:
                install_rule.perform_install(
                    path_matcher,
                    install_rule_context,
                    source_condition_context,
                )

        if enable_manifest_installation_feature:
            for search_dir in check_for_uninstalled_dirs:
                _detect_missing_installations(path_matcher, search_dir)

        for dctrl_bin in self.all_packages:
            package = dctrl_bin.name
            binary_install_rule_context = install_rule_context[package]
            build_system_pkg_staging_dir = os.path.join("debian", package)
            fs_root = binary_install_rule_context.fs_root

            context = self.package_transformations[package]
            if dctrl_bin.should_be_acted_on and enable_manifest_installation_feature:
                for special_install_rule in context.install_rules:
                    special_install_rule.perform_install(
                        path_matcher,
                        install_rule_context,
                        source_condition_context,
                    )

            if dctrl_bin.should_be_acted_on:
                self.apply_fs_transformations(package, fs_root)
                substvars_file = f"debian/{package}.substvars"
                substvars = FlushableSubstvars.load_from_path(
                    substvars_file, missing_ok=True
                )
                # We do not want to touch the substvars file (non-clean rebuild contamination)
                substvars.substvars_path = None
                control_output_dir = generated_content_dir(
                    package=dctrl_bin, subdir_key="DEBIAN"
                )
            else:
                substvars = FlushableSubstvars()
                control_output_dir = None

            udeb_package = self._binary_packages.get(f"{package}-udeb")
            if udeb_package and not udeb_package.is_udeb:
                udeb_package = None

            package_metadata_context = PackageProcessingContextProvider(
                self,
                dctrl_bin,
                udeb_package,
                package_data_table,
                # FIXME: source_package
            )

            ctrl_creator = BinaryCtrlAccessorProviderCreator(
                package_metadata_context,
                substvars,
                context.maintscript_snippets,
                context.substitution,
            )

            if not enable_manifest_installation_feature:
                assert_no_dbgsym_migration(dctrl_bin)
                dh_dbgsym_root_fs = FSROOverlay.create_root_dir(
                    "", dhe_dbgsym_root_dir(dctrl_bin)
                )
                dbgsym_root_fs = FSRootDir()
                _install_everything_from_source_dir_if_present(
                    dctrl_bin,
                    self.substitution,
                    path_matcher,
                    install_rule_context,
                    source_condition_context,
                    dh_dbgsym_root_fs,
                    into_dir=dbgsym_root_fs,
                )
                dbgsym_build_ids = read_dbgsym_file(dctrl_bin)
                dbgsym_info = DbgsymInfo(
                    dbgsym_root_fs,
                    dbgsym_build_ids,
                )
            else:
                dbgsym_info = DbgsymInfo(
                    FSRootDir(),
                    [],
                )

            package_data_dict[package] = BinaryPackageData(
                self._source_package,
                dctrl_bin,
                build_system_pkg_staging_dir,
                control_output_dir,
                fs_root,
                substvars,
                package_metadata_context,
                ctrl_creator,
                dbgsym_info,
            )

        if enable_manifest_installation_feature:
            _list_automatic_discard_rules(path_matcher)

        return package_data_table

    def condition_context(
        self, binary_package: Optional[Union[BinaryPackage, str]]
    ) -> ConditionContext:
        if binary_package is None:
            return self._source_condition_context
        if not isinstance(binary_package, str):
            binary_package = binary_package.name

        package_transformation = self.package_transformations[binary_package]
        return self._source_condition_context.replace(
            binary_package=package_transformation.binary_package,
            substitution=package_transformation.substitution,
        )

    def apply_fs_transformations(
        self,
        package: str,
        fs_root: FSPath,
    ) -> None:
        if package in self._used_for:
            raise ValueError(
                f"data.tar contents for {package} has already been finalized!?"
            )
        if package not in self.package_transformations:
            raise ValueError(
                f'The package "{package}" was not relevant for the manifest!?'
            )
        package_transformation = self.package_transformations[package]
        condition_context = ConditionContext(
            binary_package=package_transformation.binary_package,
            substitution=package_transformation.substitution,
            deb_options_and_profiles=self._build_env,
            dpkg_architecture_variables=self._dpkg_architecture_variables,
            dpkg_arch_query_table=self._dpkg_arch_query_table,
        )
        norm_rules = list(
            builtin_mode_normalization_rules(
                self._dpkg_architecture_variables,
                package_transformation.binary_package,
                package_transformation.substitution,
            )
        )
        norm_mode_transformation_rule = ModeNormalizationTransformationRule(norm_rules)
        norm_mode_transformation_rule.transform_file_system(fs_root, condition_context)
        for transformation in package_transformation.transformations:
            transformation.run_transform_file_system(fs_root, condition_context)
        interpreter_normalization = NormalizeShebangLineTransformation()
        interpreter_normalization.transform_file_system(fs_root, condition_context)

    def finalize_data_tar_contents(
        self,
        package: str,
        fs_root: FSPath,
        clamp_mtime_to: int,
    ) -> IntermediateManifest:
        if package in self._used_for:
            raise ValueError(
                f"data.tar contents for {package} has already been finalized!?"
            )
        if package not in self.package_transformations:
            raise ValueError(
                f'The package "{package}" was not relevant for the manifest!?'
            )
        self._used_for.add(package)

        # At this point, there so be no further mutations to the file system (because the will not
        # be present in the intermediate manifest)
        cast("FSRootDir", fs_root).is_read_write = False

        intermediate_manifest = list(
            _generate_intermediate_manifest(
                fs_root,
                clamp_mtime_to,
            )
        )
        return intermediate_manifest

    def apply_to_binary_staging_directory(
        self,
        package: str,
        fs_root: FSPath,
        clamp_mtime_to: int,
    ) -> IntermediateManifest:
        self.apply_fs_transformations(package, fs_root)
        return self.finalize_data_tar_contents(package, fs_root, clamp_mtime_to)


@dataclasses.dataclass(slots=True)
class SearchDirOrderState:
    search_dir: VirtualPath
    applies_to: Union[Set[BinaryPackage], FrozenSet[BinaryPackage]] = dataclasses.field(
        default_factory=set
    )
    after: Set[str] = dataclasses.field(default_factory=set)


def _present_installation_dirs(
    search_dirs: Sequence[SearchDir],
    checked_missing_dirs: Sequence[VirtualPath],
    all_pkgs: FrozenSet[BinaryPackage],
) -> None:
    _info("The following directories are considered search dirs (in order):")
    max_len = max((len(s.search_dir.fs_path) for s in search_dirs), default=1)
    for search_dir in search_dirs:
        applies_to = ""
        if search_dir.applies_to < all_pkgs:
            names = ", ".join(p.name for p in search_dir.applies_to)
            applies_to = f"  [only applicable to: {names}]"
        remark = ""
        if not os.path.isdir(search_dir.search_dir.fs_path):
            remark = "  (skipped; absent)"
        _info(f" * {search_dir.search_dir.fs_path:{max_len}}{applies_to}{remark}")

    if checked_missing_dirs:
        _info('The following directories are considered for "not-installed" paths;')
        for d in checked_missing_dirs:
            remark = ""
            if not os.path.isdir(d.fs_path):
                remark = " (skipped; absent)"
            _info(f" * {d.fs_path:{max_len}}{remark}")


def _determine_search_dir_order(
    requested: Mapping[BinaryPackage, List[VirtualPath]],
    all_pkgs: FrozenSet[BinaryPackage],
    default_search_dirs: List[VirtualPath],
    source_root: VirtualPath,
) -> Sequence[SearchDir]:
    search_dir_table = {}
    assert requested.keys() <= all_pkgs
    for pkg in all_pkgs:
        paths = requested.get(pkg, default_search_dirs)
        previous_search_dir: Optional[SearchDirOrderState] = None
        for path in paths:
            try:
                search_dir_state = search_dir_table[path.fs_path]
            except KeyError:
                search_dir_state = SearchDirOrderState(path)
                search_dir_table[path.fs_path] = search_dir_state
            search_dir_state.applies_to.add(pkg)
            if previous_search_dir is not None:
                search_dir_state.after.add(previous_search_dir.search_dir.fs_path)
            previous_search_dir = search_dir_state

    search_dirs_in_order = []
    released = set()
    remaining = set()
    for search_dir_state in search_dir_table.values():
        if not (search_dir_state.after <= released):
            remaining.add(search_dir_state.search_dir.fs_path)
            continue
        search_dirs_in_order.append(search_dir_state)
        released.add(search_dir_state.search_dir.fs_path)

    while remaining:
        current_released = len(released)
        for fs_path in remaining:
            search_dir_state = search_dir_table[fs_path]
            if not search_dir_state.after.issubset(released):
                remaining.add(search_dir_state.search_dir.fs_path)
                continue
            search_dirs_in_order.append(search_dir_state)
            released.add(search_dir_state.search_dir.fs_path)

        if current_released == len(released):
            names = ", ".join(remaining)
            _error(
                f"There is a circular dependency (somewhere) between the search dirs: {names}."
                " Note that the search directories across all packages have to be ordered (and the"
                " source root should generally be last)"
            )
        remaining -= released

    search_dirs_in_order.append(
        SearchDirOrderState(
            source_root,
            all_pkgs,
        )
    )

    return tuple(
        # Avoid duplicating all_pkgs
        SearchDir(
            s.search_dir,
            frozenset(s.applies_to) if s.applies_to != all_pkgs else all_pkgs,
        )
        for s in search_dirs_in_order
    )
