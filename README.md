# Antek Asing — Datadog AIOps Chatbot

Multi-turn chatbot that queries live Datadog data (monitors/alerts and traces) using the Datadog MCP server, powered by **Amazon Nova Micro** via AWS Bedrock.

## What it does

- **Monitors & Alerts** — fetches firing monitors, their severity, and suggests a priority investigation order.
- **Traces** — queries APM traces, surfaces slow services, error rates, and latency hotspots.
- **Multi-turn conversation** — retains context across messages in the same session.
- **Full LLM Observability** — every turn, tool call, and LLM span is traced to Datadog via `ddtrace`.

## Setup

1. Copy the root `.env.template` to `.env` and fill in your keys:
   ```
   DD_API_KEY=...
   DD_APP_KEY=...
   DD_SITE=datadoghq.com
   DD_LLMOBS_ENABLED=1
   DD_LLMOBS_AGENTLESS_ENABLED=1
   DD_LLMOBS_ML_APP=antek-asing
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   AWS_REGION=us-east-1
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the CLI chatbot:
   ```bash
   python chatbot.py
   ```

4. Or run the Streamlit UI:
   ```bash
   streamlit run app.py
   ```

## Example prompts

| What you want | What to ask |
|---|---|
| Firing monitors | `What monitors are currently alerting?` |
| Priority order | `Which alerts should I investigate first?` |
| Error traces | `Show me recent traces with errors` |
| Slow services | `Which services have the highest latency?` |
| Combined view | `Give me an AIOps summary of current issues` |

## Architecture

```
User prompt
    │
    ▼
agent_turn() [@workflow]
    │
    ├─► call_bedrock() [@llm]  ←── Amazon Nova Micro (nova-micro-v1)
    │       │
    │       └── tool_use? ──► execute_mcp_tool() [@tool]
    │                               │
    │                               └── Datadog MCP Server
    │                                    ├── search_datadog_monitors
    │                                    ├── get_apm_traces
    │                                    ├── get_apm_trace_details
    │                                    └── list_apm_services
    │
    └─► format_reply() [@task]
            │
            ▼
        Printed response + flushed to DD LLM Observability
```

## Observability

Traces are visible at: `app.datadoghq.com/llm/traces`

## Datadog Tagging Strategy

Every span (LLM, tool, task, workflow, and manual trace) is tagged with:

| Tag | Value | Purpose |
|---|---|---|
| `env` | `DD_ENV` env var (default: `dev`) | Filter by environment |
| `service` | `antek-asing` | Unified service tagging |
| `version` | `1.0.0` | Deployment version tracking |
| `team` | `aiops` | Team-level grouping |
| `app.component` | `chatbot-ui` | Component within the service |
| `model.id` | `amazon.nova-micro-v1:0` | Which model was used |
| `interface` | `streamlit` | Frontend type |
| `tool.name` | MCP tool name | Per tool-call context |
| `llm.stop_reason` | `end_turn` / `tool_use` | Why the model stopped |
| `session.turn` | turn number | Conversation depth |

Set `DD_ENV=prod` in your `.env` to switch all telemetry to the production environment filter.
