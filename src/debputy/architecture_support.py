import os
import subprocess
from functools import lru_cache
from typing import Dict, Optional, Iterator, Tuple


class DpkgArchitectureBuildProcessValuesTable:
    """Dict-like interface to dpkg-architecture values"""

    def __init__(self, *, mocked_answers: Optional[Dict[str, str]] = None) -> None:
        """Create a new dpkg-architecture table; NO INSTANTIATION

        This object will be created for you; if you need a production instance
        then call dpkg_architecture_table().  If you need a testing instance,
        then call mock_arch_table(...)

        :param mocked_answers: Used for testing purposes.  Do not use directly;
        instead use mock_arch_table(...) to create the table you want.
        """
        self._architecture_cache: Dict[str, str] = {}
        self._has_run_dpkg_architecture = False
        if mocked_answers is None:
            self._architecture_cache = {}
            self._respect_environ: bool = True
            self._has_run_dpkg_architecture = False
        else:
            self._architecture_cache = mocked_answers
            self._respect_environ = False
            self._has_run_dpkg_architecture = True

    def __contains__(self, item: str) -> bool:
        try:
            self[item]
        except KeyError:
            return False
        else:
            return True

    def __getitem__(self, item: str) -> str:
        if item not in self._architecture_cache:
            if self._respect_environ:
                value = os.environ.get(item)
                if value is not None:
                    self._architecture_cache[item] = value
                    return value
            if not self._has_run_dpkg_architecture:
                self._load_dpkg_architecture_values()
            # Fall through and look it up in the cache
        return self._architecture_cache[item]

    def __iter__(self) -> Iterator[str]:
        if not self._has_run_dpkg_architecture:
            self._load_dpkg_architecture_values()
        yield from self._architecture_cache

    @property
    def current_host_arch(self) -> str:
        """The architecture we are building for

        This is the architecture name you need if you are in doubt.
        """
        return self["DEB_HOST_ARCH"]

    @property
    def current_host_multiarch(self) -> str:
        """The multi-arch path basename

        This is the multi-arch basename name you need if you are in doubt.  It
        goes here:

            "/usr/lib/{MA}".format(table.current_host_multiarch)

        """
        return self["DEB_HOST_MULTIARCH"]

    @property
    def is_cross_compiling(self) -> bool:
        """Whether we are cross-compiling

        This is defined as DEB_BUILD_GNU_TYPE != DEB_HOST_GNU_TYPE and
        affects whether we can rely on being able to run the binaries
        that are compiled.
        """
        return self["DEB_BUILD_GNU_TYPE"] != self["DEB_HOST_GNU_TYPE"]

    def _load_dpkg_architecture_values(self) -> None:
        env = dict(os.environ)
        # For performance, disable dpkg's translation later
        env["DPKG_NLS"] = "0"
        kw_pairs = _parse_dpkg_arch_output(
            subprocess.check_output(
                ["dpkg-architecture"],
                env=env,
            )
        )
        for k, v in kw_pairs:
            self._architecture_cache[k] = os.environ.get(k, v)
        self._has_run_dpkg_architecture = True


def _parse_dpkg_arch_output(output: bytes) -> Iterator[Tuple[str, str]]:
    text = output.decode("utf-8")
    for line in text.splitlines():
        k, v = line.strip().split("=", 1)
        yield k, v


def _rewrite(value: str, from_pattern: str, to_pattern: str) -> str:
    assert value.startswith(from_pattern)
    return to_pattern + value[len(from_pattern) :]


def faked_arch_table(
    host_arch: str,
    *,
    build_arch: Optional[str] = None,
    target_arch: Optional[str] = None,
) -> DpkgArchitectureBuildProcessValuesTable:
    """Creates a mocked instance of DpkgArchitectureBuildProcessValuesTable


    :param host_arch: The dpkg architecture to mock answers for.  This affects
      DEB_HOST_* values and defines the default for DEB_{BUILD,TARGET}_* if
      not overridden.
    :param build_arch: If set and has a different value than host_arch, then
      pretend this is a cross-build.  This value affects the DEB_BUILD_* values.
    :param target_arch: If set and has a different value than host_arch, then
      pretend this is a build _of_ a cross-compiler.  This value affects the
      DEB_TARGET_* values.
    """

    if build_arch is None:
        build_arch = host_arch

    if target_arch is None:
        target_arch = host_arch
    return _faked_arch_tables(host_arch, build_arch, target_arch)


