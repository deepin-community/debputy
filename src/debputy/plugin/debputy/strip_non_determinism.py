import dataclasses
import os.path
import re
import subprocess
from contextlib import ExitStack
from enum import IntEnum
from typing import Iterator, Optional, List, Callable, Any, Tuple, Union

from debputy.plugin.api import VirtualPath
from debputy.plugin.api.impl_types import PackageProcessingContextProvider
from debputy.util import xargs, _info, escape_shell, _error


class DetectionVerdict(IntEnum):
    NOT_RELEVANT = 1
    NEEDS_FILE_OUTPUT = 2
    PROCESS = 3


def _file_starts_with(
    sequences: Union[bytes, Tuple[bytes, ...]]
) -> Callable[[VirtualPath], bool]:
    if isinstance(sequences, bytes):
        longest_sequence = len(sequences)
        sequences = (sequences,)
    else:
        longest_sequence = max(len(s) for s in sequences)

    def _checker(path: VirtualPath) -> bool:
        with path.open(byte_io=True, buffering=4096) as fd:
            buffer = fd.read(longest_sequence)
            return buffer in sequences

    return _checker


def _is_javadoc_file(path: VirtualPath) -> bool:
    with path.open(buffering=4096) as fd:
        c = fd.read(1024)
        return "<!-- Generated by javadoc" in c


