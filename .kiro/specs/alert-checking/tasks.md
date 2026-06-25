# Tasks — Alert-Checking AIOps Chatbot

## Task List

- [x] 1. Bootstrap project structure
  - Create `alert-checking/` directory with `chatbot.py`, `app.py`, `requirements.txt`, `README.md`
  - Verify `.env.template` at project root covers all required variables
  - _Requirements: R7.1, R7.2_

- [x] 2. Implement DatadogMCPClient
  - JSON-RPC 2.0 `_post()` method with session ID tracking
  - `initialize()`, `list_tools()`, `call_tool()` methods
  - HTTP timeout and `raise_for_status()` error handling
  - _Requirements: R1.1, R2.1, R4.1_

- [x] 3. Implement agent core functions
  - `extract_text()` helper for Nova content block parsing
  - `call_bedrock()` with `@llm` decorator and LLMObs annotation
  - `execute_mcp_tool()` with `@tool` decorator and tool name tags
  - `format_reply()` with `@task` decorator
  - `agent_turn()` with `@workflow` decorator and full agentic loop
  - _Requirements: R4.1, R4.2, R5.1–R5.4_

- [x] 4. Add Datadog unified service tagging
  - Set `DD_SERVICE`, `DD_ENV`, `DD_VERSION` via `os.environ.setdefault` before imports
  - Call `tracer.set_tags()` with all standard tags
  - Add `SPAN_TAGS` dict to every `LLMObs.annotate()` call
  - Add `session.turn` and `llm.stop_reason` tags
  - _Requirements: R5.5_

- [x] 5. Implement CLI entry-point (`chatbot.py __main__`)
  - MCP client init + tool filtering by `ALLOWED_TOOLS`
  - System context seeding via user→assistant exchange
  - `input()` loop with exit commands
  - `LLMObs.flush()` on session end
  - _Requirements: R3.1, R3.2, R5.6_

- [x] 6. Implement Streamlit frontend (`app.py`)
  - `@st.cache_resource` MCP initialisation
  - `st.session_state` conversation + chat_messages management
  - Chat bubble rendering (user / assistant)
  - Collapsible tool call expanders per assistant message
  - Sidebar: Datadog tag pills, active tools list, quick-prompt buttons
  - Connection status banner
  - Clear conversation button
  - `LLMObs.flush()` after each turn
  - _Requirements: R6.1–R6.6, R5.6_

- [ ] 7. Add error handling and resilience
  - Catch `requests.HTTPError` from MCP calls and surface in UI
  - Add retry logic (max 2 retries with backoff) for transient MCP failures
  - Validate `DD_API_KEY` and `DD_APP_KEY` are present at startup; show a clear
    error message if missing rather than a raw `KeyError`
  - _Requirements: R4.2_

- [ ] 8. Improve monitor prioritisation output
  - Post-process `search_datadog_monitors` results to group by: ALERT, WARN, NO DATA, OK
  - Add a structured summary table to the assistant response when >3 monitors are firing
  - Include recommended investigation order as a numbered list
  - _Requirements: R1.3, R1.4_

- [ ] 9. Improve trace analysis output
  - Parse `get_apm_traces` response to extract: service, resource, duration_ms, error bool
  - Sort by duration descending and error count descending
  - Format as a markdown table in the assistant response
  - _Requirements: R2.4_

- [ ] 10. Add session export
  - Add a "Download transcript" button in the Streamlit sidebar
  - Export chat messages (role + content + tools_used + elapsed_s) as JSON
  - _Requirements: R6.4 (extended)_

- [ ] 11. Write integration smoke test
  - Mock `DatadogMCPClient.call_tool()` to return fixture JSON
  - Assert `agent_turn()` returns a non-empty string
  - Assert `LLMObs.annotate` is called with expected tags
  - Run with: `pytest alert-checking/tests/`
  - _Requirements: R1.1, R5.5_
