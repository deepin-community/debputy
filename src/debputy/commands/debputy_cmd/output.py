import argparse
import contextlib
import itertools
import os
import re
import shutil
import subprocess
import sys
from typing import (
    Union,
    Sequence,
    Iterable,
    Iterator,
    IO,
    Mapping,
    Tuple,
    Optional,
    Any,
)

from debputy.util import assume_not_none

try:
    import colored

    if (
        not hasattr(colored, "Style")
        or not hasattr(colored, "Fore")
        or not hasattr(colored, "Back")
    ):
        # Seen with python3-colored v1 (bookworm)
        raise ImportError
except ImportError:
    colored = None


def _pager() -> Optional[str]:
    pager = os.environ.get("DEBPUTY_PAGER")
    if pager is None:
        pager = os.environ.get("PAGER")
    if pager is None and shutil.which("less") is not None:
        pager = "less"
    return pager


URL_START = "\033]8;;"
URL_END = "\033]8;;\a"
MAN_URL_REWRITE = re.compile(r"man:(\S+)[(](\d+)[)]")

_SUPPORTED_COLORS = {
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
}
_SUPPORTED_STYLES = {"none", "bold"}


class OutputStylingBase:
    def __init__(
        self,
        stream: IO[str],
        output_format: str,
        *,
        optimize_for_screen_reader: bool = False,
    ) -> None:
        self.stream = stream
        self.output_format = output_format
        self.optimize_for_screen_reader = optimize_for_screen_reader
        self._color_support = None

    def colored(
        self,
        text: str,
        *,
        fg: Optional[Union[str]] = None,
        bg: Optional[str] = None,
        style: Optional[str] = None,
    ) -> str:
        self._check_color(fg)
        self._check_color(bg)
        self._check_text_style(style)
        return text

    @property
    def supports_colors(self) -> bool:
        return False

    def print_list_table(
        self,
        headers: Sequence[Union[str, Tuple[str, str]]],
        rows: Sequence[Sequence[str]],
    ) -> None:
        if rows:
            if any(len(r) != len(rows[0]) for r in rows):
                raise ValueError(
                    "Unbalanced table: All rows must have the same column count"
                )
            if len(rows[0]) != len(headers):
                raise ValueError(
                    "Unbalanced table: header list does not agree with row list on number of columns"
                )

        if not headers:
            raise ValueError("No headers provided!?")

        cadjust = {}
        header_names = []
        for c in headers:
            if isinstance(c, str):
                header_names.append(c)
            else:
                cname, adjust = c
                header_names.append(cname)
                cadjust[cname] = adjust

        if self.output_format == "csv":
            from csv import writer

            w = writer(self.stream)
            w.writerow(header_names)
            w.writerows(rows)
            return

        column_lengths = [
            max((len(h), max(len(r[i]) for r in rows)))
            for i, h in enumerate(header_names)
        ]
        # divider => "+---+---+-...-+"
        divider = "+-" + "-+-".join("-" * x for x in column_lengths) + "-+"
        # row_format => '| {:<10} | {:<8} | ... |' where the numbers are the column lengths
        row_format_inner = " | ".join(
            f"{{CELL_COLOR}}{{:{cadjust.get(cn, '<')}{x}}}{{CELL_COLOR_RESET}}"
            for cn, x in zip(header_names, column_lengths)
        )

        row_format = f"| {row_format_inner} |"

        if self.supports_colors:
            cs = self._color_support
            assert cs is not None
            header_color = cs.Style.bold
            header_color_reset = cs.Style.reset
        else:
            header_color = ""
            header_color_reset = ""

        self.print_visual_formatting(divider)
        self.print(
            row_format.format(
                *header_names,
                CELL_COLOR=header_color,
                CELL_COLOR_RESET=header_color_reset,
            )
        )
        self.print_visual_formatting(divider)
        for row in rows:
            self.print(row_format.format(*row, CELL_COLOR="", CELL_COLOR_RESET=""))
        self.print_visual_formatting(divider)

    def print(self, /, string: str = "", **kwargs) -> None:
        if "file" in kwargs:
            raise ValueError("Unsupported kwarg file")
        print(string, file=self.stream, **kwargs)

    def print_visual_formatting(self, /, format_sequence: str, **kwargs) -> None:
        if self.optimize_for_screen_reader:
            return
        self.print(format_sequence, **kwargs)

    def print_for_screen_reader(self, /, text: str, **kwargs) -> None:
        if not self.optimize_for_screen_reader:
            return
        self.print(text, **kwargs)

    def _check_color(self, color: Optional[str]) -> None:
        if color is not None and color not in _SUPPORTED_COLORS:
            raise ValueError(
                f"Unsupported color: {color}. Only the following are supported {','.join(_SUPPORTED_COLORS)}"
            )

    def _check_text_style(self, style: Optional[str]) -> None:
        if style is not None and style not in _SUPPORTED_STYLES:
            raise ValueError(
                f"Unsupported style: {style}. Only the following are supported {','.join(_SUPPORTED_STYLES)}"
            )

    def render_url(self, link_url: str) -> str:
        return link_url

    def bts(self, bugno) -> str:
        return f"https://bugs.debian.org/{bugno}"