class SndDetectionRule:
    def initial_verdict(self, path: VirtualPath) -> DetectionVerdict:
        raise NotImplementedError

    def file_output_verdict(
        self,
        path: VirtualPath,
        file_analysis: Optional[str],
    ) -> bool:
        raise TypeError(
            "Should not have been called or the rule forgot to implement this method"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ExtensionPlusFileOutputRule(SndDetectionRule):
    extensions: Tuple[str, ...]
    file_pattern: Optional[re.Pattern[str]] = None

    def initial_verdict(self, path: VirtualPath) -> DetectionVerdict:
        _, ext = os.path.splitext(path.name)
        if ext not in self.extensions:
            return DetectionVerdict.NOT_RELEVANT
        if self.file_pattern is None:
            return DetectionVerdict.PROCESS
        return DetectionVerdict.NEEDS_FILE_OUTPUT

    def file_output_verdict(
        self,
        path: VirtualPath,
        file_analysis: str,
    ) -> bool:
        file_pattern = self.file_pattern
        assert file_pattern is not None
        m = file_pattern.search(file_analysis)
        return m is not None


@dataclasses.dataclass(frozen=True, slots=True)
class ExtensionPlusContentCheck(SndDetectionRule):
    extensions: Tuple[str, ...]
    content_check: Callable[[VirtualPath], bool]

    def initial_verdict(self, path: VirtualPath) -> DetectionVerdict:
        _, ext = os.path.splitext(path.name)
        if ext not in self.extensions:
            return DetectionVerdict.NOT_RELEVANT
        content_verdict = self.content_check(path)
        if content_verdict:
            return DetectionVerdict.PROCESS
        return DetectionVerdict.NOT_RELEVANT


class PyzipFileCheck(SndDetectionRule):
    def _is_pyzip_file(self, path: VirtualPath) -> bool:
        with path.open(byte_io=True, buffering=4096) as fd:
            c = fd.read(32)
            if not c.startswith(b"#!"):
                return False

            return b"\nPK\x03\x04" in c

    def initial_verdict(self, path: VirtualPath) -> DetectionVerdict:
        if self._is_pyzip_file(path):
            return DetectionVerdict.PROCESS
        return DetectionVerdict.NOT_RELEVANT


# These detection rules should be aligned with `get_normalizer_for_file` in File::StripNondeterminism.
# Note if we send a file too much, it is just bad for performance. If we send a file to little, we
# risk non-determinism in the final output.
SND_DETECTION_RULES: List[SndDetectionRule] = [
    ExtensionPlusContentCheck(
        extensions=(".a",),
        content_check=_file_starts_with(
            (
                b"!<arch>\n",
                b"!<thin>\n",
            ),
        ),
    ),
    ExtensionPlusContentCheck(
        extensions=(".png",),
        content_check=_file_starts_with(b"\x89PNG\x0D\x0A\x1A\x0A"),
    ),
    ExtensionPlusContentCheck(
        extensions=(".gz", ".dz"),
        content_check=_file_starts_with(b"\x1F\x8B"),
    ),
    ExtensionPlusContentCheck(
        extensions=(
            # .zip related
            ".zip",
            ".pk3",
            ".epub",
            ".whl",
            ".xpi",
            ".htb",
            ".zhfst",
            ".par",
            ".codadef",
            # .jar related
            ".jar",
            ".war",
            ".hpi",
            ".apk",
            ".sym",
        ),
        content_check=_file_starts_with(
            (
                b"PK\x03\x04\x1F",
                b"PK\x05\x06",
                b"PK\x07\x08",
            )
        ),
    ),
    ExtensionPlusContentCheck(
        extensions=(
            ".mo",
            ".gmo",
        ),
        content_check=_file_starts_with(
            (
                b"\x95\x04\x12\xde",
                b"\xde\x12\x04\x95",
            )
        ),
    ),
    ExtensionPlusContentCheck(
        extensions=(".uimage",),
        content_check=_file_starts_with(b"\x27\x05\x19\x56"),
    ),
    ExtensionPlusContentCheck(
        extensions=(".bflt",),
        content_check=_file_starts_with(b"\x62\x46\x4C\x54"),
    ),
    ExtensionPlusContentCheck(
        extensions=(".jmod",),
        content_check=_file_starts_with(b"JM"),
    ),
    ExtensionPlusContentCheck(
        extensions=(".html",),
        content_check=_is_javadoc_file,
    ),
    PyzipFileCheck(),
    ExtensionPlusFileOutputRule(
        extensions=(".cpio",),
        # XXX: Add file output check (requires the file output support)
    ),
]


def _detect_paths_with_possible_non_determinism(
    fs_root: VirtualPath,
) -> Iterator[VirtualPath]:
    needs_file_output = []
    for path in fs_root.all_paths():
        if not path.is_file:
            continue
        verdict = DetectionVerdict.NOT_RELEVANT
        needs_file_output_rules = []
        for rule in SND_DETECTION_RULES:
            v = rule.initial_verdict(path)
            if v > verdict:
                verdict = v
            if verdict == DetectionVerdict.PROCESS:
                yield path
                break
            elif verdict == DetectionVerdict.NEEDS_FILE_OUTPUT:
                needs_file_output_rules.append(rule)

        if verdict == DetectionVerdict.NEEDS_FILE_OUTPUT:
            needs_file_output.append((path, needs_file_output_rules))

    assert not needs_file_output
    # FIXME: Implement file check


def _apply_strip_non_determinism(timestamp: str, paths: List[VirtualPath]) -> None:
    static_cmd = [
        "strip-nondeterminism",
        f"--timestamp={timestamp}",
        "-v",
        "--normalizers=+all",
    ]
    with ExitStack() as manager:
        affected_files = [
            manager.enter_context(p.replace_fs_path_content()) for p in paths
        ]
        for cmd in xargs(static_cmd, affected_files):
            _info(
                f"Removing (possible) unnecessary non-deterministic content via: {escape_shell(*cmd)}"
            )
            try:
                subprocess.check_call(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    restore_signals=True,
                )
            except subprocess.CalledProcessError:
                _error(
                    "Attempting to remove unnecessary non-deterministic content failed. Please review"
                    " the error from strip-nondeterminism above understand what went wrong."
                )


def strip_non_determinism(
    fs_root: VirtualPath, _: Any, context: PackageProcessingContextProvider
) -> None:
    paths = list(_detect_paths_with_possible_non_determinism(fs_root))

    if not paths:
        _info("Detected no paths to be processed by strip-nondeterminism")
        return

    substitution = context._manifest.substitution

    source_date_epoch = substitution.substitute(
        "{{_DEBPUTY_SND_SOURCE_DATE_EPOCH}}", "Internal; strip-nondeterminism"
    )

    _apply_strip_non_determinism(source_date_epoch, paths)
