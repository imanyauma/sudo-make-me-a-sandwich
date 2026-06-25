# Design — Alert-Checking AIOps Chatbot

## Overview

The alert-checking application is structured as two layers sharing the same agent core:

```
alert-checking/
├── chatbot.py   ← CLI entry-point + agent core (reusable)
├── app.py       ← Streamlit frontend (calls agent core)
├── requirements.txt
└── README.md
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Streamlit UI (app.py)          CLI (chatbot.py __main__)   │
│  st.chat_input / st.session_state   input() loop            │
└────────────────────┬────────────────────────────────────────┘
                     │ user message + conversation list
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  agent_turn()  [@workflow]                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Agentic loop                                        │   │
│  │  ┌────────────────────────────────────────────────┐  │   │
│  │  │  call_bedrock()  [@llm]                        │  │   │
│  │  │  amazon.nova-micro-v1:0 via AWS Bedrock        │  │   │
│  │  └─────────────┬──────────────────────────────────┘  │   │
│  │                │ stopReason == tool_use               │   │
│  │                ▼                                      │   │
│  │  ┌────────────────────────────────────────────────┐  │   │
│  │  │  execute_mcp_tool()  [@tool]                   │  │   │
│  │  │  DatadogMCPClient.call_tool()                  │  │   │
│  │  └─────────────┬──────────────────────────────────┘  │   │
│  │                │ tool result → messages               │   │
│  │                └──────── loop back ──────────────────┘   │
│  │                                                          │
│  │  stopReason == end_turn                                  │
│  │  ┌────────────────────────────────────────────────┐      │
│  │  │  format_reply()  [@task]                       │      │
│  │  └────────────────────────────────────────────────┘      │
│  └──────────────────────────────────────────────────────────┘
└─────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Datadog MCP Server  (JSON-RPC over HTTPS)                  │
│  search_datadog_monitors  │  get_apm_traces                 │
│  get_apm_trace_details    │  list_apm_services              │
└─────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Datadog LLM Observability                                  │
│  app.datadoghq.com/llm/traces                               │
│  Spans: workflow → llm → tool → task                        │
└─────────────────────────────────────────────────────────────┘
```

## Component Design

### DatadogMCPClient
A lightweight JSON-RPC 2.0 client that maintains a single MCP session.

- `initialize()` — sends `initialize` handshake, captures `Mcp-Session-Id` header.
- `list_tools()` — fetches all available tools from the server.
- `call_tool(name, arguments)` — executes a single tool and returns the text content.

Session ID is stored on the instance and replayed on every subsequent request,
ensuring the MCP server can maintain stateful context.

### Agent Functions (ddtrace decorated)

| Function | Decorator | Responsibility |
|---|---|---|
| `agent_turn` | `@workflow` | Root span per user turn. Owns the agentic loop. |
| `call_bedrock` | `@llm` | Single Bedrock invocation. Annotates input/output messages. |
| `execute_mcp_tool` | `@tool` | Executes one Datadog MCP tool call. Tags tool name + source. |
| `format_reply` | `@task` | Extracts final text from Nova content blocks. |

### Conversation Memory
Conversation history is a plain Python `list` of Bedrock message dicts:
```python
[
  {'role': 'user',      'content': [{'text': '...'}]},
  {'role': 'assistant', 'content': [{'text': '...'}]},
  ...
]
```
In the Streamlit app it is stored in `st.session_state.conversation` so it survives reruns
but is isolated per browser session.

### Tool Filtering
Only four tools are exposed to the model via `ALLOWED_TOOLS`:
```python
ALLOWED_TOOLS = {
    'search_datadog_monitors',
    'get_apm_traces',
    'get_apm_trace_details',
    'list_apm_services',
}
```
This keeps Nova's context window lean and prevents the model from calling unrelated
Datadog API surfaces.

### Datadog Unified Service Tagging

Tags are set at three levels to ensure full propagation:

1. **`os.environ`** — `DD_SERVICE`, `DD_ENV`, `DD_VERSION` set before ddtrace import.
2. **`tracer.set_tags()`** — global defaults inherited by all spans in the process.
3. **`LLMObs.annotate(..., tags={})`** — per-span tags on every decorated function.

```
env:dev  service:alert-checking  version:1.0.0  team:aiops
app.component:chatbot-ui  model.id:amazon.nova-micro-v1:0
interface:streamlit  tool.name:<name>  llm.stop_reason:<reason>
session.turn:<n>
```

## Key Design Decisions

**Why Nova Micro?**
Speed and token cost for a hackathon context. Nova Micro handles tool routing and
summarisation well without needing the full Nova Pro reasoning depth.

**Why `@st.cache_resource` for MCP?**
MCP session initialisation is an HTTP round-trip. Caching it means the session survives
Streamlit's frequent script reruns without reconnecting, keeping latency low.

**Why filter tools at load time vs per-query?**
Sending all available Datadog tools to Bedrock on every call wastes context tokens and
may confuse the model. Filtering once at startup keeps the tool schema small and focused.

**Why seed the conversation with a system context message?**
Amazon Nova models use `user`/`assistant` roles only — there is no `system` role in the
Bedrock Converse API for Nova. A seeded user→assistant exchange achieves the same effect
as a system prompt without breaking the role alternation requirement.
