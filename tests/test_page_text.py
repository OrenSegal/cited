"""Text extraction: what the claim is checked against must read the way the
page reads. Adjacent elements are separate words to a reader even when the
HTML has no whitespace between the tags (minified pages)."""

from __future__ import annotations

from page_text import extract_html_text
from tiering_core import TIER_UNSUPPORTED, tier_for_claim

FILLER = " ".join(["The annual report also covers staffing, offices and product plans."] * 6)


def test_adjacent_block_elements_are_separate_words():
    html = "<h1>Acme Robotics</h1><p>Founded in 2019.</p><ul><li>Alpha</li><li>Beta</li></ul>"
    assert extract_html_text(html) == "Acme Robotics Founded in 2019. Alpha Beta"


def test_table_cells_are_separate_words():
    assert extract_html_text("<table><tr><th>Revenue</th><td>40</td><td>12</td></tr></table>") == "Revenue 40 12"


def test_line_breaks_separate_words():
    assert extract_html_text("<p>Acme Robotics<br>Founded in 2019</p>") == "Acme Robotics Founded in 2019"


def test_inline_markup_does_not_split_a_word():
    html = "<p>re<em>mark</em>able <b>Acme</b>'s <a href='/x'>Robo</a>tics H<sub>2</sub>O</p>"
    assert extract_html_text(html) == "remarkable Acme's Robotics H2O"


def test_name_at_the_end_of_a_heading_is_found_on_a_minified_page():
    # Shipped bug: "<h1>Acme Robotics</h1><p>Founded" was read as "RoboticsFounded",
    # so a claim naming Robotics came back "Not on page": the fabrication signal
    # for a name that is in the page's own heading.
    page = extract_html_text(f"<html><body><h1>Acme Robotics</h1><p>Founded in 2019 in Boston.</p><p>{FILLER}</p></body></html>")
    tier, note, _, _ = tier_for_claim("The firm Acme Robotics was founded in 2019 in Boston.", page)
    assert tier != TIER_UNSUPPORTED, note
