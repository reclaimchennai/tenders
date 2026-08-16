"""Sparse BoQ sheets must not lose their line items to the column cap.

The bug this pins was silent, which is what made it serious. A GePNIC BoQ is a
template scattered across a very sparse grid: BOQ_835214.xls declares 243
columns, of which 0-54 carry the tax and total structure, 55-237 are entirely
empty, and 238-242 carry the actual work descriptions, quantities and units —
the substance of a bill of quantities.

Taking a contiguous window of the first MAX_COLS therefore spent most of its
budget on blanks and stopped long before the line items, so the preview showed
a mangled table *and* omitted the rows that mattered, with nothing on screen to
say anything had been withheld. A reader checking what a department had priced
would have concluded the file contained no line items at all.
"""

from __future__ import annotations

from tenders.web.docview import MAX_COLS, _sheet


def _row(pairs: dict[int, str], width: int) -> list[str]:
    return [pairs.get(i, "") for i in range(width)]


def test_line_items_far_to_the_right_survive_the_column_cap():
    """The real shape: data at columns 0-2 and again at 238-242."""
    width = 243
    rows = [
        _row({0: "Percentage BoQ"}, width),
        _row({0: "BoQ_Ver4.0", 1: "Percentage", 2: "Normal"}, width),
        _row({0: "1", 238: "1.01", 239: "Supplying and fixing spls.",
              240: "item1", 241: "123.223", 242: "Nos"}, width),
    ]
    out = _sheet("BoQ1", rows, len(rows))

    flat = [c for r in out["rows"] for c in r]
    assert "Supplying and fixing spls." in flat, "line-item description dropped"
    assert "123.223" in flat, "line-item quantity dropped"
    assert "Nos" in flat, "line-item unit dropped"
    # 8 columns hold content, so nothing had to be withheld.
    assert out["cols"] == 8
    assert out["truncated_cols"] == 0


def test_empty_columns_are_dropped_not_rendered():
    """183 blank columns must not be handed to the table to divide width over."""
    width = 243
    rows = [_row({0: "A", 242: "B"}, width)]
    out = _sheet("S", rows, 1)
    assert out["cols"] == 2
    assert out["header"] == ["A", "B"]


def test_row_order_and_alignment_are_preserved():
    """Compaction must keep every row on the same columns as every other row."""
    width = 50
    rows = [
        _row({0: "h0", 10: "h10", 40: "h40"}, width),
        _row({0: "a0", 40: "a40"}, width),        # nothing at 10
        _row({10: "b10"}, width),
    ]
    out = _sheet("S", rows, len(rows))
    assert out["header"] == ["h0", "h10", "h40"]
    # The gap in row 2 stays a gap in the *same* column, not a leftward shift.
    assert out["rows"] == [["a0", "", "a40"], ["", "b10", ""]]


def test_truncated_cols_counts_only_columns_that_held_something():
    """The count is a claim about what the reader was denied.

    Blank columns were never information, so dropping them must not be
    reported as withholding data — that would turn an honest notice into
    noise on every BoQ in the archive.
    """
    width = 900
    filled = {i * 3: f"c{i}" for i in range(MAX_COLS + 5)}   # 5 more than fit
    out = _sheet("S", [_row(filled, width)], 1)
    assert out["cols"] == MAX_COLS
    assert out["truncated_cols"] == 5


def test_trailing_blank_rows_are_still_trimmed():
    rows = [_row({0: "x"}, 5), _row({}, 5), _row({}, 5)]
    out = _sheet("S", rows, len(rows))
    assert out["header"] == ["x"]
    assert out["rows"] == []
