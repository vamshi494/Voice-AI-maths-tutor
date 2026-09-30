# app/tutor/plan_validation.py
"""Deterministic TurnPlan validation, with an explicit severity model.

FATAL (plan rejected, lane fails) — only issues that make a TAUGHT NUMBER untrustworthy:
unknown_unresolved, non_finite, arithmetic_mismatch, sign_mismatch.

REPAIRED IN PLACE (reported as warnings, plan kept):
  question_mismatch   -> plan.question overwritten with the exact submitted question
  dangling_dependency -> the dangling id is dropped from depends_on
  duplicate_id        -> the later duplicate is renamed id_2, id_3, ...
  limit_exceeded      -> truncated

Only fatal issues reject a plan; a model that re-punctuates the question keeps its plan --
and the scene, because the scene gate needs a plan.
"""
import math
import re

from app.contracts.turn_plan import FATAL_PLAN_CODES, PlanIssue, Quantity, TurnPlan
from app.tutor.arithmetic import eval_expr_part, normalize_source_text


def is_fatal(issues: list[PlanIssue]) -> bool:
    return any(i.code in FATAL_PLAN_CODES for i in issues)


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip()


def validate_turn_plan(plan: TurnPlan, expected_question: str) -> list[PlanIssue]:
    issues: list[PlanIssue] = []

    # 1. question_mismatch (repaired: the submitted question is authoritative)
    if _norm(plan.question) != _norm(expected_question):
        if plan.question.strip():
            issues.append(PlanIssue(code="question_mismatch", path="question",
                                    message="plan question differed; replaced with submitted question"))
    plan.question = expected_question

    # 2. duplicate_id (repaired by renaming the later duplicate)
    seen: dict[str, int] = {}
    for bucket_name in ("givens", "derived"):
        for idx, q in enumerate(getattr(plan, bucket_name)):
            if not q.id:
                q.id = f"q_{bucket_name[0]}{idx}"
            if q.id in seen:
                seen[q.id] += 1
                new_id = f"{q.id}_{seen[q.id]}"
                issues.append(PlanIssue(code="duplicate_id", path=f"{bucket_name}[{idx}].id",
                                        message=f"duplicate id '{q.id}' renamed to '{new_id}'"))
                q.id = new_id
            else:
                seen[q.id] = 1
    seen_u: set[str] = set()
    uniq_unknowns = []
    for u in plan.unknowns:
        if u.id in seen_u:
            issues.append(PlanIssue(code="duplicate_id", path="unknowns", message=f"duplicate unknown '{u.id}' dropped"))
            continue
        seen_u.add(u.id)
        uniq_unknowns.append(u)
    plan.unknowns = uniq_unknowns

    all_quantities: list[Quantity] = plan.givens + plan.derived
    all_ids = {q.id for q in all_quantities}

    # 3. unknown_unresolved (FATAL): every requested unknown needs a FINITE derived value
    derived_by_key: dict[str, Quantity] = {}
    for d in plan.derived:
        derived_by_key[d.id] = d
        if d.symbol:
            derived_by_key.setdefault(d.symbol, d)
    for idx, u in enumerate(plan.unknowns):
        d = derived_by_key.get(u.id) or (derived_by_key.get(u.symbol) if u.symbol else None)
        if d is None:
            issues.append(PlanIssue(code="unknown_unresolved", path=f"unknowns[{idx}]",
                                    message=f"Unknown '{u.id}' (symbol '{u.symbol}') has no derived quantity"))
        elif d.value is None:
            issues.append(PlanIssue(code="non_finite", path=f"unknowns[{idx}]",
                                    message=f"Unknown '{u.id}' is derived as '{d.id}' but has no numeric value"))

    # 4. dangling_dependency (repaired by dropping the dangling id)
    for q in all_quantities:
        kept = [d for d in q.depends_on if d in all_ids]
        if len(kept) != len(q.depends_on):
            dropped = sorted(set(q.depends_on) - set(kept))
            issues.append(PlanIssue(code="dangling_dependency", path=f"{q.id}.depends_on",
                                    message=f"dropped missing dependencies {dropped}"))
            q.depends_on = kept

    # 5. sign_mismatch (FATAL) -- only checkable when a value exists
    for q in all_quantities:
        if not q.sign or q.value is None or q.sign == "unsigned":
            continue
        v = q.value
        bad = (q.sign == "positive" and v <= 1e-12) or (q.sign == "negative" and v >= -1e-12) or (
            q.sign == "zero" and abs(v) > 1e-12)
        if bad:
            issues.append(PlanIssue(code="sign_mismatch", path=f"{q.id}.sign",
                                    message=f"Declared sign '{q.sign}' disagrees with value {v}"))

    # 6. arithmetic_mismatch (FATAL): any evaluable part of sourceText vs the declared value
    var_map: dict[str, float] = {}
    for q in all_quantities:
        if q.value is None:
            continue
        var_map[q.id] = q.value
        if q.symbol:
            var_map[q.symbol] = q.value
    for q in all_quantities:
        if not q.source_text or q.value is None:
            continue
        normalized = normalize_source_text(q.source_text)
        if "solve" in normalized.lower() or "=>" in normalized:
            continue
        for part in normalized.split("="):
            part_str = part.strip()
            if (q.symbol and re.search(r"\b" + re.escape(q.symbol) + r"\b", part_str)) or (
                    q.id and re.search(r"\b" + re.escape(q.id) + r"\b", part_str)):
                continue
            ev = eval_expr_part(part_str, var_map)
            if ev is None:
                continue
            tol = max(1e-4, 1e-4 * abs(q.value))
            if abs(ev - q.value) > tol:
                issues.append(PlanIssue(code="arithmetic_mismatch", path=f"{q.id}.source_text",
                                        message=f"'{part_str}' = {ev:g} but declared value is {q.value:g}"))

    # 7. limit_exceeded (never fatal; truncate)
    if len(plan.qualitative_claims) > 8:
        issues.append(PlanIssue(code="limit_exceeded", path="qualitative_claims", message="truncated to 8"))
        plan.qualitative_claims = plan.qualitative_claims[:8]
    if len(plan.assumptions) > 6:
        issues.append(PlanIssue(code="limit_exceeded", path="assumptions", message="truncated to 6"))
        plan.assumptions = plan.assumptions[:6]
    for q in all_quantities:
        if q.source_text and len(q.source_text) > 180:
            issues.append(PlanIssue(code="limit_exceeded", path=f"{q.id}.source_text", message="truncated to 180"))
            q.source_text = q.source_text[:180]

    # non-finite guard for any value that slipped through
    for q in all_quantities:
        if q.value is not None and not math.isfinite(q.value):
            issues.append(PlanIssue(code="non_finite", path=f"{q.id}.value", message="non-finite value"))
    return issues
