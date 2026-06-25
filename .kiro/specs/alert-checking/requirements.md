# Requirements — Alert-Checking AIOps Chatbot

## Introduction

The alert-checking application is a multi-turn AIOps chatbot that gives on-call engineers
a conversational interface to query live Datadog monitor/alert state and APM traces.
It is powered by Amazon Nova Micro via AWS Bedrock and uses the Datadog MCP server as its
tool layer. A Streamlit frontend (`app.py`) wraps a reusable agent core (`chatbot.py`).

## Requirements

### R1 — Monitor & Alert Query
- **R1.1** The chatbot MUST call `search_datadog_monitors` via the Datadog MCP server to
  retrieve currently firing monitors before answering any alert-related question.
- **R1.2** The chatbot MUST summarise firing monitors with: monitor name, status, severity,
  and affected scope (host/service/tag).
- **R1.3** The chatbot MUST present monitors in priority order (ALERT > WARN > NO DATA).
- **R1.4** The chatbot SHOULD suggest a recommended investigation order based on severity
  and impacted service criticality.

### R2 — APM Trace Query
- **R2.1** The chatbot MUST call `get_apm_traces` to list recent traces when asked about
  errors, latency, or slow services.
- **R2.2** The chatbot MUST call `get_apm_trace_details` for deeper context when a specific
  trace or service is asked about.
- **R2.3** The chatbot MUST call `list_apm_services` when asked which services are currently
  instrumented or to provide a service map context.
- **R2.4** Trace responses MUST include: service name, operation, duration (p99), error rate,
  and HTTP status if applicable.

### R3 — Multi-turn Conversation
- **R3.1** The agent MUST retain the full conversation history within a session so follow-up
  questions have context from previous answers.
- **R3.2** Conversation history MUST be seeded with a system context message on session start.
- **R3.3** The agent MUST NOT hallucinate data — it MUST always call a Datadog MCP tool to
  fetch live data before answering factual questions about monitors or traces.

### R4 — Agentic Loop
- **R4.1** The agent MUST implement a Bedrock agentic loop: call model → handle `tool_use`
  stop reason → execute MCP tool → feed result back → repeat until `end_turn`.
- **R4.2** The loop MUST guard against infinite cycles by breaking on any unrecognised
  `stopReason`.
- **R4.3** Tool calls MUST be logged (name + args) for debugging visibility.

### R5 — LLM Observability (Datadog)
- **R5.1** Every LLM call MUST be wrapped with the `@llm` decorator from `ddtrace.llmobs`.
- **R5.2** Every MCP tool call MUST be wrapped with the `@tool` decorator.
- **R5.3** Each agent turn MUST be wrapped with the `@workflow` decorator as the root span.
- **R5.4** Post-processing steps (format_reply) MUST be wrapped with the `@task` decorator.
- **R5.5** All spans MUST carry unified service tags: `env`, `service`, `version`, `team`.
- **R5.6** `LLMObs.flush()` MUST be called at session end (CLI) or after each turn (UI) to
  ensure traces are delivered.

### R6 — Streamlit Frontend
- **R6.1** The UI MUST display a chat history with distinct user and assistant bubbles.
- **R6.2** Each assistant response MUST show collapsible panels for any MCP tool calls made
  during that turn (tool name, input args, raw output).
- **R6.3** The sidebar MUST display active Datadog span tags (env, service, version, team).
- **R6.4** The sidebar MUST provide quick-prompt buttons for the most common AIOps queries.
- **R6.5** The MCP client MUST be initialised once per app session using `@st.cache_resource`
  to avoid reconnecting on every Streamlit rerun.
- **R6.6** The UI MUST show a connection status banner (success/warning/error) at the top.

### R7 — Configuration & Secrets
- **R7.1** All secrets (`DD_API_KEY`, `DD_APP_KEY`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`) MUST be loaded from environment variables only — never hardcoded.
- **R7.2** The application MUST provide a `.env.template` listing all required variables.
- **R7.3** `DD_ENV`, `DD_SERVICE`, and `DD_VERSION` MUST be set via `os.environ.setdefault`
  before any `ddtrace` import to ensure correct span tagging.
