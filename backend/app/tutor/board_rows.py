# app/tutor/board_rows.py
import re
from typing import Any
from app.contracts.agent_state import RowMirror


class RowIdAllocator:
    """Manager-wide `wN` allocator: ids stay unique across every run and page."""

    def __init__(self, initial_next_number: int = 1) -> None:
        self.next_row_number = initial_next_number

    def allocate(self) -> str:
        row_id = f"w{self.next_row_number}"
        self.next_row_number += 1
        return row_id


class BoardRowTracker:
    def __init__(self, page_id: str = "p1", initial_next_number: int = 1,
                 allocator: RowIdAllocator | None = None) -> None:
        self.page_id = page_id
        self.next_row_number = initial_next_number
        self.allocator = allocator
        self.visible_rows: list[dict[str, str]] = []  # [{"row_id": "w1", "text": "..."}]
        self.rows_remaining = 99
        self.current_turn_write_row_ids: list[str] = []

    def allocate_row_id(self) -> str:
        """Allocate next unique wN id (shared allocator when the run was given one)."""
        if self.allocator is not None:
            row_id = self.allocator.allocate()
        else:
            row_id = f"w{self.next_row_number}"
            self.next_row_number += 1
        self.current_turn_write_row_ids.append(row_id)
        return row_id

    def reset_for_new_page(self) -> None:
        """Reset tracking state for a new page after a [PAGE_BREAK].

        The row_number counter is NOT reset — IDs are globally unique within a
        turn to prevent collisions if old page ops are still in flight.
        """
        self.visible_rows = []
        self.rows_remaining = 99
        self.current_turn_write_row_ids = []

    def record_board_report(self, page_id: str, rows: list[dict[str, Any]], rows_remaining: int) -> None:
        self.page_id = page_id
        self.visible_rows = [{"row_id": r.get("row_id") or r.get("rowId", ""), "text": r.get("text", "")} for r in rows]
        self.rows_remaining = rows_remaining

    def resolve_emphasize(self, target: str) -> str | None:
        """Resolve [EMPHASIZE:target] argument into a concrete 'wN' id.

        - 'last' -> the most recent WRITE row id in this turn, else the last visible row
        - 'wN' -> must be visible
        - 'N' -> the Nth visible row on the page (1-indexed)
        """
        raw = target.strip().lower()
        if raw == "last":
            if self.current_turn_write_row_ids:
                return self.current_turn_write_row_ids[-1]
            if self.visible_rows:
                return self.visible_rows[-1]["row_id"]
            return None

        # Check 'wN' pattern
        if re.match(r"^w\d+$", raw):
            target_id = raw
            # Check if visible or written in turn
            for r in self.visible_rows:
                if r["row_id"].lower() == target_id:
                    return r["row_id"]
            if target_id in [wid.lower() for wid in self.current_turn_write_row_ids]:
                return target_id
            return None  # unknown row id: drop the emphasize

        # Check numeric 'N' (1-indexed)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(self.visible_rows):
                return self.visible_rows[idx]["row_id"]
            if 0 <= idx < len(self.current_turn_write_row_ids):
                return self.current_turn_write_row_ids[idx]
            return None


        return None

    def export_row_mirror(self) -> RowMirror:
        return RowMirror(
            page_id=self.page_id,
            rows=self.visible_rows,
            rows_remaining=self.rows_remaining,
            next_row_number=self.allocator.next_row_number if self.allocator is not None else self.next_row_number,
        )
