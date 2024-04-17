import argparse
import collections
import functools
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from itertools import zip_longest
from pathlib import Path
from typing import (
    NoReturn,
    TYPE_CHECKING,
    Union,
    Set,
    FrozenSet,
    Optional,
    TypeVar,
    Dict,
    Iterator,
    Iterable,
    Literal,
    Tuple,
    Sequence,
    List,
    Mapping,
    Any,
)

from debian.deb822 import Deb822

from debputy.architecture_support import DpkgArchitectureBuildProcessValuesTable
from debputy.exceptions import DebputySubstitutionError

if TYPE_CHECKING:
    from debputy.packages import BinaryPackage
    from debputy.substitution import Substitution


T = TypeVar("T")


SLASH_PRUNE = re.compile("//+")
PKGNAME_REGEX = re.compile(r"[a-z0-9][-+.a-z0-9]+", re.ASCII)
PKGVERSION_REGEX = re.compile(
    r"""
                 (?: \d+ : )?                # Optional epoch
                 \d[0-9A-Za-z.+:~]*          # Upstream version (with no hyphens)
                 (?: - [0-9A-Za-z.+:~]+ )*   # Optional debian revision (+ upstreams versions with hyphens)
""",
    re.VERBOSE | re.ASCII,
)
DEFAULT_PACKAGE_TYPE = "deb"
DBGSYM_PACKAGE_TYPE = "deb"
UDEB_PACKAGE_TYPE = "udeb"

POSTINST_DEFAULT_CONDITION = (
    '[ "$1" = "configure" ]'
    ' || [ "$1" = "abort-upgrade" ]'
    ' || [ "$1" = "abort-deconfigure" ]'
    ' || [ "$1" = "abort-remove" ]'
)


_SPACE_RE = re.compile(r"\s")
_DOUBLE_ESCAPEES = re.compile(r'([\n`$"\\])')
_REGULAR_ESCAPEES = re.compile(r'([\s!"$()*+#;<>?@\[\]\\`|~])')
_PROFILE_GROUP_SPLIT = re.compile(r">\s+<")
_DEFAULT_LOGGER: Optional[logging.Logger] = None
_STDOUT_HANDLER: Optional[logging.StreamHandler] = None
_STDERR_HANDLER: Optional[logging.StreamHandler] = None


def assume_not_none(x: Optional[T]) -> T:
    if x is None:  # pragma: no cover
        raise ValueError(
            'Internal error: None was given, but the receiver assumed "not None" here'
        )
    return x


def _info(msg: str) -> None:
    global _DEFAULT_LOGGER
    logger = _DEFAULT_LOGGER
    if logger:
        logger.info(msg)
    # No fallback print for info


def _error(msg: str, *, prog: Optional[str] = None) -> "NoReturn":
    global _DEFAULT_LOGGER
    logger = _DEFAULT_LOGGER
    if logger:
        logger.error(msg)
    else:
        me = os.path.basename(sys.argv[0]) if prog is None else prog
        print(
            f"{me}: error: {msg}",
            file=sys.stderr,
        )
    sys.exit(1)


def _warn(msg: str, *, prog: Optional[str] = None) -> None:
    global _DEFAULT_LOGGER
    logger = _DEFAULT_LOGGER
    if logger:
        logger.warning(msg)
    else:
        me = os.path.basename(sys.argv[0]) if prog is None else prog

        print(
            f"{me}: warning: {msg}",
            file=sys.stderr,
        )


class ColorizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        _error(message, prog=self.prog)


def ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o755, exist_ok=True)


def _clean_path(orig_p: str) -> str:
    p = SLASH_PRUNE.sub("/", orig_p)
    if "." in p:
        path_base = p
        # We permit a single leading "./" because we add that when we normalize a path, and we want normalization
        # of a normalized path to be a no-op.
        if path_base.startswith("./"):
            path_base = path_base[2:]
            assert path_base
        for segment in path_base.split("/"):
            if segment in (".", ".."):
                raise ValueError(
                    'Please provide paths that are normalized (i.e., no ".." or ".").'
                    f' Offending input "{orig_p}"'
                )
    return p


def _normalize_path(path: str, with_prefix: bool = True) -> str:
    path = path.strip("/")
    if not path or path == ".":
        return "."
    if "//" in path or "." in path:
        path = _clean_path(path)
    if with_prefix ^ path.startswith("./"):
        if with_prefix:
            path = "./" + path
        else:
            path = path[2:]
    return path