class ANSIOutputStylingBase(OutputStylingBase):
    def __init__(
        self,
        stream: IO[str],
        output_format: str,
        *,
        support_colors: bool = True,
        support_clickable_urls: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(stream, output_format, **kwargs)
        self._stream = stream
        self._color_support = colored
        self._support_colors = (
            support_colors if self._color_support is not None else False
        )
        self._support_clickable_urls = support_clickable_urls

    @property
    def supports_colors(self) -> bool:
        return self._support_colors

    def colored(
        self,
        text: str,
        *,
        fg: Optional[str] = None,
        bg: Optional[str] = None,
        style: Optional[str] = None,
    ) -> str:
        self._check_color(fg)
        self._check_color(bg)
        self._check_text_style(style)
        _colored = self._color_support
        if not self.supports_colors or _colored is None:
            return text
        codes = []
        if style is not None:
            code = getattr(_colored.Style, style)
            assert code is not None
            codes.append(code)
        if fg is not None:
            code = getattr(_colored.Fore, fg)
            assert code is not None
            codes.append(code)
        if bg is not None:
            code = getattr(_colored.Back, bg)
            assert code is not None
            codes.append(code)
        if not codes:
            return text
        return "".join(codes) + text + _colored.Style.reset

    def render_url(self, link_url: str) -> str:
        if not self._support_clickable_urls:
            return super().render_url(link_url)
        link_text = link_url
        if not self.optimize_for_screen_reader and link_url.startswith("man:"):
            # Rewrite man page to a clickable link by default. I am not sure how the hyperlink
            # ANSI code works with screen readers, so lets not rewrite the man page link by
            # default. My fear is that both the link url and the link text gets read out.
            m = MAN_URL_REWRITE.match(link_url)
            if m:
                page, section = m.groups()
                link_url = f"https://manpages.debian.org/{page}.{section}"
        return URL_START + f"{link_url}\a{link_text}" + URL_END

    def bts(self, bugno) -> str:
        if not self._support_clickable_urls:
            return super().bts(bugno)
        return self.render_url(f"https://bugs.debian.org/{bugno}")


def no_fancy_output(
    stream: IO[str] = None,
    output_format: str = str,
    optimize_for_screen_reader: bool = False,
) -> OutputStylingBase:
    if stream is None:
        stream = sys.stdout
    return OutputStylingBase(
        stream,
        output_format,
        optimize_for_screen_reader=optimize_for_screen_reader,
    )


def _output_styling(
    parsed_args: argparse.Namespace,
    stream: IO[str],
) -> OutputStylingBase:
    output_format = getattr(parsed_args, "output_format", None)
    if output_format is None:
        output_format = "text"
    optimize_for_screen_reader = os.environ.get("OPTIMIZE_FOR_SCREEN_READER", "") != ""
    if not stream.isatty():
        return no_fancy_output(
            stream,
            output_format,
            optimize_for_screen_reader=optimize_for_screen_reader,
        )

    return ANSIOutputStylingBase(
        stream, output_format, optimize_for_screen_reader=optimize_for_screen_reader
    )


@contextlib.contextmanager
def _stream_to_pager(
    parsed_args: argparse.Namespace,
) -> Iterator[Tuple[IO[str], OutputStylingBase]]:
    fancy_output = _output_styling(parsed_args, sys.stdout)
    if (
        not parsed_args.pager
        or not sys.stdout.isatty()
        or fancy_output.output_format != "text"
    ):
        yield sys.stdout, fancy_output
        return

    pager = _pager()
    if pager is None:
        yield sys.stdout, fancy_output
        return

    env: Mapping[str, str] = os.environ
    if "LESS" not in env:
        env_copy = dict(os.environ)
        env_copy["LESS"] = "-FRSXMQ"
        env = env_copy

    cmd = subprocess.Popen(
        pager,
        stdin=subprocess.PIPE,
        encoding="utf-8",
        env=env,
    )
    stdin = assume_not_none(cmd.stdin)
    try:
        fancy_output.stream = stdin
        yield stdin, fancy_output
    except Exception:
        stdin.close()
        cmd.kill()
        cmd.wait()
        raise
    finally:
        fancy_output.stream = sys.stdin
    stdin.close()
    cmd.wait()


def _normalize_cell(cell: Union[str, Sequence[str]], times: int) -> Iterable[str]:
    if isinstance(cell, str):
        return itertools.chain([cell], itertools.repeat("", times=times - 1))
    if not cell:
        return itertools.repeat("", times=times)
    return itertools.chain(cell, itertools.repeat("", times=times - len(cell)))


def _expand_rows(
    rows: Sequence[Sequence[Union[str, Sequence[str]]]]
) -> Iterator[Sequence[str]]:
    for row in rows:
        if all(isinstance(c, str) for c in row):
            yield row
        else:
            longest = max(len(c) if isinstance(c, list) else 1 for c in row)
            cells = [_normalize_cell(c, times=longest) for c in row]
            yield from zip(*cells)
