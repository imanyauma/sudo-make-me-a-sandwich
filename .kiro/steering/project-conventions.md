# Project Conventions

This file is always included. Follow these rules for every file in this workspace.

## Stack

- **Language**: Python 3.11+
- **LLM runtime**: AWS Bedrock (`boto3`) — model `amazon.nova-micro-v1:0`
- **Observability**: Datadog `ddtrace` — LLM Observability + APM
- **Datadog data layer**: Datadog MCP server over JSON-RPC 2.0 (HTTPS)
- **Frontend**: Streamlit
- **Secrets**: `python-dotenv` loading from `.env` — never hardcode credentials

## Datadog Unified Service Tagging

Every application MUST set these three env vars before importing `ddtrace`:

```python
os.environ.setdefault('DD_SERVICE', '<app-name>')
os.environ.setdefault('DD_ENV',     os.environ.get('DD_ENV', 'dev'))
os.environ.setdefault('DD_VERSION', '1.0.0')
```

Every `LLMObs.annotate()` call MUST include a `tags` dict with at minimum:

```python
tags={
    'env':     os.environ['DD_ENV'],
    'service': os.environ['DD_SERVICE'],
    'version': os.environ['DD_VERSION'],
    'team':    'aiops',
}
```

`tracer.set_tags()` MUST be called after `ddtrace` is imported to set process-level defaults.

## ddtrace Decorator Rules

| Scenario | Decorator |
|---|---|
| Full agent turn (root span) | `@workflow` |
| Any call to an LLM (Bedrock, etc.) | `@llm(model_name=..., model_provider=...)` |
| Any external tool / API call | `@tool` |
| Non-LLM processing step | `@task` |

Always call `LLMObs.annotate(input_data=..., output_data=..., tags=...)` inside each
decorated function to populate span content in the Datadog UI.

## Amazon Nova Message Format

Nova uses `user` / `assistant` roles only — there is NO `system` role.
Seed system instructions as a user→assistant exchange at conversation start:

```python
conversation = [
    {'role': 'user',      'content': [{'text': '[System context]\n...instructions...'}]},
    {'role': 'assistant', 'content': [{'text': 'Understood. ...'}]},
]
```

Content is always a list of blocks: `[{'text': '...'}]`.

## Datadog MCP Client

Use the JSON-RPC 2.0 pattern from `alert-checking/chatbot.py`:
- Maintain a single `Mcp-Session-Id` across requests
- Filter exposed tools via `ALLOWED_TOOLS` set — only load what the model needs
- Call `client.initialize()` once at startup

MCP server URL: `https://mcp.datadoghq.com/api/unstable/mcp-server/mcp`

## Agentic Loop Pattern

```python
while True:
    body = call_bedrock(messages, tools)
    stop_reason = body['stopReason']
    content = body['output']['message']['content']
    messages.append({'role': 'assistant', 'content': content})

    if stop_reason == 'end_turn':
        break
    elif stop_reason == 'tool_use':
        # execute tools, append toolResult, continue loop
        ...
    else:
        break  # guard against unknown stop reasons
```

## Code Style

- Use `# ── section name ──` comment separators to organise module sections
- Keep functions small and single-responsibility
- Prefer `os.environ.get('KEY', 'default')` over raw `os.environ['KEY']` for optional vars
- Always add a `timeout=` to `requests.post()` calls (minimum 30s)
- Type-hint all function signatures