def _normalize_link_target(link_target: str) -> str:
    link_target = SLASH_PRUNE.sub("/", link_target.lstrip("/"))
    result: List[str] = []
    for segment in link_target.split("/"):
        if segment in (".", ""):
            # Ignore these - the empty string is generally a trailing slash
            continue
        if segment == "..":
            # We ignore "root escape attempts" like the OS would (mapping /.. -> /)
            if result:
                result.pop()
        else:
            result.append(segment)
    return "/".join(result)


def _backslash_escape(m: re.Match[str]) -> str:
    return "\\" + m.group(0)


def _escape_shell_word(w: str) -> str:
    if _SPACE_RE.match(w):
        w = _DOUBLE_ESCAPEES.sub(_backslash_escape, w)
        return f'"{w}"'
    return _REGULAR_ESCAPEES.sub(_backslash_escape, w)


def escape_shell(*args: str) -> str:
    return " ".join(_escape_shell_word(w) for w in args)


def print_command(*args: str) -> None:
    print(f"   {escape_shell(*args)}")


def debian_policy_normalize_symlink_target(
    link_path: str,
    link_target: str,
    normalize_link_path: bool = False,
) -> str:
    if normalize_link_path:
        link_path = _normalize_path(link_path)
    elif not link_path.startswith("./"):
        raise ValueError("Link part was not normalized")

    link_path = link_path[2:]

    if not link_target.startswith("/"):
        link_target = "/" + os.path.dirname(link_path) + "/" + link_target

    link_path_parts = link_path.split("/")
    link_target_parts = [
        s for s in _normalize_link_target(link_target).split("/") if s != "."
    ]

    assert link_path_parts

    if link_target_parts and link_path_parts[0] == link_target_parts[0]:
        # Per Debian Policy, must be relative

        # First determine the length of the overlap
        common_segment_count = 1
        shortest_path_length = min(len(link_target_parts), len(link_path_parts))
        while (
            common_segment_count < shortest_path_length
            and link_target_parts[common_segment_count]
            == link_path_parts[common_segment_count]
        ):
            common_segment_count += 1

        if common_segment_count == shortest_path_length and len(
            link_path_parts
        ) - 1 == len(link_target_parts):
            normalized_link_target = "."
        else:
            up_dir_count = len(link_path_parts) - 1 - common_segment_count
            normalized_link_target_parts = []
            if up_dir_count:
                up_dir_part = "../" * up_dir_count
                # We overshoot with a single '/', so rstrip it away
                normalized_link_target_parts.append(up_dir_part.rstrip("/"))
            # Add the relevant down parts
            normalized_link_target_parts.extend(
                link_target_parts[common_segment_count:]
            )

            normalized_link_target = "/".join(normalized_link_target_parts)
    else:
        # Per Debian Policy, must be absolute
        normalized_link_target = "/" + "/".join(link_target_parts)

    return normalized_link_target


def has_glob_magic(pattern: str) -> bool:
    return glob.has_magic(pattern) or "{" in pattern


def glob_escape(replacement_value: str) -> str:
    if not glob.has_magic(replacement_value) or "{" not in replacement_value:
        return replacement_value
    return (
        replacement_value.replace("[", "[[]")
        .replace("]", "[]]")
        .replace("*", "[*]")
        .replace("?", "[?]")
        .replace("{", "[{]")
        .replace("}", "[}]")
    )


# TODO: This logic should probably be moved to `python-debian`
def active_profiles_match(
    profiles_raw: str,
    active_build_profiles: Union[Set[str], FrozenSet[str]],
) -> bool:
    profiles_raw = profiles_raw.strip()
    if profiles_raw[0] != "<" or profiles_raw[-1] != ">" or profiles_raw == "<>":
        raise ValueError(
            'Invalid Build-Profiles: Must start start and end with "<" + ">" but cannot be a literal "<>"'
        )
    profile_groups = _PROFILE_GROUP_SPLIT.split(profiles_raw[1:-1])
    for profile_group_raw in profile_groups:
        should_process_package = True
        for profile_name in profile_group_raw.split():
            negation = False
            if profile_name[0] == "!":
                negation = True
                profile_name = profile_name[1:]

            matched_profile = profile_name in active_build_profiles
            if matched_profile == negation:
                should_process_package = False
                break

        if should_process_package:
            return True

    return False


