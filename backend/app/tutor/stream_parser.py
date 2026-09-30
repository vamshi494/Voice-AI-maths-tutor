# app/tutor/stream_parser.py
import math
import re
from app.contracts.board_ops import (
    DOUBT_ALLOWED_OPS,
    LESSON_ALLOWED_OPS,
    PROMPT_FORBIDDEN_TAGS,
    BoardOp,
    Step,
)
from app.contracts.diagram import VerifiedDiagram
from app.observability import log_event
from app.tutor.board_rows import BoardRowTracker

STEP_REGEX = re.compile(r"\[STEP\](?P<content>.*?)(?:\[/STEP\]|(?=\[STEP\]))", re.DOTALL)
TAG_REGEX = re.compile(r"\[(?P<name>[A-Z_]+)(?::(?P<arg>[^\]]*))?\]")

FOCUS_CAP = 2

JARGON_WORDS = (
    "TurnPlan", "SymPy", "LangGraph", "SceneDocument", "Pydantic",
    "compiler", "JSON", "schema", "planner",
)


_LATEX_SYMBOLS = {
    "theta": "θ", "alpha": "α", "beta": "β", "gamma": "γ", "pi": "π", "phi": "φ",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈",
    "circ": "°", "degree": "°", "pm": "±", "div": "÷", "cong": "≅", "sim": "~",
    "perp": "⊥", "therefore": "∴", "because": "∵",
}
_LATEX_FUNCS = ("sin", "cos", "tan", "cot", "sec", "cosec", "csc", "log")


def scrub_board_text(text: str) -> str:
    """Board-side LaTeX cleanup: symbols, not words.

    Handles outline block text such as `\\sin\\theta=\\frac{\\text{opposite}}{\\text{hypotenuse}}`:
    \\text{} inside \\frac, Greek letters and \\sin all become canvas-safe symbols."""
    text = text.replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    text = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", text)
    text = text.replace("\\left", "").replace("\\right", "")
    for fn in _LATEX_FUNCS:
        text = re.sub(rf"\\{fn}(?![a-zA-Z])\s*", f"{fn} ", text)
    for name, sym in _LATEX_SYMBOLS.items():
        text = re.sub(rf"\\{name}(?![a-zA-Z])", sym, text)
    text = re.sub(r"\^\{?°\}?", "°", text)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√\1", text)
    text = text.replace("\\times", "×").replace("\\cdot", "·")
    text = text.replace("^2", "²").replace("^3", "³")
    text = text.replace("\\angle", "∠").replace("\\triangle", "△").replace("\\parallel", "||")
    text = text.replace("$", "")
    text = re.sub(r"\\[a-zA-Z]+", "", text)       # any command left over is markup, not maths
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\bw\d+\b", "", text)
    text = text.replace("∥", "||").replace("‖", "||")
    return " ".join(text.split())


def normalize_words(text: str) -> list[str]:
    """Word normalization:

    1. lowercase
    2. strip <...> SSML
    3. replace ’ with '
    4. remove every character except letters, digits and '
    5. split on whitespace
    6. drop empty tokens
    """
    s = text.lower()
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("’", "'")
    # REPLACE with a space (identical to frontend normalizeWord), so "side-length" splits
    # the same way on both sides and hyphenated/decimal words never desync the matcher.
    s = re.sub(r"[^a-z0-9']", " ", s)
    return [t for t in s.split() if t]


_ENTITY_LETTERS_RE = re.compile(r"_([A-Z]{1,4})$")
_SINGLE_LETTER_CUES = {"pt": ("point", "vertex"), "ang": ("angle",), "point": ("point", "vertex")}


