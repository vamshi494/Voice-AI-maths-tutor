# app/state_machine/page_ledger.py
"""PageLedger: the server's copy of what is drawn per sub-page.

Only acked ops count as visible; a PAGE_BREAK opens the next sub-page before its ops are
recorded. Sub-page ids match the client's BoardLayout (`{root}` then `{root}_p{n}`).
"""
from dataclasses import dataclass, field
from typing import Any

from app.contracts.board_ops import BoardOp, Step
from app.contracts.diagram import VerifiedDiagram
from app.contracts.messages import PageTurned, SnapshotPage, SnapshotSubPage


@dataclass
class LedgerOp:
    op: BoardOp
    acked: bool = False


@dataclass
class SubPage:
    sub_id: str
    ops: list[LedgerOp] = field(default_factory=list)


class PageLedger:
    def __init__(self) -> None:
        self.pages: dict[str, list[SubPage]] = {}
        self.client_sub: dict[str, str] = {}      # root page_id -> sub id the student sees

    def _subs(self, page_id: str) -> list[SubPage]:
        subs = self.pages.get(page_id)
        if subs is None:
            subs = [SubPage(sub_id=page_id)]
            self.pages[page_id] = subs
            self.client_sub.setdefault(page_id, page_id)
        return subs

    def current_sub(self, page_id: str) -> str:
        self._subs(page_id)
        return self.client_sub.get(page_id, page_id)

    def on_published(self, page_id: str, step: Step) -> None:
        """Record a published step's ops; a PAGE_BREAK (always ops[0]) opens the next sub-page."""
        subs = self._subs(page_id)
        for op in step.ops:
            if op.kind == "PAGE_BREAK":
                new_sub = f"{page_id}_p{len(subs) + 1}"
                subs.append(SubPage(sub_id=new_sub))
                self.client_sub[page_id] = new_sub
            subs[-1].ops.append(LedgerOp(op=op))

    def on_acked(self, op_ids: list[str]) -> None:
        ids = set(op_ids or [])
        if not ids:
            return
        for subs in self.pages.values():
            for sub in subs:
                for ledger_op in sub.ops:
                    if ledger_op.op.op_id in ids:
                        ledger_op.acked = True

    def on_page_turned(self, r: PageTurned) -> None:
        """Client page turn (overflow or applied PAGE_BREAK) keeps server and client in step.

        Overflow splits the sub-page at `at_op_id`: that op and every later op move to the new
        sub-page (the client re-placed them there). Both causes set the visible sub-page.
        """
        subs = self._subs(r.root_page_id)
        current = self.client_sub.get(r.root_page_id, r.root_page_id)
        if r.from_sub_id != current:
            return                                    # a stale turn report: ignore
        target = next((s for s in subs if s.sub_id == r.to_sub_id), None)
        if target is None:
            target = SubPage(sub_id=r.to_sub_id)
            subs.append(target)
        if r.cause == "overflow" and r.at_op_id:
            source = next((s for s in subs if s.sub_id == r.from_sub_id), None)
            if source is not None:
                split_at = next(
                    (i for i, lo in enumerate(source.ops) if lo.op.op_id == r.at_op_id), None)
                if split_at is not None:
                    target.ops = source.ops[split_at:] + target.ops
                    source.ops = source.ops[:split_at]
        self.client_sub[r.root_page_id] = r.to_sub_id

    def rows_for_prompt(self, page_id: str) -> list[dict[str, str]]:
        """Acked WRITE rows of the sub-page the student sees: [{"row_id","text"}]."""
        current = self.current_sub(page_id)
        for sub in self.pages.get(page_id, []):
            if sub.sub_id == current:
                return [
                    {"row_id": lo.op.row_id or "", "text": lo.op.text or ""}
                    for lo in sub.ops
                    if lo.acked and lo.op.kind == "WRITE" and lo.op.row_id
                ]
        return []

    def acked_ops(self, page_id: str) -> list[BoardOp]:
        """Every acked op of the page, sub-pages in order (used for PageRestore)."""
        return [lo.op for sub in self.pages.get(page_id, []) for lo in sub.ops if lo.acked]

    def acked_ops_all(self) -> list[BoardOp]:
        """Every acked op of every page, for the sticky reveal-state scan."""
        return [lo.op for subs in self.pages.values() for sub in subs for lo in sub.ops if lo.acked]

    def snapshot_page(self, page_id: str) -> SnapshotPage:
        return SnapshotPage(
            page_id=page_id,
            sub_pages=[
                SnapshotSubPage(sub_id=sub.sub_id, ops=[lo.op for lo in sub.ops if lo.acked])
                for sub in self.pages.get(page_id, [])
            ],
        )

    def to_rows(self) -> list[dict]:
        """Persistence view: one row per op with its page, sub-page and acked flag."""
        out: list[dict] = []
        for page_id, subs in self.pages.items():
            for sub in subs:
                for lo in sub.ops:
                    out.append({
                        "page_id": page_id,
                        "sub_id": sub.sub_id,
                        "op": lo.op.model_dump(by_alias=True, exclude_none=True),
                        "acked": lo.acked,
                    })
        return out

    def to_pages(self, *, board_id: str, lesson_id: str | None, page_index: int,
                 diagrams: dict[str, VerifiedDiagram | None] | None = None,
                 commits: dict[str, Any] | None = None) -> list[dict]:
        """Persistence view: one dict per sub-page, ops keep their acked flags.

        A sub-page's title is the PAGE_BREAK that opened it; `diagrams` maps a root
        page id to its verified figure; `commits` maps a root page id to its built
        PageCommit (the notes drawer reads it back).
        """
        out: list[dict] = []
        for root, subs in self.pages.items():
            for i, sub in enumerate(subs):
                title = next((op.op.page_title for op in sub.ops
                              if op.op.kind == "PAGE_BREAK" and op.op.page_title), "")
                out.append({
                    "page_id": sub.sub_id,
                    "board_id": board_id,
                    "lesson_id": lesson_id or "",
                    "root_page_id": root,
                    "page_index": page_index,
                    "sub_index": i + 1,
                    "title": title or "",
                    "diagram": (diagrams or {}).get(root),
                    "layout": (commits or {}).get(root),
                    "ops": [{"op": lo.op.model_dump(by_alias=True, exclude_none=True),
                             "acked": lo.acked} for lo in sub.ops],
                })
        return out

    def restore_pages(self, pages: list[dict]) -> None:
        """Rebuild the ledger from persisted sub-page rows.

        Rows must be ordered root/page/sub (load_pages does); the last row of a root
        is the sub-page the student last saw.
        """
        self.pages = {}
        self.client_sub = {}
        for page in pages:
            root = page.get("root_page_id") or page["page_id"]
            subs = self._subs(root)
            sub_id = page["page_id"]
            target = next((s for s in subs if s.sub_id == sub_id), None)
            if target is None:
                target = SubPage(sub_id=sub_id)
                subs.append(target)
            target.ops = [
                LedgerOp(op=BoardOp.model_validate(entry["op"]), acked=bool(entry.get("acked")))
                for entry in page.get("ops") or []
            ]
            self.client_sub[root] = sub_id
