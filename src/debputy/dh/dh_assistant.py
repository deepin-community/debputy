import dataclasses
import json
import re
import subprocess
from typing import Iterable, FrozenSet, Optional, List, Union, Mapping, Any, Set, Tuple

from debian.deb822 import Deb822

from debputy.plugin.api import VirtualPath

_FIND_DH_WITH = re.compile(r"--with(?:\s+|=)(\S+)")
_DEP_REGEX = re.compile("^([a-z0-9][-+.a-z0-9]+)", re.ASCII)


@dataclasses.dataclass(frozen=True, slots=True)
class DhListCommands:
    active_commands: FrozenSet[str]
    disabled_commands: FrozenSet[str]


@dataclasses.dataclass(frozen=True, slots=True)
class DhSequencerData:
    sequences: FrozenSet[str]
    uses_dh_sequencer: bool


def _parse_dh_cmd_list(
    cmd_list: Optional[List[Union[Mapping[str, Any], object]]]
) -> Iterable[str]:
    if not isinstance(cmd_list, list):
        return

    for command in cmd_list:
        if not isinstance(command, dict):
            continue
        command_name = command.get("command")
        if isinstance(command_name, str):
            yield command_name


def resolve_active_and_inactive_dh_commands(
    dh_rules_addons: Iterable[str],
    *,
    source_root: Optional[str] = None,
) -> DhListCommands:
    cmd = ["dh_assistant", "list-commands", "--output-format=json"]
    if dh_rules_addons:
        addons = ",".join(dh_rules_addons)
        cmd.append(f"--with={addons}")
    try:
        output = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            cwd=source_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return DhListCommands(
            frozenset(),
            frozenset(),
        )
    else:
        result = json.loads(output)
        active_commands = frozenset(_parse_dh_cmd_list(result.get("commands")))
        disabled_commands = frozenset(
            _parse_dh_cmd_list(result.get("disabled-commands"))
        )
        return DhListCommands(
            active_commands,
            disabled_commands,
        )


def parse_drules_for_addons(lines: Iterable[str], sequences: Set[str]) -> bool:
    saw_dh = False
    for line in lines:
        if not line.startswith("\tdh "):
            continue
        saw_dh = True
        for match in _FIND_DH_WITH.finditer(line):
            sequence_def = match.group(1)
            sequences.update(sequence_def.split(","))
    return saw_dh


def extract_dh_addons_from_control(
    source_paragraph: Union[Mapping[str, str], Deb822],
    sequences: Set[str],
) -> None:
    for f in ("Build-Depends", "Build-Depends-Indep", "Build-Depends-Arch"):
        field = source_paragraph.get(f)
        if not field:
            continue

        for dep_clause in (d.strip() for d in field.split(",")):
            match = _DEP_REGEX.match(dep_clause.strip())
            if not match:
                continue
            dep = match.group(1)
            if not dep.startswith("dh-sequence-"):
                continue
            sequences.add(dep[12:])


def read_dh_addon_sequences(
    debian_dir: VirtualPath,
) -> Optional[Tuple[Set[str], Set[str], bool]]:
    ctrl_file = debian_dir.get("control")
    if ctrl_file:
        dr_sequences: Set[str] = set()
        bd_sequences: Set[str] = set()

        drules = debian_dir.get("rules")
        saw_dh = False
        if drules and drules.is_file:
            with drules.open() as fd:
                saw_dh = parse_drules_for_addons(fd, dr_sequences)

        with ctrl_file.open() as fd:
            ctrl = list(Deb822.iter_paragraphs(fd))
        source_paragraph = ctrl[0] if ctrl else {}

        extract_dh_addons_from_control(source_paragraph, bd_sequences)
        return bd_sequences, dr_sequences, saw_dh
    return None


def extract_dh_compat_level(*, cwd=None) -> Tuple[Optional[int], int]:
    try:
        output = subprocess.check_output(
            ["dh_assistant", "active-compat-level"],
            stderr=subprocess.DEVNULL,
            cwd=cwd,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        exit_code = 127
        if isinstance(e, subprocess.CalledProcessError):
            exit_code = e.returncode
        return None, exit_code
    else:
        data = json.loads(output)
        active_compat_level = data.get("active-compat-level")
        exit_code = 0
        if not isinstance(active_compat_level, int) or active_compat_level < 1:
            active_compat_level = None
            exit_code = 255
        return active_compat_level, exit_code