def entity_mention_end(words: list[str], entity_id: str, start: int = 0) -> int | None:
    """Index just AFTER the first spoken mention of a figure entity at or after `start`
    ("angle a" for ang_BAC, "a b c" / "abc" for tri_ABC, "point p" for pt_P), else None.

    A trailing [FOCUS:id] fires as the entity is named rather than on the step's last
    word; this helper finds that mention. A lone letter is only matched after a cue
    word ("a" is also an article)."""
    m = _ENTITY_LETTERS_RE.search(entity_id or "")
    if not m:
        return None
    letters = [c.lower() for c in m.group(1)]
    prefix = entity_id[: m.start()].lower()
    patterns: list[list[str]] = []
    if len(letters) >= 2:
        patterns += [letters, ["".join(letters)]]
    if prefix == "ang" and len(letters) == 3:
        patterns.append(["angle", letters[1]])
    if len(letters) == 1:
        patterns += [[cue, letters[0]] for cue in _SINGLE_LETTER_CUES.get(prefix, ())]
    best: int | None = None
    for pat in patterns:
        k = len(pat)
        for i in range(max(0, start), len(words) - k + 1):
            if words[i:i + k] == pat:
                end = i + k
                best = end if best is None else min(best, end)
                break
    return best


def repair_header_only_text_tags(buffer: str) -> str:
    """Repair an unwrapped [WRITE] tag: [WRITE] text -> [WRITE:text]."""
    return re.sub(r"\[(WRITE)\]\s*([^\r\n\]]+)", r"[\1:\2]", buffer)


def sanitize_spoken_text(text: str) -> str:
    """Clean spoken text for natural, human-sounding TTS delivery.

    1. Strip leading board layout prefixes like 'Given:', 'Goal:', 'Step 1:'
    2. Expand common geometry symbols to natural spoken words for TTS
    3. Scrub accidental spoken row IDs (e.g. 'w1', 'line w1')
    4. Collapse whitespace
    """
    if not text:
        return text
    # Strip leading board layout prefixes (e.g. "Given: ", "Goal: ", "Step 1: ")
    text = re.sub(r"^(?:Given|Goal|Step\s*\d+)\s*:\s*", "", text, flags=re.IGNORECASE)
    # LaTeX -> spoken words, then any leftover markup characters go.
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1 by \2", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"root \1", text)
    text = text.replace("^2", " squared ").replace("^3", " cubed ")
    text = re.sub(r"[\\$^_{}]", " ", text)
    # Jargon must never be read aloud; log the leak for prompt tuning.
    for word in JARGON_WORDS:
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        if pattern.search(text):
            log_event("jargon_leak", word=word)
            text = pattern.sub("", text)
    # Expand geometry symbols to spoken words
    text = re.sub(r"[△Δ]\s*([A-Za-z]+)", r"triangle \1", text)
    text = re.sub(r"∠\s*([A-Za-z]+)", r"angle \1", text)
    text = text.replace("∼", " is similar to ")
    text = text.replace("≅", " is congruent to ")
    text = text.replace("∥", " is parallel to ")
    text = text.replace("||", " is parallel to ")
    text = text.replace("⊥", " is perpendicular to ")
    # Expand fractions
    text = text.replace("½", " half ")
    text = text.replace("⅓", " one third ")
    text = text.replace("¼", " one quarter ")
    text = text.replace("¾", " three quarters ")
    text = re.sub(r"\b1/2\b", " half ", text)
    text = re.sub(r"\b1/3\b", " one third ", text)
    text = re.sub(r"\b1/4\b", " one quarter ", text)
    text = re.sub(r"\b3/4\b", " three quarters ", text)
    # Expand math operators
    text = text.replace("×", " times ")
    text = text.replace("÷", " divided by ")
    text = text.replace("=", " equals ")
    text = text.replace("≠", " is not equal to ")
    text = text.replace("≤", " is less than or equal to ")
    text = text.replace("≥", " is greater than or equal to ")
    text = text.replace("±", " plus or minus ")
    text = text.replace("²", " squared ")
    text = text.replace("³", " cubed ")
    text = text.replace("√", " square root of ")
    text = text.replace("°", " degrees ")
    text = text.replace("π", " pi ")
    text = text.replace("⇒", " implies ")
    text = text.replace("⇔", " if and only if ")
    text = text.replace("⊂", " is a subset of ")
    # Scrub accidental spoken row IDs
    text = re.sub(r"\b(?:the\s+given\s+)?(?:row|line\s+)?w\d+\b", "the given line", text, flags=re.IGNORECASE)
    text = re.sub(r"\bw\d+\b", "that step", text, flags=re.IGNORECASE)
    # Uppercase labels of 2-4 letters are spoken letter by letter ("ABC" -> "A B C").
    # Runs after the symbol expansions so "ΔABC" also becomes "triangle A B C".
    text = re.sub(r"\b([A-Z]{2,4})\b", lambda m: " ".join(m.group(1)), text)

    leading_ws = " " if text[:1].isspace() else ""
    trailing_ws = " " if text[-1:].isspace() else ""
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    return f"{leading_ws}{collapsed}{trailing_ws}"


