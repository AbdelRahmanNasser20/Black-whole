"""/deals map view: the visitor sizes the map (gutter drag for width, corner drag for height),
the side list shows two cards per row, and the feed grid is five across."""
from pathlib import Path

WEB = Path("automation/web")
MAP_CSS = (WEB / "static/deals/map.css").read_text()
MAP_JS = (WEB / "static/deals/map.js").read_text()


def test_split_is_driven_by_a_css_variable_and_a_gutter():
    assert "--map-w" in MAP_CSS and "minmax(280px, var(--map-w))" in MAP_CSS
    assert ".feed-map-gutter" in MAP_CSS and "cursor: col-resize" in MAP_CSS
    assert "feed-map-gutter" in MAP_JS and "pointerdown" in MAP_JS and "setPointerCapture" in MAP_JS
    assert "localStorage.setItem(SPLIT_STORE" in MAP_JS          # the split is remembered


def test_map_box_resizes_vertically_and_tells_maplibre():
    rule = MAP_CSS.split('[id="feed-map"] {', 1)[1].split("}", 1)[0]
    assert "resize: vertical" in rule
    assert "ResizeObserver" in MAP_JS and "invalidateSize()" in MAP_JS
    assert "panel.style.height" in MAP_JS                        # the list keeps the map's height


def test_side_list_is_two_across_on_a_laptop():
    assert '[id="feed-map-panel"] .map-panel-list { grid-template-columns: repeat(2, minmax(0, 1fr))' in MAP_CSS


def test_phone_keeps_the_stacked_layout_without_a_gutter():
    phone = MAP_CSS.split("@media (max-width: 1023px)", 1)[1]
    assert ".feed-map-gutter { display: none; }" in phone and "resize: none" in phone
