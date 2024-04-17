import os
from functools import lru_cache

from typing import FrozenSet, Optional, Mapping, Dict


def _parse_deb_build_options(value: str) -> Mapping[str, Optional[str]]:
    res: Dict[str, Optional[str]] = {}
    for kvish in value.split():
        if "=" in kvish:
            key, value = kvish.split("=", 1)
            res[key] = value
        else:
            res[kvish] = None
    return res


class DebBuildOptionsAndProfiles:
    """Accessor to common environment related values

    >>> env = DebBuildOptionsAndProfiles(environ={'DEB_BUILD_PROFILES': 'noudeb nojava'})
    >>> 'noudeb' in env.deb_build_profiles
    True
    >>> 'nojava' in env.deb_build_profiles
    True
    >>> 'nopython' in env.deb_build_profiles
    False
    >>> sorted(env.deb_build_profiles)
    ['nojava', 'noudeb']
    """

    def __init__(self, *, environ: Optional[Mapping[str, str]] = None) -> None:
        """Provide a view of the options. Though consider using DebBuildOptionsAndProfiles.instance() instead

        :param environ: Alternative to os.environ. Mostly useful for testing purposes
        """
        if environ is None:
            environ = os.environ
        self._deb_build_profiles = frozenset(
            x for x in environ.get("DEB_BUILD_PROFILES", "").split()
        )
        self._deb_build_options = _parse_deb_build_options(
            environ.get("DEB_BUILD_OPTIONS", "")
        )

    @staticmethod
    @lru_cache(1)
    def instance() -> "DebBuildOptionsAndProfiles":
        return DebBuildOptionsAndProfiles()

    @property
    def deb_build_profiles(self) -> FrozenSet[str]:
        """A set-like view of all build profiles active during the build

        >>> env = DebBuildOptionsAndProfiles(environ={'DEB_BUILD_PROFILES': 'noudeb nojava'})
        >>> 'noudeb' in env.deb_build_profiles
        True
        >>> 'nojava' in env.deb_build_profiles
        True
        >>> 'nopython' in env.deb_build_profiles
        False
        >>> sorted(env.deb_build_profiles)
        ['nojava', 'noudeb']

        """
        return self._deb_build_profiles

    @property
    def deb_build_options(self) -> Mapping[str, Optional[str]]:
        """A set-like view of all build profiles active during the build

        >>> env = DebBuildOptionsAndProfiles(environ={'DEB_BUILD_OPTIONS': 'nostrip parallel=4'})
        >>> 'nostrip' in env.deb_build_options
        True
        >>> 'parallel' in env.deb_build_options
        True
        >>> 'noautodbgsym' in env.deb_build_options
        False
        >>> env.deb_build_options['nostrip'] is None
        True
        >>> env.deb_build_options['parallel']
        '4'
        >>> env.deb_build_options['noautodbgsym']
        Traceback (most recent call last):
            ...
        KeyError: 'noautodbgsym'
        >>> sorted(env.deb_build_options)
        ['nostrip', 'parallel']

        """
        return self._deb_build_options
