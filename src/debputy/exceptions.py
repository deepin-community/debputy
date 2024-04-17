from typing import cast, TYPE_CHECKING

if TYPE_CHECKING:
    from debputy.plugin.api.impl_types import DebputyPluginMetadata


class DebputyRuntimeError(RuntimeError):
    @property
    def message(self) -> str:
        return cast("str", self.args[0])


class DebputySubstitutionError(DebputyRuntimeError):
    pass


class DebputyManifestVariableRequiresDebianDirError(DebputySubstitutionError):
    pass


class DebputyDpkgGensymbolsError(DebputyRuntimeError):
    pass


class SymlinkLoopError(ValueError):
    @property
    def message(self) -> str:
        return cast("str", self.args[0])


class PureVirtualPathError(TypeError):
    @property
    def message(self) -> str:
        return cast("str", self.args[0])


class TestPathWithNonExistentFSPathError(TypeError):
    @property
    def message(self) -> str:
        return cast("str", self.args[0])


class DebputyFSError(DebputyRuntimeError):
    pass


class DebputyFSIsROError(DebputyFSError):
    pass


class PluginBaseError(DebputyRuntimeError):
    pass


class DebputyPluginRuntimeError(PluginBaseError):
    pass


class PluginNotFoundError(PluginBaseError):
    pass


class PluginInitializationError(PluginBaseError):
    pass


class PluginMetadataError(PluginBaseError):
    pass


class PluginConflictError(PluginBaseError):
    @property
    def plugin_a(self) -> "DebputyPluginMetadata":
        return cast("DebputyPluginMetadata", self.args[1])

    @property
    def plugin_b(self) -> "DebputyPluginMetadata":
        return cast("DebputyPluginMetadata", self.args[2])


class PluginAPIViolationError(PluginBaseError):
    pass


class UnhandledOrUnexpectedErrorFromPluginError(PluginBaseError):
    pass


class DebputyMetadataAccessError(DebputyPluginRuntimeError):
    pass
