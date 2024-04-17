import functools
import itertools
import os
import re
import subprocess
from typing import Iterable, FrozenSet, Tuple, Optional, List

from debian.debian_support import Release
from lsprotocol.types import Diagnostic, Range, Position, DiagnosticSeverity

from debputy.lsp.quickfixes import propose_correct_text_quick_fix
from debputy.lsp.text_util import LintCapablePositionCodec
from debputy.util import _info, _warn

_SPELL_CHECKER_DICT = "/usr/share/hunspell/en_US.dic"
_SPELL_CHECKER_AFF = "/usr/share/hunspell/en_US.aff"
_WORD_PARTS = re.compile(r"(\S+)")
_PRUNE_SYMBOLS_RE = re.compile(r"(\w+(?:-\w+|'\w+)?)")
_FIND_QUOTE_CHAR = re.compile(r'["`]')
_LOOKS_LIKE_FILENAME = re.compile(
    r"""
      [.]{0,3}/[a-z0-9]+(/[a-z0-9]+)+/*
    | [a-z0-9-_]+(/[a-z0-9]+)+/*
    | [a-z0-9_]+(/[a-z0-9_]+){2,}/*
    | (?:\S+)?[.][a-z]{1,3}

""",
    re.VERBOSE,
)
_LOOKS_LIKE_PROGRAMMING_TERM = re.compile(
    r"""
    (
        # Java identifier Camel Case
          [a-z][a-z0-9]*(?:[A-Z]{1,3}[a-z0-9]+)+
        # Type name Camel Case
        | [A-Z]{1,3}[a-z0-9]+(?:[A-Z]{1,3}[a-z0-9]+)+
        # Type name Camel Case with underscore (seen in Dh_Lib.pm among other
        | [A-Z]{1,3}[a-z0-9]+(?:_[A-Z]{1,3}[a-z0-9]+)+
        # Perl module
        | [A-Z]{1,3}[a-z0-9]+(?:_[A-Z]{1,3}[a-z0-9]+)*(::[A-Z]{1,3}[a-z0-9]+(?:_[A-Z]{1,3}[a-z0-9]+)*)+
        # Probably an abbreviation
        | [A-Z]{3,}
        # Perl/Python identifiers or Jinja templates
        | [$%&@_]?[{]?[{]?[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:(?:->)?[\[{]\S+|}}?)?
        # SCREAMING_SNAKE_CASE (environment variables plus -DVAR=B or $FOO)
        | [-$%&*_]{0,2}[A-Z][A-Z0-9]*(_[A-Z0-9]+)+(?:=\S+)?
        | \#[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\#
        # Subcommand names. Require at least two "-" to avoid skipping hyphenated words
        | [a-z][a-z0-9]*(-[a-z0-9]+){2,}
        # Short args
        | -[a-z0-9]+
        # Things like 32bit
        | \d{2,}-?[a-z]+
        # Source package (we do not have a package without prefix/suffix because it covers 95% of all lowercase words)
        | src:[a-z0-9][-+.a-z0-9]+
        | [a-z0-9][-+.a-z0-9]+:(?:any|native)
        # Version
        | v\d+(?:[.]\S+)?
        # chmod symbolic mode or math
        | \S*=\S+
    )
""",
    re.VERBOSE,
)
_LOOKS_LIKE_EMAIL = re.compile(
    r"""
    <[^>@\s]+@[^>@\s]+>
""",
    re.VERBOSE,
)
_NO_CORRECTIONS = tuple()
_WORDLISTS = [
    "debian-wordlist.dic",
]
_NAMELISTS = [
    "logins-and-people.dic",
]
_PERSONAL_DICTS = [
    "${HOME}/.hunspell_default",
    "${HOME}/.hunspell_en_US",
]


try:
    if not os.path.lexists(_SPELL_CHECKER_DICT) or not os.path.lexists(
        _SPELL_CHECKER_AFF
    ):
        raise ImportError
    from hunspell import HunSpell

    _HAS_HUNSPELL = True
except ImportError:
    _HAS_HUNSPELL = False


def _read_wordlist(
    base_dir: str, wordlist_name: str, *, namelist: bool = False
) -> Iterable[str]:
    with open(os.path.join(base_dir, wordlist_name)) as fd:
        w = [w.strip() for w in fd]
        yield from w
        if namelist:
            yield from (f"{n}'s" for n in w)


def _all_debian_archs() -> Iterable[str]:
    try:
        output = subprocess.check_output(["dpkg-architecture", "-L"])
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        _warn(f"dpkg-architecture -L failed: {e}")
        return tuple()

    return (x.strip() for x in output.decode("utf-8").splitlines())


@functools.lru_cache
def _builtin_exception_words() -> FrozenSet[str]:
    basedirs = os.path.dirname(__file__)
    release_names = (x for x in Release.releases)
    return frozenset(
        itertools.chain(
            itertools.chain.from_iterable(
                _read_wordlist(basedirs, wl) for wl in _WORDLISTS
            ),
            itertools.chain.from_iterable(
                _read_wordlist(basedirs, wl, namelist=True) for wl in _NAMELISTS
            ),
            release_names,
            _all_debian_archs(),
        )
    )


_DEFAULT_SPELL_CHECKER: Optional["Spellchecker"] = None