class StreamParser:
    def __init__(
        self,
        turn_id: str,
        generation: int,
        turn_kind: str = "lesson",
        row_tracker: BoardRowTracker | None = None,
        diagram: VerifiedDiagram | None = None,
    ) -> None:
        self.turn_id = turn_id
        self.generation = generation
        self.turn_kind = turn_kind
        self.row_tracker = row_tracker or BoardRowTracker()
        self.diagram = diagram
        self.buffer = ""
        self.step_index = 0
        self.raw_response = ""
        self._carried_ops: list[BoardOp] = []       # tag-only step ops awaiting the next step

    def append(self, chunk: str) -> list[tuple[Step, str]]:
        """Append stream chunk and return list of (Step, tts_text) completed so far."""
        self.buffer += chunk
        self.raw_response += chunk
        self.buffer = repair_header_only_text_tags(self.buffer)
        steps_out = []

        while True:
            match = STEP_REGEX.search(self.buffer)
            if not match:
                break
            content = match.group("content")
            # Remove this step block from buffer
            self.buffer = self.buffer[match.end():]
            step, tts_text = self._build_step(content)
            if step is not None:
                steps_out.append((step, tts_text))
                self.step_index += 1

        return steps_out

    def finish(self) -> list[tuple[Step, str]]:
        """Finish stream; drain remaining buffer, handling both closed and unclosed steps."""
        steps_out = []
        # First drain any matched steps using regex
        while True:
            match = STEP_REGEX.search(self.buffer)
            if not match:
                break
            content = match.group("content")
            self.buffer = self.buffer[match.end():]
            step, tts_text = self._build_step(content)
            if step is not None:
                steps_out.append((step, tts_text))
                self.step_index += 1

        trailing = self.buffer.strip()
        if trailing:
            if trailing.startswith("[STEP]"):
                trailing = trailing[6:]
            step, tts_text = self._build_step(trailing)
            if step is not None:
                steps_out.append((step, tts_text))
                self.step_index += 1
        if self._carried_ops:
            log_event("tag_dropped", reason="tag_only_tail", count=len(self._carried_ops))
            self._carried_ops = []
        self.buffer = ""
        return steps_out

    def _resolve_alias(self, raw_id: str, valid: set[str]) -> str | None:
        """Resolve an entity id before validation — exact id, then an alias_map key
        (LLM id, entity label), then the reversed two-letter segment name."""
        if not self.diagram:
            return None
        if raw_id in valid:
            return raw_id
        aliases = self.diagram.alias_map or {}
        resolved = aliases.get(raw_id)
        if resolved and resolved in valid:
            return resolved
        if len(raw_id) == 2 and raw_id[0] != raw_id[1]:
            resolved = aliases.get(raw_id[::-1])
            if resolved and resolved in valid:
                return resolved
        return None

    def _resolve_focus_id(self, raw_id: str) -> str | None:
        """Resolve a FOCUS id against the anchor and reveal-group ids."""
        if not self.diagram:
            return None
        anchors = {a.id for a in self.diagram.anchors}
        reveals = {r.target_id for r in self.diagram.reveals} if self.diagram.reveals else set()
        return self._resolve_alias(raw_id, anchors | reveals)

    def _build_step(self, content: str) -> tuple[Step | None, str]:
        # Strip any extraneous [STEP] or [/STEP] tags from content
        content = re.sub(r"\[/?STEP\]", "", content).strip()
        if not content:
            return None, ""

        # Auto-close any unclosed trailing tag at the end of content (e.g. "[WRITE:text" -> "[WRITE:text]")
        # This prevents unclosed tags from leaking into clean_text_segments / spoken_text if truncated.
        content = re.sub(r"(\[[A-Z_]+(?::[^\]]*)?)$", r"\1]", content)

        # Extract tags and calculate word counts
        tags_with_pos: list[tuple[int, str, str | None]] = []
        for m in TAG_REGEX.finditer(content):
            name = m.group("name")
            arg = m.group("arg")
            tags_with_pos.append((m.start(), name, arg))

        # Build spoken_text and tts_text
        # Remove tags for spoken_text
        clean_text_segments = []
        tts_text_segments = []
        last_end = 0

        for m in TAG_REGEX.finditer(content):
            chunk = sanitize_spoken_text(content[last_end:m.start()])
            clean_text_segments.append(chunk)
            tts_text_segments.append(chunk)
            # If PAUSE tag, add SSML break to tts_text only
            if m.group("name") == "PAUSE":
                ms = 0
                try:
                    ms = min(3000, max(0, int(m.group("arg") or 0)))
                except ValueError:
                    pass
                if ms > 0:
                    # No SSML: ellipses give the TTS the same short silence.
                    tts_text_segments.append(" … " * min(3, math.ceil(ms / 700)))
            last_end = m.end()

        tail = sanitize_spoken_text(content[last_end:])
        clean_text_segments.append(tail)
        tts_text_segments.append(tail)

        spoken_text = " ".join("".join(clean_text_segments).split())
        tts_text = " ".join("".join(tts_text_segments).split())

        words = normalize_words(spoken_text)

        # Convert tags to ops
        ops: list[BoardOp] = []
        allowed_ops = DOUBT_ALLOWED_OPS if self.turn_kind == "doubt" else LESSON_ALLOWED_OPS

        # First pass: the first PAGE_BREAK/NEW_PAGE of the step is applied before
        # every other op, after a tracker reset, so WRITEs of this step get post-break row ids.
        page_break_seen = False
        for idx, (pos, name, arg) in enumerate(tags_with_pos):
            if name not in ("PAGE_BREAK", "NEW_PAGE"):
                continue
            if "PAGE_BREAK" not in allowed_ops:
                log_event("tag_dropped", reason="not_allowed_in_turn_kind", tag=name, kind=self.turn_kind)
                continue
            if page_break_seen:
                log_event("tag_dropped", reason="duplicate_page_break", tag=name)
                continue
            page_break_seen = True
            page_title = (arg or "").strip() or None
            self.row_tracker.reset_for_new_page()
            ops.append(
                BoardOp(
                    op_id=f"{self.turn_id}:{self.step_index}:{idx}",
                    kind="PAGE_BREAK",
                    at_word=0,
                    page_title=page_title,
                )
            )
            log_event("page_break_parsed", title=page_title)

        focus_count = 0
        for idx, (pos, name, arg) in enumerate(tags_with_pos):
            # Check forbidden tags
            if any(name.startswith(fb) or name == fb for fb in PROMPT_FORBIDDEN_TAGS):
                log_event("tag_dropped", reason="forbidden", tag=name, arg=arg)
                continue

            if name in ("PAGE_BREAK", "NEW_PAGE"):
                continue                             # handled in the first pass

            if name not in allowed_ops:
                log_event("tag_dropped", reason="not_allowed_in_turn_kind", tag=name, kind=self.turn_kind)
                continue

            # Calculate at_word (number of spoken words before this tag position)
            text_before = content[:pos]
            clean_before = TAG_REGEX.sub("", text_before)
            words_before = normalize_words(sanitize_spoken_text(clean_before))
            at_word = len(words_before)

            op_id = f"{self.turn_id}:{self.step_index}:{idx}"

            if name == "WRITE":
                text = scrub_board_text((arg or "").strip())
                if not text:
                    log_event("tag_dropped", reason="empty_write", tag=name)
                    continue
                row_id = self.row_tracker.allocate_row_id()
                ops.append(
                    BoardOp(
                        op_id=op_id,
                        kind="WRITE",
                        at_word=at_word,
                        text=text,
                        row_id=row_id,
                    )
                )

            elif name == "PAUSE":
                try:
                    ms = min(3000, max(0, int(arg or 0)))
                except ValueError:
                    ms = 500
                ops.append(
                    BoardOp(
                        op_id=op_id,
                        kind="PAUSE",
                        at_word=at_word,
                        duration_ms=ms,
                    )
                )

            elif name == "FOCUS":
                if not self.diagram:
                    log_event("tag_dropped", reason="no_diagram_for_focus", tag=name)
                    continue
                parts = (arg or "").split("|")
                raw_id = parts[0].strip()
                mode = parts[1].strip() if len(parts) > 1 else None
                if mode not in ("spotlight", "pulse"):
                    mode = "outline"

                # Resolve through alias_map (exact → alias → reversed pair) before validation
                entity_id = self._resolve_focus_id(raw_id)
                if entity_id is None:
                    log_event("focus_id_dropped", entity_id=raw_id)
                    continue

                if focus_count >= FOCUS_CAP:
                    log_event("tag_dropped", reason="focus_cap", entity_id=entity_id)
                    continue
                focus_count += 1

                ops.append(
                    BoardOp(
                        op_id=op_id,
                        kind="FOCUS",
                        at_word=at_word,
                        entity_id=entity_id,
                        focus_mode=mode,
                    )
                )

            elif name == "EMPHASIZE":
                target = (arg or "last").strip()
                row_id = self.row_tracker.resolve_emphasize(target)
                if not row_id:
                    log_event("tag_dropped", reason="unresolved_emphasize", target=target)
                    continue
                ops.append(
                    BoardOp(
                        op_id=op_id,
                        kind="EMPHASIZE",
                        at_word=at_word,
                        emphasize_row_id=row_id,
                    )
                )

            elif name == "ANNOTATE":
                if self.turn_kind == "doubt":
                    log_event("tag_dropped", reason="annotate_forbidden_in_doubt")
                    continue
                raw_id = (arg or "").strip()
                if not self.diagram or not self.diagram.deferred_annotations:
                    log_event("tag_dropped", reason="no_deferred_annotations")
                    continue
                deferred_ids = {da.entity_id for da in self.diagram.deferred_annotations}
                # Lenient contract: resolve shorthand ids (LLM id, letter pair) like FOCUS.
                entity_id = self._resolve_alias(raw_id, deferred_ids)
                if entity_id is None:
                    log_event("tag_dropped", reason="entity_not_in_deferred", entity_id=raw_id)
                    continue
                ops.append(
                    BoardOp(
                        op_id=op_id,
                        kind="ANNOTATE",
                        at_word=at_word,
                        entity_id=entity_id,
                    )
                )

        # Tag-only step: no spoken words -> carry its ops into the next step.
        if not words:
            if ops:
                self._carried_ops.extend(ops)
            return None, ""
        if self._carried_ops:
            merged = [op.model_copy(update={"at_word": 0}) for op in self._carried_ops] + ops
            ops = [op.model_copy(update={"op_id": f"{self.turn_id}:{self.step_index}:{i}"})
                   for i, op in enumerate(merged)]
            self._carried_ops = []

        # Ops are placed at their natural word offset in the text.
        # For trailing WRITE ops (placed after the spoken sentence), pace them so handwriting
        # begins while the sentence is being spoken rather than after speech terminates.
        # Invariant: Never invert order (always >= prior op's at_word) and never precede word 0.
        n = len(words)
        if n > 0:
            paced_ops = []
            prior_at = 0
            for op in ops:
                at = op.at_word
                if op.kind == "WRITE" and at >= n:
                    write_words = len((op.text or "").split())
                    lead = max(2, min(n // 2, write_words))
                    paced_at = max(prior_at, n - lead)
                    at = min(n, max(0, paced_at))
                elif op.kind == "FOCUS" and at >= n and op.entity_id:
                    # A trailing highlight fires as its entity is named, not on the last
                    # word (where the next step's first FOCUS fades it ~250 ms later).
                    end = entity_mention_end(words, op.entity_id, prior_at)
                    if end is None:
                        end = entity_mention_end(words, op.entity_id, 0)
                    at = n if end is None else min(n, max(prior_at, end))
                elif at > n:
                    at = n
                paced_ops.append(op if at == op.at_word else op.model_copy(update={"at_word": at}))
                prior_at = max(prior_at, at)
            ops = paced_ops

        step = Step(
            turn_id=self.turn_id,
            generation=self.generation,
            step_index=self.step_index,
            spoken_text=spoken_text,
            words=words,
            ops=ops,
        )
        return step, tts_text
