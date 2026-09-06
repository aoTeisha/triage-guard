# Adding an agent

Worked example: a throwaway **Greeter** agent. Same five steps for any real one.

## 1. Define the agent

New file `agents/greeter.jsonc`:

```jsonc
{
  "role": "Greeter",
  "goal": "Say hello to the person named in the input.",
  "backstory": "You are a friendly assistant. You keep it to one sentence.",
  "llm": "openrouter/anthropic/claude-sonnet-4",
  "tools": [],
  "allow_delegation": false,
  "verbose": true,
}
```

Keep `"allow_delegation": false` on every specialist — only the Orchestrator
delegates, and letting specialists re-delegate creates loops.

## 2. Register it in `crew.jsonc`

Add the **filename stem** to `"agents"`:

```jsonc
"agents": ["intake_parser", "acuity_classifier", "safety_validator", "greeter"],
```

The Orchestrator is deliberately absent from that list — it is named separately as
`"manager_agent"`, and crewAI rejects a manager that also appears in `"agents"`.

## 3. Add its task

Append to the `"tasks"` array. `"context"` names the tasks whose output this one
receives; omit it if the task needs nothing from earlier ones.

```jsonc
{
  "name": "say_hello",
  "description": "Greet {name} in one short sentence.",
  "expected_output": "A one-sentence greeting.",
  "agent": "greeter",
  "output_pydantic": { "python": "schemas.Greeting" },
  "output_file": "output/greeting.json",
}
```

Tasks must stay inline here — the loader requires `"tasks"` to be a list of objects
(`json_loader.py:441`), so there is no `tasks/` folder.

## 4. Add the output schema

New model in `[schemas.py](schemas.py)`:

```python
class Greeting(BaseModel):
    message: str = Field(description="The greeting, one sentence")
```

Reference it as `"schemas.Greeting"` — the loader resolves python refs relative to
`crew.jsonc`'s own directory, **not** the package root, so `"app.schemas.Greeting"`
fails.

## 5. Add the mock output

New entry in `[mock_data.py](mock_data.py)` under `MOCK_OUTPUTS`, keyed by the
**task name** (`say_hello`), not the agent name:

```python
"say_hello": {"message": "Hello, world!"},
```

`main.py` walks `crew.tasks` and looks up each one, so a missing key raises `KeyError`.

## 6. Verify

```bash
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from pathlib import Path
from crewai.project import load_crew
crew, _ = load_crew(Path('app/crew.jsonc'))
print(len(crew.agents), '|', crew.manager_agent.role, '|', [t.agent.role for t in crew.tasks])
"
uv run triage-guard
```

The first command validates every `.jsonc` and resolves the schema refs without
running anything. The second prints the mock outputs and sends one Langfuse span
per agent. Span names come from the loaded crew, so a correct trace is itself
proof the config parsed.

## Adding a tool

Create `tools/<name>.py` (the folder does not exist yet — make it with the first
tool) as a `BaseTool` subclass, then reference it from an agent as
`"tools": ["custom:<name>"]`.

```python
from crewai.tools import BaseTool

class ShoutTool(BaseTool):
    name: str = "shout"
    description: str = "Uppercase a string."

    def _run(self, text: str) -> str:
        return text.upper()
```
