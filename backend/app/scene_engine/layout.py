# app/scene_engine/layout.py
"""Layout engine: page intent -> work rect + placed block rects.

Layout numbers are fixed constants: the region rects live here; the cell minimums /
aspect clamps / padding / gutter come from `app.config`.
"""
from dataclasses import dataclass
from typing import Literal

from app.config import settings


@dataclass(frozen=True)
class Rect:
    """Server-internal rect in board logical coordinates (1200x700)."""

    x: float
    y: float
    width: float
    height: float


# Fixed regions (single source; board logical coordinates).
FIGURE_REGION = Rect(x=420, y=40, width=720, height=600)
FULL_REGION = Rect(x=40, y=40, width=1100, height=600)
WORK_RECT_DIAGRAM = Rect(x=40, y=72, width=340, height=608)
WORK_RECT_TEXT = Rect(x=40, y=72, width=1100, height=608)
ROW_PITCH_PX = 46.4
ROWS_PER_COLUMN = int(608 // ROW_PITCH_PX)          # 13


@dataclass
class BlockSpec:                                    # server-internal
    id: str
    role: Literal["figure", "table", "text"]
    preferred_aspect: float
    sticky: bool = False


@dataclass
class PageIntent:
    has_work: bool
    blocks: list[BlockSpec]
    hint: Literal["auto", "side_by_side", "stacked"] = "auto"


@dataclass
class PlacedBlock:
    id: str
    role: str
    rect: Rect
    sticky: bool


@dataclass
class PageLayout:
    work_rect: Rect | None
    blocks: list[PlacedBlock]
    overflow: list[BlockSpec]


def fit(aspect: float, w: float, h: float) -> float:
    """Fraction of a w×h cell filled by the largest rect of the given aspect."""
    return min(w, h * aspect) * min(h, w / aspect) / (w * h)


def estimate_rows(work_rect: Rect | None) -> int:
    """ROWS_PER_COLUMN × (3 if the work rect spans the text-only width, else 1); 0 when None."""
    if work_rect is None:
        return 0
    columns = 3 if work_rect.width > 400 else 1
    return ROWS_PER_COLUMN * columns


def table_aspect(text_lines: list[str]) -> float:
    """Preferred aspect for a table block: (cols*110) / (rows*36)."""
    cols = max((len(line.split(" | ")) for line in text_lines), default=1)
    rows = max(len(text_lines), 1)
    return (cols * 110) / (rows * 36)


def text_aspect(text_lines: list[str]) -> float:
    """Preferred aspect for a text block: (max_line_chars*11 + 32) / (lines*38 + 24)."""
    max_chars = max((len(line) for line in text_lines), default=0)
    lines = max(len(text_lines), 1)
    return (max_chars * 11 + 32) / (lines * 38 + 24)


def _aspect_clamp(cw: float, ch: float) -> tuple[float, float]:
    """Clamp the cell ratio into [ASPECT_MIN, ASPECT_MAX]."""
    ratio = cw / ch
    if ratio > settings.ASPECT_MAX:
        return ch * settings.ASPECT_MAX, ch
    if ratio < settings.ASPECT_MIN:
        return cw, cw / settings.ASPECT_MIN
    return cw, ch


def compute_layout(intent: PageIntent) -> PageLayout:
    """Place the page's blocks and compute the work rect."""
    has_blocks = bool(intent.blocks)
    region = FIGURE_REGION if (intent.has_work and has_blocks) else FULL_REGION
    if intent.has_work:
        work: Rect | None = WORK_RECT_DIAGRAM if has_blocks else WORK_RECT_TEXT
    else:
        work = None

    if not has_blocks:
        return PageLayout(work_rect=work, blocks=[], overflow=[])

    blocks = list(intent.blocks[: settings.MAX_BLOCKS_PER_PAGE])
    overflow = list(intent.blocks[settings.MAX_BLOCKS_PER_PAGE:])
    placed: list[PlacedBlock] = []

    w_total = region.width - 2 * settings.LAYOUT_PADDING
    h_total = region.height - 2 * settings.LAYOUT_PADDING

    while blocks:
        n = len(blocks)
        # (comparison key, rows, cols, clamped cell w, clamped cell h)
        best: tuple[tuple[float, int, int], int, int, float, float] | None = None
        for r in range(1, 5):
            for c in range(1, 5):
                if r * c < n or r * c - n >= c:
                    continue
                cw = (w_total - (c - 1) * settings.LAYOUT_GUTTER) / c
                ch = (h_total - (r - 1) * settings.LAYOUT_GUTTER) / r
                if cw < settings.MIN_CELL_W or ch < settings.MIN_CELL_H:
                    continue
                cw2, ch2 = _aspect_clamp(cw, ch)
                score = min(fit(b.preferred_aspect, cw2, ch2) for b in blocks)
                if (intent.hint == "side_by_side" and r == 1) or (intent.hint == "stacked" and c == 1):
                    score += 0.05
                key = (round(score, 4), -abs(r - c), -r)
                if best is None or key > best[0]:
                    best = (key, r, c, cw2, ch2)
        if best is None:
            # No grid fits: move the last block to overflow and repeat (step 4).
            overflow.insert(0, blocks.pop())
            continue

        _, r, c, cw2, ch2 = best
        cw = (w_total - (c - 1) * settings.LAYOUT_GUTTER) / c
        ch = (h_total - (r - 1) * settings.LAYOUT_GUTTER) / r
        for i, b in enumerate(blocks):
            row, col = divmod(i, c)
            cell_x = region.x + settings.LAYOUT_PADDING + col * (cw + settings.LAYOUT_GUTTER)
            cell_y = region.y + settings.LAYOUT_PADDING + row * (ch + settings.LAYOUT_GUTTER)
            if row == r - 1 and (n - row * c) < c:
                # Last row holding fewer than c blocks is centred horizontally (step 5).
                k = n - row * c
                group_w = k * cw + (k - 1) * settings.LAYOUT_GUTTER
                cell_x = region.x + settings.LAYOUT_PADDING + (w_total - group_w) / 2 + col * (cw + settings.LAYOUT_GUTTER)
            # Aspect-clamped block centred inside its cell (step 6).
            block_x = cell_x + (cw - cw2) / 2
            block_y = cell_y + (ch - ch2) / 2
            placed.append(PlacedBlock(
                id=b.id,
                role=b.role,
                rect=Rect(round(block_x, 2), round(block_y, 2), round(cw2, 2), round(ch2, 2)),
                sticky=b.sticky,
            ))
        break

    return PageLayout(work_rect=work, blocks=placed, overflow=overflow)
