"""Coverage for the TextMeshPro markup translator.

The inputs here are real strings observed on public Eco servers — each case is
a regression we'd hit again if `format_eco_markup` drifted.
"""

from __future__ import annotations

import pytest

from eco_mcp_app.server import format_eco_markup


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        (None, ""),
        ("plain text", "plain text"),
        (
            "<color=green>Eco</color> via <color=blue>Sirens</color>",
            '<span style="color:green">Eco</span> via <span style="color:blue">Sirens</span>',
        ),
        (
            "<color=#ff0000>red</color>",
            '<span style="color:#ff0000">red</span>',
        ),
        (
            "<#ff0000>hex shorthand</color>",
            '<span style="color:#ff0000">hex shorthand</span>',
        ),
        (
            "<#fff>short hex</color>",
            '<span style="color:#fff">short hex</span>',
        ),
        (
            "<b>bold</b> <i>ital</i> <size=20>big</size>",
            "bold ital big",
        ),
        (
            "<sprite name=foo> <icon name=bar>",
            " ",
        ),
        (
            "naked </color> close",
            "naked  close",
        ),
        (
            "<color=red>unclosed",
            '<span style="color:red">unclosed</span>',
        ),
        (
            "<color=red><color=blue>nested</color></color>",
            '<span style="color:red"><span style="color:blue">nested</span></span>',
        ),
        (
            "<script>alert(1)</script>",
            "&lt;script&gt;alert(1)&lt;/script&gt;",
        ),
        (
            "<color=red>&</color>",
            '<span style="color:red">&amp;</span>',
        ),
        (
            "<color=notacolor>x</color>",
            '<span style="color:notacolor">x</span>',
        ),
    ],
)
def test_format_eco_markup(raw: str | None, expected: str) -> None:
    assert str(format_eco_markup(raw)) == expected
