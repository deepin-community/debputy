import dataclasses
import os.path
import re
import shutil
from typing import Optional, IO, TYPE_CHECKING

if TYPE_CHECKING:
    from debputy.plugin.api import VirtualPath

_SHEBANG_RE = re.compile(
    rb"""
    ^[#][!]\s*
    (/\S+/([a-zA-Z][^/\s]*))
""",
    re.VERBOSE | re.ASCII,
)
_WORD = re.compile(rb"\s+(\S+)")
_STRIP_VERSION = re.compile(r"(-?\d+(?:[.]\d.+)?)$")

_KNOWN_INTERPRETERS = {
    os.path.basename(c): c
    for c in ["/bin/sh", "/bin/bash", "/bin/dash", "/usr/bin/perl", "/usr/bin/python"]
}


class Interpreter:
    @property
    def original_command(self) -> str:
        """The original command (without arguments) from the #! line

        This returns the command as it was written (without flags/arguments) in the file.

        Note as a special-case, if the original command is `env` then the first argument is included
        as well, because it is assumed to be the real command.


        >>> # Note: Normally, you would use `VirtualPath.interpreter()` instead for extracting the interpreter
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3 -b")
        >>> python3.original_command
        '/usr/bin/python3'
        >>> env_sh = extract_shebang_interpreter(b"#! /usr/bin/env sh")
        >>> env_sh.original_command
        '/usr/bin/env sh'

        :return: The original command in the #!-line
        """
        raise NotImplementedError

    @property
    def command_full_basename(self) -> str:
        """The full basename of the command (with version)

        Note that for #!-lines that uses `env`, this will return the argument for `env` rather than
        `env`.

        >>> # Note: Normally, you would use `VirtualPath.interpreter()` instead for extracting the interpreter
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3 -b")
        >>> python3.command_full_basename
        'python3'
        >>> env_sh = extract_shebang_interpreter(b"#! /usr/bin/env sh")
        >>> env_sh.command_full_basename
        'sh'

        :return: The full basename of the command.
        """
        raise NotImplementedError

    @property
    def command_stem(self) -> str:
        """The basename of the command **without** version

        Note that for #!-lines that uses `env`, this will return the argument for `env` rather than
        `env`.

        >>> # Note: Normally, you would use `VirtualPath.interpreter()` instead for extracting the interpreter
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3 -b")
        >>> python3.command_stem
        'python'
        >>> env_sh = extract_shebang_interpreter(b"#! /usr/bin/env sh")
        >>> env_sh.command_stem
        'sh'
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3.12-dbg -b")
        >>> python3.command_stem
        'python'

        :return: The basename of the command **without** version.
        """
        raise NotImplementedError

    @property
    def interpreter_version(self) -> str:
        """The version part of the basename

        Note that for #!-lines that uses `env`, this will return the argument for `env` rather than
        `env`.

        >>> # Note: Normally, you would use `VirtualPath.interpreter()` instead for extracting the interpreter
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3 -b")
        >>> python3.interpreter_version
        '3'
        >>> env_sh = extract_shebang_interpreter(b"#! /usr/bin/env sh")
        >>> env_sh.interpreter_version
        ''
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3.12-dbg -b")
        >>> python3.interpreter_version
        '3.12-dbg'

        :return: The version part of the command or the empty string if the command is versionless.
        """
        raise NotImplementedError

    @property
    def fixup_needed(self) -> bool:
        """Whether the interpreter uses a non-canonical location

        >>> # Note: Normally, you would use `VirtualPath.interpreter()` instead for extracting the interpreter
        >>> python3 = extract_shebang_interpreter(b"#! /usr/bin/python3 -b")
        >>> python3.fixup_needed
        False
        >>> env_sh = extract_shebang_interpreter(b"#! /usr/bin/env sh")
        >>> env_sh.fixup_needed
        True
        >>> ub_sh = extract_shebang_interpreter(b"#! /usr/bin/sh")
        >>> ub_sh.fixup_needed
        True
        >>> sh = extract_shebang_interpreter(b"#! /bin/sh")
        >>> sh.fixup_needed
        False

        :return: True if this interpreter is uses a non-canonical version.
        """
        return False


@dataclasses.dataclass(slots=True, frozen=True)
class DetectedInterpreter(Interpreter):
    original_command: str
    command_full_basename: str
    command_stem: str
    interpreter_version: str
    correct_command: Optional[str] = None
    corrected_shebang_line: Optional[str] = None

    @property
    def fixup_needed(self) -> bool:
        return self.corrected_shebang_line is not None

    def replace_shebang_line(self, path: "VirtualPath") -> None:
        new_shebang_line = self.corrected_shebang_line
        assert new_shebang_line.startswith("#!")
        if not new_shebang_line.endswith("\n"):
            new_shebang_line += "\n"
        parent_dir = path.parent_dir
        assert parent_dir is not None
        with path.open(byte_io=True) as rfd:
            original_first_line = rfd.readline()
            if not original_first_line.startswith(b"#!"):
                raise ValueError(
                    f'The provided path "{path.path}" does not start with a shebang line!?'
                )
            mtime = path.mtime
            with path.replace_fs_path_content() as new_fs_path, open(
                new_fs_path, "wb"
            ) as wfd:
                wfd.write(new_shebang_line.encode("utf-8"))
                shutil.copyfileobj(rfd, wfd)
            # Ensure the mtime is not updated (we do not count interpreter correction as a "change")
            path.mtime = mtime


def extract_shebang_interpreter_from_file(
    fd: IO[bytes],
) -> Optional[DetectedInterpreter]:
    first_line = fd.readline(4096)
    if b"\n" not in first_line:
        # If there is no newline, then it is probably not a shebang line
        return None
    return extract_shebang_interpreter(first_line)


def extract_shebang_interpreter(first_line: bytes) -> Optional[DetectedInterpreter]:
    m = _SHEBANG_RE.search(first_line)
    if not m:
        return None
    raw_command = m.group(1).strip().decode("utf-8")
    command_full_basename = m.group(2).strip().decode("utf-8")
    endpos = m.end()
    if command_full_basename == "env":
        wm = _WORD.search(first_line, pos=m.end())
        if wm is not None:
            command_full_basename = wm.group(1).decode("utf-8")
            raw_command += " " + command_full_basename
            endpos = wm.end()
    command_stem = command_full_basename
    vm = _STRIP_VERSION.search(command_full_basename)
    if vm:
        version = vm.group(1)
        command_stem = command_full_basename[: -len(version)]
    else:
        version = ""
    correct_command = _KNOWN_INTERPRETERS.get(command_stem)
    if correct_command is not None and version != "":
        correct_command += version

    if correct_command is not None and correct_command != raw_command:
        trailing = first_line[endpos + 1 :].strip().decode("utf-8")
        corrected_shebang_line = "#! " + correct_command
        if trailing:
            corrected_shebang_line += " " + trailing
    else:
        corrected_shebang_line = None

    return DetectedInterpreter(
        raw_command,
        command_full_basename,
        command_stem,
        version,
        correct_command,
        corrected_shebang_line,
    )
