# dr-market

Deep research tools using xAI's SDK.

## Setup

### Create and activate virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### Install dependencies

```bash
pip install xai-sdk
```

### Set your API key

```bash
export XAI_API_KEY="your-api-key-here"
```

## Running Scripts

With the virtual environment activated:

```bash
# Run a script directly
python examples/deep_research.py

# Or run any example
python examples/server_side_tools.py
```

## Examples

Example scripts demonstrating SDK usage can be found in the `examples/` directory:

- `server_side_tools.py` - Demonstrates web search, X search, code execution, and client-side tools
- `telemetry.py` - Shows OpenTelemetry integration for tracing
- `deep_research.py` - Deep research tool with reasoning trace capture