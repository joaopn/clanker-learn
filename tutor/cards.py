"""Exercises and quick questions: cards a tutor sets in a lesson, which the learner acts on in
place.

New for clanker-learn, in the shape of gaggiclanker's proposal cards (the maintainer's project,
AGPL like this one): a card is stored, sent as an event and kept with its message; the
learner's action is applied at once and told to the tutor with their next message
(gaggiclanker/web/src/components/chat/tellAgent.ts), or sent as a message of its own whose
text the system composed, never counted as the learner's own words.

An exercise is `{"title", "steps": [text], "check": {"command", "expect_exit"}}`; its states are
`unchecked`, `passed`, `failed`, and its result is the last check. A question is `{"question",
"options": [text], "answer": index, "explanation"}`; its states are `open` and `answered`, and
its result is the learner's choice. Every check and every answer is evidence (knowledge.py).
"""
import json

import knowledge
import store

LETTERS = "ABCDEFGH"
OUTPUT_TOLD = 1500   # of a check's output, the end of it, in what the tutor is told


class CheckError(Exception):
    """A check that could not run (the sandbox is not reachable); the message says so."""


def letter(index):
    return LETTERS[index] if 0 <= index < len(LETTERS) else str(index)


def record_check(db, row, outcome, by, run_id, told):
    """A check's outcome (turn.run_check) stored as evidence and as the exercise's last check.
    `by` is "tutor" or "learner"; `told` is whether the tutor knows already."""
    spec = json.loads(row["spec_json"])
    lesson = store.lesson(db, row["lesson_id"])
    passed = not outcome["timed_out"] and outcome["exit_code"] == spec["check"]["expect_exit"]
    data = {"card": row["id"], "title": spec["title"], "command": spec["check"]["command"], "expect_exit": spec["check"]["expect_exit"],
            "exit_code": outcome["exit_code"], "output": outcome["output"], "output_cut": outcome["cut"], "timed_out": outcome["timed_out"],
            "seconds": outcome["seconds"], "passed": passed, "by": by}
    evidence_id = knowledge.add_evidence(db, "check", lesson["project"], lesson["created"], lesson["id"], run_id, data)
    result = dict(data, evidence=evidence_id, at=knowledge.evidence(db, evidence_id)["at"])
    return store.set_card(db, row["id"], "passed" if passed else "failed", result, told)


def record_answer(db, row, choice):
    """The learner's choice on a question card, stored as evidence; the card shows it, with the
    right answer and the explanation from now on."""
    spec = json.loads(row["spec_json"])
    lesson = store.lesson(db, row["lesson_id"])
    right = choice == spec["answer"]
    data = {"card": row["id"], "question": spec["question"], "options": spec["options"], "choice": choice, "choice_text": spec["options"][choice],
            "right": right, "right_choice": spec["answer"], "how": "card"}
    evidence_id = knowledge.add_evidence(db, "answer", lesson["project"], lesson["created"], lesson["id"], None, data)
    result = {"choice": choice, "right": right, "evidence": evidence_id, "at": knowledge.evidence(db, evidence_id)["at"]}
    return store.set_card(db, row["id"], "answered", result, False)


def check_words(result):
    if result["timed_out"]:
        return "it did not finish in time"
    return "it passed" if result["passed"] else "it failed"


def note(row):
    """What the tutor is told, with the learner's next message, of what they did on a card."""
    spec, result = json.loads(row["spec_json"]), json.loads(row["result_json"])
    if row["kind"] == "exercise":
        said = f"[clanker-learn: the learner pressed Check on the exercise “{spec['title']}” (exercise {row['id']}): {check_words(result)}"
        said += f" (exit {result['exit_code']}, evidence {result['evidence']})" if result["exit_code"] is not None else f" (evidence {result['evidence']})"
        if not result["passed"] and result["output"].strip():
            return said + ". The end of its output:\n" + result["output"][-OUTPUT_TOLD:] + "]"
        return said + ".]"
    verdict = "which is right" if result["right"] else f"which is wrong (the right one is {letter(spec['answer'])})"
    return (f"[clanker-learn: the learner answered your question “{spec['question']}” (question {row['id']}): {letter(result['choice'])} "
            f"“{spec['options'][result['choice']]}”, {verdict} (evidence {result['evidence']}).]")


def composed_text(row):
    """The learner's message the system writes for them when the page sends an action as a
    message of its own."""
    spec, result = json.loads(row["spec_json"]), json.loads(row["result_json"])
    if row["kind"] == "exercise":
        return f"I ran the check of “{spec['title']}”: {check_words(result)}."
    return f"I answered “{spec['question']}”: {letter(result['choice'])}, which is {'right' if result['right'] else 'wrong'}."


def transcript_lines(card):
    """A card in the lesson's transcript (card_out's view: a question's answer only once answered)."""
    if card["kind"] == "exercise":
        lines = [f"> **Card: exercise {card['id']}, “{card['title']}”** ({card['state']})", ">"]
        lines += [f"> {n}. {step}" for n, step in enumerate(card["steps"], 1)]
        lines.append(f"> Check: `{card['check']['command']}` (passes with exit {card['check']['expect_exit']})")
        if card["last_check"]:
            last = card["last_check"]
            lines.append(f"> Last check, by the {last['by']}: {check_words(last)} (exit {last['exit_code']}).")
        return lines
    lines = [f"> **Card: question {card['id']}** ({card['state']}): {card['question']}", ">"]
    lines += [f"> {letter(i)}. {option}" for i, option in enumerate(card["options"])]
    if card["answer"]:
        lines.append(f"> The learner chose {letter(card['answer']['choice'])}, which is {'right' if card['answer']['right'] else 'wrong'}. "
                     f"{card.get('explanation', '')}")
    return lines