@lru_cache
def _faked_arch_tables(
    host_arch: str, build_arch: str, target_arch: str
) -> DpkgArchitectureBuildProcessValuesTable:
    mock_table = {}

    env = dict(os.environ)
    # Set CC to /bin/true avoid a warning from dpkg-architecture
    env["CC"] = "/bin/true"
    # For performance, disable dpkg's translation later
    env["DPKG_NLS"] = "0"
    # Clear environ variables that might confuse dpkg-architecture
    for k in os.environ:
        if k.startswith("DEB_"):
            del env[k]

    if build_arch == host_arch:
        # easy / common case - we can handle this with a single call
        kw_pairs = _parse_dpkg_arch_output(
            subprocess.check_output(
                ["dpkg-architecture", "-a", host_arch, "-A", target_arch],
                env=env,
            )
        )
        for k, v in kw_pairs:
            if k.startswith(("DEB_HOST_", "DEB_TARGET_")):
                mock_table[k] = v
            # Clone DEB_HOST_* into DEB_BUILD_* as well
            if k.startswith("DEB_HOST_"):
                k2 = _rewrite(k, "DEB_HOST_", "DEB_BUILD_")
                mock_table[k2] = v
    elif build_arch != host_arch and host_arch != target_arch:
        # This will need two dpkg-architecture calls because we cannot set
        # DEB_BUILD_* directly.  But we can set DEB_HOST_* and then rewrite
        # it
        # First handle the build arch
        kw_pairs = _parse_dpkg_arch_output(
            subprocess.check_output(
                ["dpkg-architecture", "-a", build_arch],
                env=env,
            )
        )
        for k, v in kw_pairs:
            if k.startswith("DEB_HOST_"):
                k = _rewrite(k, "DEB_HOST_", "DEB_BUILD_")
                mock_table[k] = v

        kw_pairs = _parse_dpkg_arch_output(
            subprocess.check_output(
                ["dpkg-architecture", "-a", host_arch, "-A", target_arch],
                env=env,
            )
        )
        for k, v in kw_pairs:
            if k.startswith(("DEB_HOST_", "DEB_TARGET_")):
                mock_table[k] = v
    else:
        # This is a fun special case.  We know that:
        # * build_arch != host_arch
        # * host_arch == target_arch
        # otherwise we would have hit one of the previous cases.
        #
        # We can do this in a single call to dpkg-architecture by
        # a bit of "cleaver" rewriting.
        #
        # - Use -a to set DEB_HOST_* and then rewrite that as
        #   DEB_BUILD_*
        # - use -A to set DEB_TARGET_* and then use that for both
        #   DEB_HOST_* and DEB_TARGET_*

        kw_pairs = _parse_dpkg_arch_output(
            subprocess.check_output(
                ["dpkg-architecture", "-a", build_arch, "-A", target_arch], env=env
            )
        )
        for k, v in kw_pairs:
            if k.startswith("DEB_HOST_"):
                k2 = _rewrite(k, "DEB_HOST_", "DEB_BUILD_")
                mock_table[k2] = v
                continue
            if k.startswith("DEB_TARGET_"):
                mock_table[k] = v
                k2 = _rewrite(k, "DEB_TARGET_", "DEB_HOST_")
                mock_table[k2] = v

    table = DpkgArchitectureBuildProcessValuesTable(mocked_answers=mock_table)
    return table


_ARCH_TABLE = DpkgArchitectureBuildProcessValuesTable()


def dpkg_architecture_table() -> DpkgArchitectureBuildProcessValuesTable:
    return _ARCH_TABLE