def spellcheck_line(
    lines: List[str],
    position_codec: LintCapablePositionCodec,
    line_no: int,
    line: str,
) -> Iterable[Diagnostic]:
    spell_checker = default_spellchecker()
    for word, pos, endpos in spell_checker.iter_words(line):
        corrections = spell_checker.provide_corrections_for(word)
        if not corrections:
            continue
        word_range_server_units = Range(
            Position(line_no, pos),
            Position(line_no, endpos),
        )
        word_range = position_codec.range_to_client_units(
            lines,
            word_range_server_units,
        )
        yield Diagnostic(
            word_range,
            f'Spelling "{word}"',
            severity=DiagnosticSeverity.Hint,
            source="debputy",
            data=[propose_correct_text_quick_fix(c) for c in corrections],
        )


def default_spellchecker() -> "Spellchecker":
    global _DEFAULT_SPELL_CHECKER
    spellchecker = _DEFAULT_SPELL_CHECKER
    if spellchecker is None:
        if _HAS_HUNSPELL:
            spellchecker = HunspellSpellchecker()
        else:
            spellchecker = _do_nothing_spellchecker()
        _DEFAULT_SPELL_CHECKER = spellchecker
    return spellchecker


@functools.lru_cache()
def _do_nothing_spellchecker() -> "Spellchecker":
    return EverythingIsCorrectSpellchecker()


def disable_spellchecking() -> None:
    global _DEFAULT_SPELL_CHECKER
    _DEFAULT_SPELL_CHECKER = _do_nothing_spellchecker()


def _skip_quoted_parts(line: str) -> Iterable[Tuple[str, int]]:
    current_pos = 0
    while True:
        try:
            m = _FIND_QUOTE_CHAR.search(line, current_pos)
            if m is None:
                if current_pos == 0:
                    yield line, 0
                else:
                    yield line[current_pos:], current_pos
                return
            starting_marker_pos = m.span()[0]
            quote_char = m.group()
            end_marker_pos = line.index(quote_char, starting_marker_pos + 1)
        except ValueError:
            yield line[current_pos:], current_pos
            return

        part = line[current_pos:starting_marker_pos]

        if not part.isspace():
            yield part, current_pos
        current_pos = end_marker_pos + 1


def _split_line_to_words(line: str) -> Iterable[Tuple[str, int, int]]:
    for line_part, part_pos in _skip_quoted_parts(line):
        for m in _WORD_PARTS.finditer(line_part):
            fullword = m.group(1)
            if fullword.startswith("--"):
                # CLI arg
                continue
            if _LOOKS_LIKE_PROGRAMMING_TERM.match(fullword):
                continue
            if _LOOKS_LIKE_FILENAME.match(fullword):
                continue
            if _LOOKS_LIKE_EMAIL.match(fullword):
                continue
            mpos = m.span(1)[0]
            for sm in _PRUNE_SYMBOLS_RE.finditer(fullword):
                pos, endpos = sm.span(1)
                offset = part_pos + mpos
                yield sm.group(1), pos + offset, endpos + offset


class Spellchecker:

    @staticmethod
    def do_nothing_spellchecker() -> "Spellchecker":
        return EverythingIsCorrectSpellchecker()

    def iter_words(self, line: str) -> Iterable[Tuple[str, int, int]]:
        yield from _split_line_to_words(line)

    def provide_corrections_for(self, word: str) -> Iterable[str]:
        raise NotImplementedError

    def ignore_word(self, word: str) -> None:
        raise NotImplementedError


class EverythingIsCorrectSpellchecker(Spellchecker):
    def provide_corrections_for(self, word: str) -> Iterable[str]:
        return _NO_CORRECTIONS

    def ignore_word(self, word: str) -> None:
        # It is hard to ignore words, when you never check them in the fist place.
        pass


class HunspellSpellchecker(Spellchecker):

    def __init__(self) -> None:
        self._checker = HunSpell(_SPELL_CHECKER_DICT, _SPELL_CHECKER_AFF)
        for w in _builtin_exception_words():
            self._checker.add(w)
        self._load_personal_exclusions()

    def provide_corrections_for(self, word: str) -> Iterable[str]:
        if word.startswith(
            (
                "dpkg-",
                "dh-",
                "dh_",
                "debian-",
                "debconf-",
                "update-",
                "DEB_",
                "DPKG_",
            )
        ):
            return _NO_CORRECTIONS
        # 'ing is deliberately forcing a word into another word-class
        if word.endswith(("'ing", "-nss")):
            return _NO_CORRECTIONS
        return self._lookup(word)

    @functools.lru_cache(128)
    def _lookup(self, word: str) -> Iterable[str]:
        if self._checker.spell(word):
            return _NO_CORRECTIONS
        return self._checker.suggest(word)

    def ignore_word(self, word: str) -> None:
        self._checker.add(word)

    def _load_personal_exclusions(self) -> None:
        for filename in _PERSONAL_DICTS:
            if filename.startswith("${"):
                end_index = filename.index("}")
                varname = filename[2:end_index]
                value = os.environ.get(varname)
                if value is None:
                    continue
                filename = value + filename[end_index + 1 :]
            if os.path.isfile(filename):
                _info(f"Loading personal spelling dictionary from {filename}")
                self._checker.add_dic(filename)
