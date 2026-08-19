from langfuse import get_client
from crewai import Agent, Crew, Task
from dotenv import load_dotenv

load_dotenv()


langfuse = get_client()

greeter = Agent(
    role="Greeter",
    goal="Greet the world",
    backstory="A friendly agent that says hello.",
)

task = Task(
    description="Say hello world in one short sentence.",
    expected_output="A short hello world greeting.",
    agent=greeter,
)

crew = Crew(agents=[greeter], tasks=[task])

MOCK_OUTPUT = "Hello, Barak! This is your friendly agent reporting for duty."


def mock_kickoff() -> str:
    # ponytail: no real LLM call — canned output, real trace shape sent to Langfuse
    return MOCK_OUTPUT


if __name__ == "__main__":
    with langfuse.start_as_current_observation(name="hello-world-crew", as_type="span") as span:
        span.update(
            input={"task": task.description},
            metadata={"agent": greeter.role, "mock": True},
        )
        result = mock_kickoff()
        span.update(output=result)
        print(result)

    langfuse.flush()
