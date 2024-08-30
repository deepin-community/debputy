from typing import Mapping, Optional

from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.commands.debputy_cmd.context import CommandContext
from debputy.manifest_conditions import _run_build_time_tests


class BuildContext:
    @staticmethod
    def from_command_context(
        cmd_context: CommandContext,
    ) -> "BuildContext":
        return BuildContextImpl(cmd_context)

    @property
    def deb_build_options(self) -> Mapping[str, Optional[str]]:
        raise NotImplementedError

    def parallelization_limit(self, *, support_zero_as_unlimited: bool = False) -> int:
        """Parallelization limit of the build

        This is accessor that reads the `parallel` option from `DEB_BUILD_OPTIONS` with relevant
        fallback behavior.

        :param support_zero_as_unlimited: The debhelper framework allowed `0` to mean unlimited
          in some build systems. If the build system supports this, it should set this option
          to True, which will allow `0` as a possible return value. WHen this option is False
          (which is the default), `0` will be remapped to a high number to preserve the effect
          in spirit (said fallback number is also from `debhelper`).
        """
        limit = self.deb_build_options.get("parallel")
        if limit is None:
            return 1
        try:
            v = int(limit)
        except ValueError:
            return 1
        if v == 0 and not support_zero_as_unlimited:
            # debhelper allowed "0" to be used as unlimited in some cases. Preserve that feature
            # for callers that are prepared for it. For everyone else, remap 0 to an obscene number
            # that de facto has the same behaviour
            #
            # The number is taken out of `cmake.pm` from `debhelper` to be "Bug compatible" with
            # debhelper on the fallback as well.
            return 999
        return v

    @property
    def is_terse_build(self) -> bool:
        """Whether the build is terse

        This is a shorthand for testing for `terse` in DEB_BUILD_OPTIONS
        """
        return "terse" in self.deb_build_options

    @property
    def is_cross_compiling(self) -> bool:
        """Whether the build is considered a cross build

        Note: Do **not** use this as indicator for whether tests should run. Use `should_run_tests` instead.
          To the naive eye, they seem like they overlap in functionality, but they do not. There are cross
          builds where tests can be run. Additionally, there are non-cross-builds where tests should be
          skipped.
        """
        return self.dpkg_architecture_variables.is_cross_compiling

    def cross_tool(self, command: str) -> str:
        if not self.is_cross_compiling:
            return command
        cross_prefix = self.dpkg_architecture_variables["DEB_HOST_GNU_TYPE"]
        return f"{cross_prefix}-{command}"

    @property
    def dpkg_architecture_variables(self) -> DpkgArchitectureBuildProcessValuesTable:
        raise NotImplementedError

    @property
    def should_run_tests(self) -> bool:
        return _run_build_time_tests(self.deb_build_options)


class BuildContextImpl(BuildContext):
    def __init__(
        self,
        cmd_context: CommandContext,
    ) -> None:
        self._cmd_context = cmd_context

    @property
    def deb_build_options(self) -> Mapping[str, Optional[str]]:
        return self._cmd_context.deb_build_options

    @property
    def dpkg_architecture_variables(self) -> DpkgArchitectureBuildProcessValuesTable:
        return self._cmd_context.dpkg_architecture_variables()
