from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

FLOWS = Path(__file__).resolve().parents[3] / "kestra" / "flows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((FLOWS / name).read_text(encoding="utf-8"))


def _walk(tasks: list[dict[str, Any]]):
    for task in tasks:
        yield task
        for key in ("tasks", "then", "else", "errors"):
            children = task.get(key)
            if isinstance(children, list):
                yield from _walk(children)


def _find(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    for task in _walk(tasks):
        if task.get("id") == task_id:
            return task
    raise AssertionError(f"task {task_id!r} not found")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_intake_flow_has_schedule_ai_tasks_and_error_handler() -> None:
    flow = _load("hr_intake.yml")

    assert flow["id"] == "hr_intake"
    assert flow["namespace"] == "hr.recruitment"
    assert flow["triggers"][0]["type"] == "io.kestra.plugin.core.trigger.Schedule"
    assert flow["triggers"][0]["cron"] == "*/15 * * * *"

    types = [task["type"] for task in _walk(flow["tasks"])]
    assert "io.kestra.plugin.core.flow.ForEach" in types
    assert "io.kestra.plugin.ai.completion.ChatCompletion" in types
    assert "io.kestra.plugin.core.http.Request" in types

    # Raw sender data must never be sent to Kestra: the flow only forwards the
    # opaque email_key returned by prepare.
    apply_body = _find(flow["tasks"], "apply_application")["body"]
    assert "email_key" in apply_body
    assert "from_address" not in apply_body
    assert "attachments" not in apply_body

    assert _find(flow.get("errors", []), "alert_failure")


def test_intake_flow_never_hardcodes_secrets() -> None:
    flow = _load("hr_intake.yml")
    text = "\n".join(_strings(flow))
    assert "secret('GEMINI_API_KEY')" in text
    assert "secret('INTERNAL_SERVICE_TOKEN')" in text
    assert "AIza" not in text


def test_screening_flow_pauses_without_auto_resume() -> None:
    flow = _load("hr_screening_and_decisions.yml")

    assert flow["id"] == "hr_screening_and_decisions"
    assert flow["namespace"] == "hr.recruitment"
    inputs = {item["id"]: item for item in flow["inputs"]}
    assert inputs["job_id"]["required"] is True
    assert inputs["run_id"]["required"] is True

    pause = _find(flow["tasks"], "wait_for_hr_approval")
    assert pause["type"] == "io.kestra.plugin.core.flow.Pause"
    assert pause["pauseDuration"] == "P7D"
    # The default Kestra behaviour is RESUME. CANCEL is the only safe choice.
    assert pause["behavior"] == "CANCEL"
    resume_ids = {item["id"] for item in pause["onResume"]}
    assert {"approved", "approved_by"} <= resume_ids


def test_screening_flow_sends_only_after_approval_and_reports_after_send() -> None:
    flow = _load("hr_screening_and_decisions.yml")
    gate = _find(flow["tasks"], "approved_gate")

    assert gate["type"] == "io.kestra.plugin.core.flow.If"
    assert "wait_for_hr_approval" in gate["condition"]
    then_ids = [task["id"] for task in gate["then"]]
    assert then_ids[0] == "send_approved"
    assert "report_prepare" in then_ids
    assert "report_finalize" in then_ids

    send = _find(flow["tasks"], "send_approved")
    assert send["uri"].endswith("/internal/hr-agent/send/approved")
    assert "run_id" in send["body"]
    assert _find(flow.get("errors", []), "alert_failure")


def test_screening_flow_never_hardcodes_secrets() -> None:
    flow = _load("hr_screening_and_decisions.yml")
    text = "\n".join(_strings(flow))
    assert "secret('GEMINI_API_KEY')" in text
    assert "secret('INTERNAL_SERVICE_TOKEN')" in text
    assert "AIza" not in text


@pytest.mark.parametrize(
    "name",
    [
        "hr_intake.yml",
        "hr_screening_and_decisions.yml",
        "fragments/hr_manager_screening.yml",
    ],
)
def test_flows_use_kestra_1x_syntax(name: str) -> None:
    flow = _load(name)
    text = (FLOWS / name).read_text(encoding="utf-8")
    assert "io.kestra.plugin.core.flow.ForEach" in text
    assert "pluginDefaults" in flow
    assert "io.kestra.plugin.core.flow.Loop" not in text
