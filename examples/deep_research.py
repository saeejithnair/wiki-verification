"""
Deep Research Tool with Reasoning Trace Capture

Since xAI doesn't expose raw reasoning traces, this implements a client-side tool
that prompts the model to output a summary of its reasoning after each search.
The model is instructed to call the `record_reasoning` tool to capture its
thought process, findings, and next steps.

Usage:
    python examples/deep_research.py "your research query here"
"""

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from xai_sdk import AsyncClient

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")
from xai_sdk.chat import system, tool, tool_result, user
from xai_sdk.tools import web_search, get_tool_call_type


@dataclass
class ReasoningTrace:
    """Stores the reasoning trace from the research session."""
    entries: list[dict] = field(default_factory=list)

    def add(self, step: int, reasoning: str, findings: str, next_steps: str) -> None:
        self.entries.append({
            "step": step,
            "reasoning": reasoning,
            "findings": findings,
            "next_steps": next_steps,
        })

    def display(self) -> None:
        print("\n" + "=" * 60)
        print("REASONING TRACE")
        print("=" * 60)
        for entry in self.entries:
            print(f"\n--- Step {entry['step']} ---")
            print(f"Reasoning: {entry['reasoning']}")
            print(f"Findings: {entry['findings']}")
            print(f"Next Steps: {entry['next_steps']}")
        print("\n" + "=" * 60)


def create_record_reasoning_tool():
    """Create the client-side tool for capturing reasoning traces."""
    return tool(
        name="record_reasoning",
        description=(
            "Record your current reasoning process. Call this after each search "
            "to document what you learned, how it connects to the research question, "
            "and what you plan to investigate next. This helps build a comprehensive "
            "research trace."
        ),
        parameters={
            "type": "object",
            "properties": {
                "step": {
                    "type": "integer",
                    "description": "The current step number in your research process",
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Your reasoning about the current state of research. "
                        "What are you trying to figure out? Why did you choose this search?"
                    ),
                },
                "findings": {
                    "type": "string",
                    "description": (
                        "Key findings from the most recent search. "
                        "What did you learn? What's relevant to the research question?"
                    ),
                },
                "next_steps": {
                    "type": "string",
                    "description": (
                        "What you plan to do next. "
                        "What gaps remain? What should be investigated further?"
                    ),
                },
            },
            "required": ["step", "reasoning", "findings", "next_steps"],
        },
    )


SYSTEM_PROMPT = """You are a deep research assistant. Your task is to thoroughly investigate the user's question using web search.

IMPORTANT: After EVERY search you perform, you MUST call the `record_reasoning` tool to document:
1. Your reasoning about why you performed that search
2. The key findings and how they relate to the research question
3. What you plan to investigate next

This creates a transparent trace of your research process.

Research methodology:
- Start with broad searches to understand the landscape
- Drill down into specific aspects as you learn more
- Cross-reference information from multiple sources
- Identify gaps in your understanding and address them
- Synthesize findings into a coherent answer

Always call `record_reasoning` after each search before proceeding to the next search or providing your final answer."""


async def deep_research(client: AsyncClient, query: str, model: str = "grok-4-fast") -> None:
    """
    Perform deep research on a query, capturing reasoning traces.

    Args:
        client: The xAI async client
        query: The research question to investigate
        model: The model to use (default: grok-4-fast)
    """
    trace = ReasoningTrace()
    reasoning_tool = create_record_reasoning_tool()

    chat = client.chat.create(
        model=model,
        tools=[web_search(), reasoning_tool],
        use_encrypted_content=True,
    )
    chat.append(system(SYSTEM_PROMPT))
    chat.append(user(query))

    print(f"\nResearching: {query}")
    print("-" * 60)

    is_thinking = True
    iteration = 0
    max_iterations = 20

    while iteration < max_iterations:
        iteration += 1
        client_side_tool_calls = []

        async for response, chunk in chat.stream():
            # Show tool calls as they happen
            for tool_call in chunk.tool_calls:
                tool_type = get_tool_call_type(tool_call)
                if tool_type == "client_side_tool":
                    client_side_tool_calls.append(tool_call)
                else:
                    print(f"\n[Search] {tool_call.function.name}: {tool_call.function.arguments}")

            # Show thinking progress (overwrite same line)
            if response.usage.reasoning_tokens and is_thinking:
                print(f"\rThinking... ({response.usage.reasoning_tokens} tokens)   ", end="", flush=True)

            # Transition to final response
            if chunk.content and is_thinking:
                print("\n\nFinal Response:")
                print("-" * 60)
                is_thinking = False

            # Stream final content
            if chunk.content and not is_thinking:
                print(chunk.content, end="", flush=True)

        chat.append(response)

        # No client-side tool calls means we're done
        if not client_side_tool_calls:
            break

        # Process reasoning tool calls
        for tool_call in client_side_tool_calls:
            if tool_call.function.name == "record_reasoning":
                args = json.loads(tool_call.function.arguments)
                trace.add(
                    step=args.get("step", iteration),
                    reasoning=args.get("reasoning", ""),
                    findings=args.get("findings", ""),
                    next_steps=args.get("next_steps", ""),
                )
                print(f"\n[Reasoning Step {args.get('step', iteration)}] {args.get('findings', '')[:100]}...")
                chat.append(tool_result("Reasoning recorded. Continue your research."))

        is_thinking = True

    # Display the complete reasoning trace
    trace.display()

    # Show citations and usage
    print("\nCitations:")
    if response.citations:
        for citation in response.citations:
            print(f"  - {citation}")
    else:
        print("  (none)")

    print("\nUsage:")
    print(f"  {response.usage}")


async def main(query: str) -> None:
    client = AsyncClient()
    await deep_research(client, query=query)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deep research with reasoning trace capture")
    parser.add_argument("query", help="The research question to investigate")
    args = parser.parse_args()

    asyncio.run(main(args.query))
