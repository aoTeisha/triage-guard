#!/usr/bin/env python
"""Triage Guard entrypoint.

Loads the crew from crew.jsonc (which validates every agents/*.jsonc) and walks
its tasks, emitting one Langfuse span per agent with canned output. No LLM is
called yet — this exists to prove the wiring and the trace shape.
"""

import json
from pathlib import Path

from crewai.project import load_crew
from dotenv import load_dotenv

from app.mock_data import DEMO_CASE_1, MOCK_OUTPUTS
from app.observability import agent_span, langfuse


def run():
    load_dotenv()

    crew, default_inputs = load_crew(Path(__file__).with_name("crew.jsonc"))
    inputs = {**default_inputs, "case": json.dumps(DEMO_CASE_1, indent=2)}

    with agent_span("triage-case", case_id=DEMO_CASE_1["case_id"]) as root:
        root.update(input=DEMO_CASE_1)

        results = {}
        for task in crew.tasks:
            output = MOCK_OUTPUTS[task.name]
            with agent_span(task.agent.role, task=task.name) as span:
                span.update(input=task.description.format(**inputs), output=output)
            results[task.name] = output
            print(f"\n=== {task.agent.role} / {task.name} ===")
            print(json.dumps(output, indent=2))

        root.update(output=results)

    # ponytail: replace the loop above with crew.kickoff(inputs=inputs) once the
    # agents do real work, and swap manual spans for CrewAIInstrumentor().
    langfuse.flush()


if __name__ == "__main__":
    run()
