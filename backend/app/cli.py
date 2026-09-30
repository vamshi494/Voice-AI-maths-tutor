# app/cli.py
"""Offline test runner: input question -> stdout board ops + step speech."""
import argparse
import asyncio
import json
import sys
from uuid import uuid4
from app.agents.graph import run_turn_stream
from app.contracts.agent_state import AgentState, TurnRequest
from app.tutor.board_rows import BoardRowTracker


async def main_async(question: str):
    print(f"\n========================================")
    print(f"AI Math Tutor — Offline Runner")
    print(f"Question: {question}")
    print(f"========================================\n")

    turn_id = str(uuid4())
    req = TurnRequest(
        kind="lesson",
        turn_id=turn_id,
        generation=1,
        question=question,
    )

    state = AgentState(
        session_id=f"sess-{uuid4().hex[:8]}",
        board_id=f"board-{uuid4().hex[:8]}",
        user_id="cli-user",
        generation=1,
        active_turn_id=turn_id,
        active_turn_kind="lesson",
    )

    tracker = BoardRowTracker()

    print("[1] Executing Turn Pipeline...\n")

    step_count = 0
    async for chunk in run_turn_stream(req, row_tracker=tracker, agent_state=state):
        step_count += 1
        print(f"\n--- STEP {step_count} ---")
        if state.steps_sent:
            latest_step = state.steps_sent[-1]
            print(f"[Spoken text]: {latest_step.spoken_text}")
            print(f"[Ops ({len(latest_step.ops)})]:")
            for op in latest_step.ops:
                print(f"  * {op.kind} (at word {op.at_word}) -> {op.text or op.entity_id or op.emphasize_row_id or op.duration_ms}")
        print(f"[TTS Stream]: {chunk.strip()}")

    print(f"\n========================================")
    print(f"Completed turn {turn_id} with {len(state.steps_sent)} steps.")
    print(f"Visible board rows ({len(tracker.visible_rows)}):")
    for r in tracker.visible_rows:
        print(f"  {r['row_id']}: {r['text']}")
    print(f"========================================\n")


def main():
    parser = argparse.ArgumentParser(description="AI Math Tutor offline test runner")
    parser.add_argument("question", nargs="?", default="Find the roots of x^2 - 5x + 6 = 0", help="Math question to run")
    args = parser.parse_args()

    asyncio.run(main_async(args.question))


if __name__ == "__main__":
    main()
