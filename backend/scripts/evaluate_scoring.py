"""Score a labelled resume set and report agreement / false-reject / consistency.

Dataset format (JSON array):

    [
      {"resume_text": "...", "expected": "shortlisted", "name": "optional"},
      {"resume_text": "...", "expected": "not_shortlisted"}
    ]

Run with a real key (GEMINI_API_KEY) against one job:

    python scripts/evaluate_scoring.py --job-id <uuid> --dataset labelled.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from app.agents.prompts import get_prompt
from app.core.exceptions import LLMNotConfiguredError
from app.db.session import SessionLocal
from app.schemas.llm import ScoreResult
from app.services.job_service import JobService
from app.utils.llm_factory import get_llm
from app.utils.redaction import redact_pii


def _job_block(job) -> str:  # noqa: ANN001
    return "\n".join(
        [
            f"Title: {job.title}",
            f"Required skills: {', '.join(job.required_skills) or 'not listed'}",
            f"Preferred skills: {', '.join(job.preferred_skills) or 'not listed'}",
            f"Minimum experience: {job.min_experience_years} year(s)",
            f"Education: {job.education or 'not specified'}",
            f"Location: {job.location or 'not specified'}",
            f"Description: {(job.description or '')[:1000]}",
        ]
    )


def _score(llm, prompt, job_block: str, text: str) -> int:  # noqa: ANN001
    user = f"<job>\n{job_block}\n</job>\n\n<resume>\n{text[:12000]}\n</resume>"
    result: ScoreResult = llm.invoke(
        ScoreResult,
        system=prompt.full_system,
        user=user,
        prompt_version=prompt.version,
    )
    return result.score


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    entries = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not entries:
        print("Dataset is empty", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        job = JobService(session).get(args.job_id)
    job_block = _job_block(job)
    prompt = get_prompt("screening")
    llm = get_llm()

    results = []
    for entry in entries:
        redacted = redact_pii(
            entry["resume_text"], known_name=entry.get("name")
        ).text
        scores = [_score(llm, prompt, job_block, redacted) for _ in range(args.runs)]
        decisions = [score >= job.min_score for score in scores]
        results.append(
            {
                "expected": entry["expected"],
                "scores": scores,
                "decisions": decisions,
                "mean": round(statistics.fmean(scores), 2),
            }
        )

    total = len(results)
    agree = sum(
        1
        for item in results
        if (item["mean"] >= job.min_score) == (item["expected"] == "shortlisted")
    )
    expected_short = [item for item in results if item["expected"] == "shortlisted"]
    false_rejects = sum(
        1 for item in expected_short if item["mean"] < job.min_score
    )
    consistent = sum(
        1 for item in results if len(set(item["decisions"])) == 1
    )

    summary = {
        "job_id": job.id,
        "min_score": job.min_score,
        "cases": total,
        "agreement": round(agree / total, 4),
        "false_reject_rate": round(false_rejects / len(expected_short), 4)
        if expected_short
        else 0.0,
        "three_run_consistency": round(consistent / total, 4),
        "results": results,
    }
    text = json.dumps(summary, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LLMNotConfiguredError as exc:
        print(f"LLM not configured: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