def _parse_build_profiles(build_profiles_raw: str) -> FrozenSet[FrozenSet[str]]:
    profiles_raw = build_profiles_raw.strip()
    if profiles_raw[0] != "<" or profiles_raw[-1] != ">" or profiles_raw == "<>":
        raise ValueError(
            'Invalid Build-Profiles: Must start start and end with "<" + ">" but cannot be a literal "<>"'
        )
    profile_groups = _PROFILE_GROUP_SPLIT.split(profiles_raw[1:-1])
    return frozenset(frozenset(g.split()) for g in profile_groups)


def resolve_source_date_epoch(
    command_line_value: Optional[int],
    *,
    substitution: Optional["Substitution"] = None,
) -> int:
    mtime = command_line_value
    if mtime is None and "SOURCE_DATE_EPOCH" in os.environ:
        sde_raw = os.environ["SOURCE_DATE_EPOCH"]
        if sde_raw == "":
            _error("SOURCE_DATE_EPOCH is set but empty.")
        mtime = int(sde_raw)
    if mtime is None and substitution is not None:
        try:
            sde_raw = substitution.substitute(
                "{{SOURCE_DATE_EPOCH}}",
                "Internal resolution",
            )
            mtime = int(sde_raw)
        except (DebputySubstitutionError, ValueError):
            pass
    if mtime is None:
        mtime = int(time.time())
    os.environ["SOURCE_DATE_EPOCH"] = str(mtime)
    return mtime


def compute_output_filename(control_root_dir: str, is_udeb: bool) -> str:
    with open(os.path.join(control_root_dir, "control"), "rt") as fd:
        control_file = Deb822(fd)

    package_name = control_file["Package"]
    package_version = control_file["Version"]
    package_architecture = control_file["Architecture"]
    extension = control_file.get("Package-Type") or "deb"
    if ":" in package_version:
        package_version = package_version.split(":", 1)[1]
    if is_udeb:
        extension = "udeb"

    return f"{package_name}_{package_version}_{package_architecture}.{extension}"


_SCRATCH_DIR = None
_DH_INTEGRATION_MODE = False


def integrated_with_debhelper() -> None:
    global _DH_INTEGRATION_MODE
    _DH_INTEGRATION_MODE = True


def scratch_dir() -> str:
    global _SCRATCH_DIR
    if _SCRATCH_DIR is not None:
        return _SCRATCH_DIR
    debputy_scratch_dir = "debian/.debputy/scratch-dir"
    is_debputy_dir = True
    if os.path.isdir("debian/.debputy") and not _DH_INTEGRATION_MODE:
        _SCRATCH_DIR = debputy_scratch_dir
    elif os.path.isdir("debian/.debhelper") or _DH_INTEGRATION_MODE:
        _SCRATCH_DIR = "debian/.debhelper/_debputy/scratch-dir"
        is_debputy_dir = False
    else:
        _SCRATCH_DIR = debputy_scratch_dir
    ensure_dir(_SCRATCH_DIR)
    if is_debputy_dir:
        Path("debian/.debputy/.gitignore").write_text("*\n")
    return _SCRATCH_DIR


_RUNTIME_CONTAINER_DIR_KEY: Optional[str] = None


def generated_content_dir(
    *,
    package: Optional["BinaryPackage"] = None,
    subdir_key: Optional[str] = None,
) -> str:
    global _RUNTIME_CONTAINER_DIR_KEY
    container_dir = _RUNTIME_CONTAINER_DIR_KEY
    first_run = False

    if container_dir is None:
        first_run = True
        container_dir = f"_pb-{os.getpid()}"
        _RUNTIME_CONTAINER_DIR_KEY = container_dir

    directory = os.path.join(scratch_dir(), container_dir)

    if first_run and os.path.isdir(directory):
        # In the unlikely case there is a re-run with exactly the same pid, `debputy` should not
        # see "stale" data.
        # TODO: Ideally, we would always clean up this directory on failure, but `atexit` is not
        #  reliable enough for that and we do not have an obvious hook for it.
        shutil.rmtree(directory)

    directory = os.path.join(
        directory,
        "generated-fs-content",
        f"pkg_{package.name}" if package else "no-package",
    )
    if subdir_key is not None:
        directory = os.path.join(directory, subdir_key)

    os.makedirs(directory, exist_ok=True)
    return directory


PerlIncDir = collections.namedtuple("PerlIncDir", ["vendorlib", "vendorarch"])
PerlConfigData = collections.namedtuple("PerlConfigData", ["version", "debian_abi"])
_PERL_MODULE_DIRS: Dict[str, PerlIncDir] = {}


