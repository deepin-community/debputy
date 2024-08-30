from typing import Mapping, Any, Optional

import pytest
from debian.deb822 import Deb822
from debputy.yaml.compat import CommentedMap

from debputy.lsp.maint_prefs import (
    MaintainerPreferenceTable,
    determine_effective_preference,
    EffectiveFormattingPreference,
    _WAS_DEFAULTS,
)
from debputy.packages import SourcePackage


def test_load_styles() -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    assert "niels@thykier.net" in styles.maintainer_preferences
    nt_maint_pref = styles.maintainer_preferences["niels@thykier.net"]
    # Note this is data dependent; if it fails because the style changes, update the test
    assert nt_maint_pref.canonical_name == "Niels Thykier"
    assert not nt_maint_pref.is_packaging_team
    black_style = styles.named_styles["black"]
    nt_style = nt_maint_pref.formatting
    assert nt_style is not None
    assert black_style == black_style


def test_load_no_styles() -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    assert "packages@qa.debian.org" in styles.maintainer_preferences
    qa_maint_pref = styles.maintainer_preferences["packages@qa.debian.org"]
    assert qa_maint_pref.canonical_name == "Debian QA Group"
    assert qa_maint_pref.is_packaging_team
    # Orphaned packages do not have a predefined style, since Debian (nor Debian QA) have
    # one well-defined style.
    assert qa_maint_pref.formatting is None


def test_load_named_styles() -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    assert "black" in styles.named_styles
    black_style = styles.named_styles["black"]
    # Note this is data dependent; if it fails because the style changes, update the test
    assert black_style.deb822_normalize_field_content
    assert black_style.deb822_short_indent
    assert black_style.deb822_always_wrap
    assert black_style.deb822_trailing_separator
    assert black_style.deb822_max_line_length == 79
    assert not black_style.deb822_normalize_stanza_order

    # TODO: Not implemented yet
    assert not black_style.deb822_normalize_field_order


def test_compat_styles() -> None:
    styles = MaintainerPreferenceTable.load_preferences()

    # Data dependent; if it breaks, provide a stubbed style preference table
    assert "niels@thykier.net" in styles.maintainer_preferences
    assert "zeha@debian.org" in styles.maintainer_preferences
    assert "random-package@packages.debian.org" not in styles.maintainer_preferences
    assert "random@example.org" not in styles.maintainer_preferences

    nt_style = styles.maintainer_preferences["niels@thykier.net"].formatting
    zeha_style = styles.maintainer_preferences["zeha@debian.org"].formatting

    # Data dependency
    assert nt_style == zeha_style

    fields = Deb822(
        {
            "Package": "foo",
            "Maintainer": "Foo <random-package@packages.debian.org>",
            "Uploaders": "Niels Thykier <niels@thykier.net>",
        },
    )
    src = SourcePackage(fields)

    effective_style, tool, _ = determine_effective_preference(styles, src, None)
    assert effective_style == nt_style
    assert tool == "debputy reformat"

    fields["Uploaders"] = (
        "Niels Thykier <niels@thykier.net>, Chris Hofstaedtler <zeha@debian.org>"
    )
    src = SourcePackage(fields)

    effective_style, tool, _ = determine_effective_preference(styles, src, None)
    assert effective_style == nt_style
    assert effective_style == zeha_style
    assert tool == "debputy reformat"

    fields["Uploaders"] = (
        "Niels Thykier <niels@thykier.net>, Chris Hofstaedtler <zeha@debian.org>, Random Developer <random@example.org>"
    )
    src = SourcePackage(fields)

    effective_style, tool, _ = determine_effective_preference(styles, src, None)
    assert effective_style is None
    assert tool is None


