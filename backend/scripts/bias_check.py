"""Check score sensitivity to swapped names/genders.

Dataset format (JSON array):

    [
      {"resume_text": "...", "variants": [
          {"name": "Asha Rao", "gender": "female"},
          {"name": "Ravi Kumar", "gender": "male"}
      ]}
    ]

Names are redacted before the LLM sees the text, so a non-zero delta points at
a prompt or model problem worth investigating.

    python scripts/bias_check.py --job-id <uuid> --dataset bias_pairs.json
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

    pairs = json.loads(args.dataset.read_text(encoding="utf-8"))
    with SessionLocal() as session:
        job = JobService(session).get(args.job_id)
    job_block = _job_block(job)
    prompt = get_prompt("screening")
    llm = get_llm()

    report = []
    deltas: list[float] = []
    for index, pair in enumerate(pairs):
        variant_scores = {}
        for variant in pair["variants"]:
            redacted = redact_pii(
                pair["resume_text"], known_name=variant.get("name")
            ).text
            scores = [_score(llm, prompt, job_block, redacted) for _ in range(args.runs)]
            variant_scores[variant.get("name") or f"variant-{index}"] = {
                "gender": variant.get("gender"),
                "scores": scores,
                "mean": round(statistics.fmean(scores), 2),
            }
        means = [item["mean"] for item in variant_scores.values()]
        delta = max(means) - min(means)
        deltas.append(delta)
        report.append(
            {"pair": index, "delta": round(delta, 2), "variants": variant_scores}
        )

    summary = {
        "job_id": job.id,
        "pairs": len(report),
        "mean_delta": round(statistics.fmean(deltas), 3) if deltas else 0.0,
        "max_delta": round(max(deltas), 3) if deltas else 0.0,
        "report": report,
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