@functools.lru_cache(1)
def _perl_config_data() -> PerlConfigData:
    d = (
        subprocess.check_output(
            [
                "perl",
                "-MConfig",
                "-e",
                'print "$Config{version}\n$Config{debian_abi}\n"',
            ]
        )
        .decode("utf-8")
        .splitlines()
    )
    return PerlConfigData(*d)


def _perl_version() -> str:
    return _perl_config_data().version


def perlxs_api_dependency() -> str:
    # dh_perl used the build version of perl for this, so we will too.  Most of the perl cross logic
    # assumes that the major version of build variant of Perl is the same as the host variant of Perl.
    config = _perl_config_data()
    if config.debian_abi is not None and config.debian_abi != "":
        return f"perlapi-{config.debian_abi}"
    return f"perlapi-{config.version}"


def perl_module_dirs(
    dpkg_architecture_variables: DpkgArchitectureBuildProcessValuesTable,
    dctrl_bin: "BinaryPackage",
) -> PerlIncDir:
    global _PERL_MODULE_DIRS
    arch = (
        dctrl_bin.resolved_architecture
        if dpkg_architecture_variables.is_cross_compiling
        else "_default_"
    )
    module_dir = _PERL_MODULE_DIRS.get(arch)
    if module_dir is None:
        cmd = ["perl"]
        if dpkg_architecture_variables.is_cross_compiling:
            version = _perl_version()
            inc_dir = f"/usr/lib/{dctrl_bin.deb_multiarch}/perl/cross-config-{version}"
            # FIXME: This should not fallback to "build-arch" but on the other hand, we use the perl module dirs
            #  for every package at the moment. So mandating correct perl dirs implies mandating perl-xs-dev in
            #  cross builds... meh.
            if os.path.exists(os.path.join(inc_dir, "Config.pm")):
                cmd.append(f"-I{inc_dir}")
        cmd.extend(
            ["-MConfig", "-e", 'print "$Config{vendorlib}\n$Config{vendorarch}\n"']
        )
        output = subprocess.check_output(cmd).decode("utf-8").splitlines(keepends=False)
        if len(output) != 2:
            raise ValueError(
                "Internal error: Unable to determine the perl include directories:"
                f" Raw output from perl snippet: {output}"
            )
        module_dir = PerlIncDir(
            vendorlib=_normalize_path(output[0]),
            vendorarch=_normalize_path(output[1]),
        )
        _PERL_MODULE_DIRS[arch] = module_dir
    return module_dir


@functools.lru_cache(1)
def detect_fakeroot() -> bool:
    if os.getuid() != 0 or "LD_PRELOAD" not in os.environ:
        return False
    env = dict(os.environ)
    del env["LD_PRELOAD"]
    try:
        return subprocess.check_output(["id", "-u"], env=env).strip() != b"0"
    except subprocess.CalledProcessError:
        print(
            'Could not run "id -u" with LD_PRELOAD unset; assuming we are not run under fakeroot',
            file=sys.stderr,
        )
        return False


@functools.lru_cache(1)
def _sc_arg_max() -> Optional[int]:
    try:
        return os.sysconf("SC_ARG_MAX")
    except RuntimeError:
        _warn("Could not resolve SC_ARG_MAX, falling back to a hard-coded limit")
        return None


def _split_xargs_args(
    static_cmd: Sequence[str],
    max_args_byte_len: int,
    varargs: Iterable[str],
    reuse_list_ok: bool,
) -> Iterator[List[str]]:
    static_cmd_len = len(static_cmd)
    remaining_len = max_args_byte_len
    pending_args = list(static_cmd)
    for arg in varargs:
        arg_len = len(arg.encode("utf-8")) + 1  # +1 for leading space
        remaining_len -= arg_len
        if not remaining_len:
            if len(pending_args) <= static_cmd_len:
                raise ValueError(
                    f"Could not fit a single argument into the command line !?"
                    f" {max_args_byte_len} (variable argument limit) < {arg_len} (argument length)"
                )
            yield pending_args
            remaining_len = max_args_byte_len - arg_len
            if reuse_list_ok:
                pending_args.clear()
                pending_args.extend(static_cmd)
            else:
                pending_args = list(static_cmd)
        pending_args.append(arg)

    if len(pending_args) > static_cmd_len:
        yield pending_args


