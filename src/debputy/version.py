from typing import Optional, Callable

__version__ = "N/A"

IS_RELEASE_BUILD = False

if __version__ in ("N/A",):
    import subprocess

    class LazyString:
        def __init__(self, initializer: Callable[[], str]) -> None:
            self._initializer = initializer
            self._value: Optional[str] = None

        def __str__(self) -> str:
            value = object.__getattribute__(self, "_value")
            if value is None:
                value = object.__getattribute__(self, "_initializer")()
                object.__setattr__(self, "_value", value)
            return value

        def __getattribute__(self, item):
            value = str(self)
            return getattr(value, item)

        def __contains__(self, item):
            return item in str(self)

    def _initialize_version() -> str:
        try:
            devnull: Optional[int] = subprocess.DEVNULL
        except AttributeError:
            devnull = None  # Not supported, but not critical

        try:
            v = (
                subprocess.check_output(
                    ["git", "describe", "--tags"],
                    stderr=devnull,
                )
                .strip()
                .decode("utf-8")
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                v = (
                    subprocess.check_output(
                        ["dpkg-parsechangelog", "-SVersion"],
                        stderr=devnull,
                    )
                    .strip()
                    .decode("utf-8")
                )

            except (subprocess.CalledProcessError, FileNotFoundError):
                v = "N/A"

        if v.startswith("debian/"):
            v = v[7:]
        return v

    __version__ = LazyString(_initialize_version)
    IS_RELEASE_BUILD = False

else:
    # Disregard snapshot versions (gbp dch -S) as "release builds"
    IS_RELEASE_BUILD = ".gbp" not in __version__