@pytest.mark.xfail
def test_compat_styles_team_maint() -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    fields = Deb822(
        {
            "Package": "foo",
            # Missing a stubbed definition for `team@lists.debian.org`
            "Maintainer": "Packaging Team <team@lists.debian.org>",
            "Uploaders": "Random Developer <random@example.org>",
        },
    )
    src = SourcePackage(fields)
    assert "team@lists.debian.org" in styles.maintainer_preferences
    assert "random@example.org" not in styles.maintainer_preferences
    team_style = styles.maintainer_preferences["team@lists.debian.org"]
    assert team_style.is_packaging_team
    effective_style, tool, _ = determine_effective_preference(styles, src, None)
    assert effective_style == team_style.formatting
    assert tool is None


def test_x_style() -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    fields = Deb822(
        {
            "Package": "foo",
            "X-Style": "black",
            "Maintainer": "Random Developer <random@example.org>",
        },
    )
    src = SourcePackage(fields)
    assert "random@example.org" not in styles.maintainer_preferences
    assert "black" in styles.named_styles
    black_style = styles.named_styles["black"]
    effective_style, tool, _ = determine_effective_preference(styles, src, None)
    assert effective_style == black_style
    assert tool == "debputy reformat"


def test_was_from_salsa_ci_style() -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    fields = Deb822(
        {
            "Package": "foo",
            "Maintainer": "Random Developer <random@example.org>",
        },
    )
    src = SourcePackage(fields)
    assert "random@example.org" not in styles.maintainer_preferences
    effective_style, tool, _ = determine_effective_preference(styles, src, None)
    assert effective_style is None
    assert tool is None
    salsa_ci = CommentedMap(
        {"variables": CommentedMap({"SALSA_CI_DISABLE_WRAP_AND_SORT": "yes"})}
    )
    effective_style, tool, _ = determine_effective_preference(styles, src, salsa_ci)
    assert effective_style is None
    assert tool is None

    salsa_ci = CommentedMap(
        {"variables": CommentedMap({"SALSA_CI_DISABLE_WRAP_AND_SORT": "no"})}
    )
    effective_style, tool, _ = determine_effective_preference(styles, src, salsa_ci)
    was_style = EffectiveFormattingPreference(**_WAS_DEFAULTS)
    assert effective_style == was_style
    assert tool == "wrap-and-sort"


@pytest.mark.parametrize(
    "was_args,style_delta",
    [
        (
            "-a",
            {
                "deb822_always_wrap": True,
            },
        ),
        (
            "-sa",
            {
                "deb822_always_wrap": True,
                "deb822_short_indent": True,
            },
        ),
        (
            "-sa --keep-first",
            {
                "deb822_always_wrap": True,
                "deb822_short_indent": True,
            },
        ),
        (
            "-sab --keep-first",
            {
                "deb822_always_wrap": True,
                "deb822_short_indent": True,
                "deb822_normalize_stanza_order": True,
            },
        ),
        (
            "-sab --no-keep-first",
            {
                "deb822_always_wrap": True,
                "deb822_short_indent": True,
                "deb822_normalize_stanza_order": False,
            },
        ),
    ],
)
def test_was_from_salsa_ci_style_args(
    was_args: str,
    style_delta: Optional[Mapping[str, Any]],
) -> None:
    styles = MaintainerPreferenceTable.load_preferences()
    fields = Deb822(
        {
            "Package": "foo",
            "Maintainer": "Random Developer <random@example.org>",
        },
    )
    src = SourcePackage(fields)
    assert "random@example.org" not in styles.maintainer_preferences
    salsa_ci = CommentedMap(
        {
            "variables": CommentedMap(
                {
                    "SALSA_CI_DISABLE_WRAP_AND_SORT": "no",
                    "SALSA_CI_WRAP_AND_SORT_ARGS": was_args,
                }
            )
        }
    )
    effective_style, tool, _ = determine_effective_preference(styles, src, salsa_ci)
    if style_delta is None:
        assert effective_style is None
        assert tool is None
    else:
        was_style = EffectiveFormattingPreference(**_WAS_DEFAULTS).replace(
            **style_delta,
        )

        assert effective_style == was_style
        assert tool == f"wrap-and-sort {was_args}".strip()