def xargs(
    static_cmd: Sequence[str],
    varargs: Iterable[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    reuse_list_ok: bool = False,
) -> Iterator[List[str]]:
    max_args_bytes = _sc_arg_max()
    # len overshoots with one space explaining the -1.  The _split_xargs_args
    # will account for the space for the first argument
    static_byte_len = (
        len(static_cmd) - 1 + sum(len(a.encode("utf-8")) for a in static_cmd)
    )
    if max_args_bytes is not None:
        if env is None:
            # +2 for nul bytes after key and value
            static_byte_len += sum(len(k) + len(v) + 2 for k, v in os.environb.items())
        else:
            # +2 for nul bytes after key and value
            static_byte_len += sum(
                len(k.encode("utf-8")) + len(v.encode("utf-8")) + 2
                for k, v in env.items()
            )
        # Add a fixed buffer for OS overhead here (in case env and cmd both must be page-aligned or something like
        # that)
        static_byte_len += 2 * 4096
    else:
        # The 20 000 limit is from debhelper, and it did not account for environment.  So neither will we here.
        max_args_bytes = 20_000
    remain_len = max_args_bytes - static_byte_len
    yield from _split_xargs_args(static_cmd, remain_len, varargs, reuse_list_ok)


# itertools recipe
def grouper(
    iterable: Iterable[T],
    n: int,
    *,
    incomplete: Literal["fill", "strict", "ignore"] = "fill",
    fillvalue: Optional[T] = None,
) -> Iterator[Tuple[T, ...]]:
    """Collect data into non-overlapping fixed-length chunks or blocks"""
    # grouper('ABCDEFG', 3, fillvalue='x') --> ABC DEF Gxx
    # grouper('ABCDEFG', 3, incomplete='strict') --> ABC DEF ValueError
    # grouper('ABCDEFG', 3, incomplete='ignore') --> ABC DEF
    args = [iter(iterable)] * n
    if incomplete == "fill":
        return zip_longest(*args, fillvalue=fillvalue)
    if incomplete == "strict":
        return zip(*args, strict=True)
    if incomplete == "ignore":
        return zip(*args)
    else:
        raise ValueError("Expected fill, strict, or ignore")


_LOGGING_SET_UP = False


def _check_color() -> Tuple[bool, bool, Optional[str]]:
    dpkg_or_default = os.environ.get(
        "DPKG_COLORS", "never" if "NO_COLOR" in os.environ else "auto"
    )
    requested_color = os.environ.get("DEBPUTY_COLORS", dpkg_or_default)
    bad_request = None
    if requested_color not in {"auto", "always", "never"}:
        bad_request = requested_color
        requested_color = "auto"

    if requested_color == "auto":
        stdout_color = sys.stdout.isatty()
        stderr_color = sys.stdout.isatty()
    else:
        enable = requested_color == "always"
        stdout_color = enable
        stderr_color = enable
    return stdout_color, stderr_color, bad_request


def program_name() -> str:
    name = os.path.basename(sys.argv[0])
    if name.endswith(".py"):
        name = name[:-3]
    if name == "__main__":
        name = os.path.basename(os.path.dirname(sys.argv[0]))
    # FIXME: Not optimal that we have to hardcode these kind of things here
    if name == "debputy_cmd":
        name = "debputy"
    return name


def package_cross_check_precheck(
    pkg_a: "BinaryPackage",
    pkg_b: "BinaryPackage",
) -> Tuple[bool, bool]:
    """Whether these two packages can do content cross-checks

    :param pkg_a: The first package
    :param pkg_b: The second package
    :return: A tuple if two booleans. If the first is True, then binary_package_a may do content cross-checks
      that invoĺves binary_package_b. If the second is True, then binary_package_b may do content cross-checks
      that involves binary_package_a. Both can be True and both can be False at the same time, which
      happens in common cases (arch:all + arch:any cases both to be False as a common example).
    """

    # Handle the two most obvious base-cases
    if not pkg_a.should_be_acted_on or not pkg_b.should_be_acted_on:
        return False, False
    if pkg_a.is_arch_all ^ pkg_b.is_arch_all:
        return False, False

    a_may_see_b = True
    b_may_see_a = True

    a_bp = pkg_a.fields.get("Build-Profiles", "")
    b_bp = pkg_b.fields.get("Build-Profiles", "")

    if a_bp != b_bp:
        a_bp_set = _parse_build_profiles(a_bp) if a_bp != "" else frozenset()
        b_bp_set = _parse_build_profiles(b_bp) if b_bp != "" else frozenset()

        # Check for build profiles being identically but just ordered differently.
        if a_bp_set != b_bp_set:
            # For simplicity, we let groups cancel each other out. If one side has no clauses
            # left, then it will always be built when the other is built.
            #
            # Eventually, someone will be here with a special case where more complex logic is
            # required. Good luck to you! Remember to add test cases for it (the existing logic
            # has some for a reason and if the logic is going to be more complex, it will need
            # tests cases to assert it fixes the problem and does not regress)
            if a_bp_set - b_bp_set:
                a_may_see_b = False
            if b_bp_set - a_bp_set:
                b_may_see_a = False

    if pkg_a.declared_architecture != pkg_b.declared_architecture:
        # Also here we could do a subset check, but wildcards vs. non-wildcards make that a pain
        if pkg_a.declared_architecture != "any":
            b_may_see_a = False
        if pkg_a.declared_architecture != "any":
            a_may_see_b = False

    return a_may_see_b, b_may_see_a


def setup_logging(
    *, log_only_to_stderr: bool = False, reconfigure_logging: bool = False
) -> None:
    global _LOGGING_SET_UP, _DEFAULT_LOGGER, _STDOUT_HANDLER, _STDERR_HANDLER
    if _LOGGING_SET_UP and not reconfigure_logging:
        raise RuntimeError(
            "Logging has already been configured."
            " Use reconfigure_logging=True if you need to reconfigure it"
        )
    stdout_color, stderr_color, bad_request = _check_color()

    if stdout_color or stderr_color:
        try:
            import colorlog
        except ImportError:
            stdout_color = False
            stderr_color = False

    if log_only_to_stderr:
        stdout = sys.stderr
        stdout_color = stderr_color
    else:
        stdout = sys.stderr

    class LogLevelFilter(logging.Filter):
        def __init__(self, threshold: int, above: bool):
            super().__init__()
            self.threshold = threshold
            self.above = above

        def filter(self, record: logging.LogRecord) -> bool:
            if self.above:
                return record.levelno >= self.threshold
            else:
                return record.levelno < self.threshold

    color_format = (
        "{bold}{name}{reset}: {bold}{log_color}{levelnamelower}{reset}: {message}"
    )
    colorless_format = "{name}: {levelnamelower}: {message}"

    existing_stdout_handler = _STDOUT_HANDLER
    existing_stderr_handler = _STDERR_HANDLER

    if stdout_color:
        stdout_handler = colorlog.StreamHandler(stdout)
        stdout_handler.setFormatter(
            colorlog.ColoredFormatter(color_format, style="{", force_color=True)
        )
        logger = colorlog.getLogger()
        if existing_stdout_handler is not None:
            logger.removeHandler(existing_stdout_handler)
        _STDOUT_HANDLER = stdout_handler
        logger.addHandler(stdout_handler)
    else:
        stdout_handler = logging.StreamHandler(stdout)
        stdout_handler.setFormatter(logging.Formatter(colorless_format, style="{"))
        logger = logging.getLogger()
        if existing_stdout_handler is not None:
            logger.removeHandler(existing_stdout_handler)
        _STDOUT_HANDLER = stdout_handler
        logger.addHandler(stdout_handler)

    if stderr_color:
        stderr_handler = colorlog.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(
            colorlog.ColoredFormatter(color_format, style="{", force_color=True)
        )
        logger = logging.getLogger()
        if existing_stdout_handler is not None:
            logger.removeHandler(existing_stderr_handler)
        _STDERR_HANDLER = stderr_handler
        logger.addHandler(stderr_handler)
    else:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(colorless_format, style="{"))
        logger = logging.getLogger()
        if existing_stdout_handler is not None:
            logger.removeHandler(existing_stderr_handler)
        _STDERR_HANDLER = stderr_handler
        logger.addHandler(stderr_handler)

    stdout_handler.addFilter(LogLevelFilter(logging.WARN, False))
    stderr_handler.addFilter(LogLevelFilter(logging.WARN, True))

    name = program_name()

    old_factory = logging.getLogRecordFactory()

    def record_factory(
        *args: Any, **kwargs: Any
    ) -> logging.LogRecord:  # pragma: no cover
        record = old_factory(*args, **kwargs)
        record.levelnamelower = record.levelname.lower()
        return record

    logging.setLogRecordFactory(record_factory)

    logging.getLogger().setLevel(logging.INFO)
    _DEFAULT_LOGGER = logging.getLogger(name)

    if bad_request:
        _DEFAULT_LOGGER.warning(
            f'Invalid color request for "{bad_request}" in either DEBPUTY_COLORS or DPKG_COLORS.'
            ' Resetting to "auto".'
        )

    _LOGGING_SET_UP = True
