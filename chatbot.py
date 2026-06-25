"""
alert-checking — Agent core module
Imported by app.py (Streamlit UI). No CLI entry-point.

Responsibilities:
  - Datadog MCP client (JSON-RPC 2.0)
  - Bedrock invocation with Bedrock Guardrails applied
  - ddtrace LLM Observability spans (@workflow / @llm / @tool / @task)
  - Exported constants used by app.py
"""

import boto3
import json
import os
import requests
from dotenv import load_dotenv
from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.decorators import llm, tool, workflow, task

load_dotenv()

# ── Datadog unified service tagging (must be set before ddtrace uses them) ──
os.environ.setdefault('DD_SERVICE', 'incident-aiops')
os.environ.setdefault('DD_ENV',     os.environ.get('DD_ENV', 'dev'))
os.environ.setdefault('DD_VERSION', '1.0.0')

# ── Enable LLM Observability (idempotent — safe when imported by app.py) ────
if not LLMObs._instance:
    LLMObs.enable(
        ml_app=os.environ['DD_LLMOBS_ML_APP'],
        agentless_enabled=True,
        api_key=os.environ['DD_API_KEY'],
        site=os.environ.get('DD_SITE', 'datadoghq.com'),
    )

# ── AWS Bedrock client ───────────────────────────────────────────────────────
bedrock = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

# ── Constants (exported for app.py) ─────────────────────────────────────────
MODEL_ID = 'amazon.nova-micro-v1:0'

DD_MCP_URL = 'https://mcp.datadoghq.com/api/unstable/mcp-server/mcp'
DD_HEADERS = {
    'Content-Type':       'application/json',
    'DD-API-KEY':         os.environ['DD_API_KEY'],
    'DD-APPLICATION-KEY': os.environ['DD_APP_KEY'],
}

ALLOWED_TOOLS = {
    'search_datadog_monitors',  # monitor & alert data
    'get_apm_traces',           # trace listing
    'get_apm_trace_details',    # individual trace detail
    'list_apm_services',        # supporting APM context
}

# ── Bedrock Guardrail config (optional — skipped if not set in .env) ────────
# Set BEDROCK_GUARDRAIL_ID and BEDROCK_GUARDRAIL_VERSION in your .env to enable.
# The guardrail is applied on every invoke_model call via the guardrailConfig field.
GUARDRAIL_ID      = os.environ.get('BEDROCK_GUARDRAIL_ID')
GUARDRAIL_VERSION = os.environ.get('BEDROCK_GUARDRAIL_VERSION', 'DRAFT')

# ── Span tags applied to every LLMObs.annotate() call ───────────────────────
SPAN_TAGS: dict = {
    'env':      os.environ['DD_ENV'],
    'service':  os.environ['DD_SERVICE'],
    'version':  os.environ['DD_VERSION'],
    'team':     'aiops',
    'model.id': MODEL_ID,
    'guardrail.enabled': str(bool(GUARDRAIL_ID)),
}


# ─────────────────────────────────────────────────────────────────────────────
# Datadog MCP Client
# ─────────────────────────────────────────────────────────────────────────────
class DatadogMCPClient:
    """Minimal JSON-RPC 2.0 client for the Datadog MCP server."""

    def __init__(self) -> None:
        self._id: int = 0
        self._session_id: str | None = None

    def _post(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {
            'jsonrpc': '2.0',
            'id':      self._id,
            'method':  method,
            'params':  params or {},
        }
        headers = {**DD_HEADERS, 'Accept': 'application/json, text/event-stream'}
        if self._session_id:
            headers['Mcp-Session-Id'] = self._session_id
        response = requests.post(DD_MCP_URL, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        if 'Mcp-Session-Id' in response.headers:
            self._session_id = response.headers['Mcp-Session-Id']
        return response.json()

    def initialize(self) -> None:
        self._post('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities':    {},
            'clientInfo':      {'name': 'incident-aiops', 'version': '1.0'},
        })

    def list_tools(self) -> list:
        result = self._post('tools/list')
        return result.get('result', {}).get('tools', [])

    def call_tool(self, name: str, arguments: dict) -> str:
        result  = self._post('tools/call', {'name': name, 'arguments': arguments})
        content = result.get('result', {}).get('content', [])
        return content[0].get('text', '') if content else ''


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def extract_text(content: list) -> str:
    """Flatten Nova content blocks to a single string."""
    return '\n'.join(b['text'] for b in content if b.get('text')).strip()


def build_system_prompt() -> str:
    return (
        'You are Sudo Make (Me A) Sandwich, an AIOps assistant with direct access to Datadog. '
        'You specialise in two areas:\n'
        '1. Monitors & Alerts — summarising firing monitors, their severity, '
        'and recommended investigation order.\n'
        '2. Traces — analysing APM traces, identifying slow services, errors, '
        'and latency hotspots.\n\n'
        'Always use the available Datadog tools to fetch live data before answering. '
        'Be concise, use bullet points, and highlight the most critical issues first. '
        'When asked to prioritise, consider alert severity, impacted services, and error rates.'
    )


def _build_bedrock_payload(messages: list, tools: list) -> dict:
    """
    Build the invoke_model request body.
    Attaches guardrailConfig when BEDROCK_GUARDRAIL_ID is configured.
    """
    payload: dict = {
        'messages':        messages,
        'inferenceConfig': {'max_new_tokens': 2048},
    }

    # ── Guardrail (optional) ─────────────────────────────────
    # Uses the guardrailConfig field supported by Amazon Nova models.
    # streamProcessingMode SYNC means the guardrail runs inline on every call;
    # the response will include a 'amazon-bedrock-guardrailAction' field
    # set to 'INTERVENED' if content was blocked or altered.
    if GUARDRAIL_ID:
        payload['guardrailConfig'] = {
            'guardrailIdentifier':  GUARDRAIL_ID,
            'guardrailVersion':     GUARDRAIL_VERSION,
            'streamProcessingMode': 'SYNC',
        }

    # ── Tool config ──────────────────────────────────────────
    if tools:
        payload['toolConfig'] = {
            'tools': [
                {
                    'toolSpec': {
                        'name':        t['name'],
                        'description': t.get('description', ''),
                        'inputSchema': {'json': t['input_schema']},
                    }
                }
                for t in tools
            ]
        }

    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Agent functions — decorated for Datadog LLM Observability
# All decorated functions live here (single module) to avoid span context errors.
# ─────────────────────────────────────────────────────────────────────────────

@llm(model_name='nova-micro', model_provider='bedrock')
def call_bedrock(messages: list, tools: list) -> dict:
    """Single Bedrock invocation with optional guardrail applied."""
    LLMObs.annotate(
        input_data=[
            {
                'role':    m['role'],
                'content': (
                    extract_text(m['content'])
                    if isinstance(m['content'], list)
                    else json.dumps(m['content'])
                ),
            }
            for m in messages
        ],
        tags={
            **SPAN_TAGS,
            'llm.model':    MODEL_ID,
            'llm.provider': 'bedrock',
        },
    )

    body_payload = _build_bedrock_payload(messages, tools)

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body_payload),
        contentType='application/json',
    )
    body = json.loads(response['body'].read())

    # ── Guardrail intervention check ─────────────────────────
    # If the guardrail blocked or altered the response, tag the span so it
    # is visible in Datadog and surface a safe fallback message.
    guardrail_action = body.get('amazon-bedrock-guardrailAction', 'NONE')
    if guardrail_action == 'INTERVENED':
        body.setdefault('output', {}).setdefault('message', {})['content'] = [
            {'text': '⚠️ My response was blocked by a content guardrail. Please rephrase your question.'}
        ]
        body['stopReason'] = 'end_turn'

    text_output = extract_text(body['output']['message']['content'])
    LLMObs.annotate(
        output_data=[{'role': 'assistant', 'content': text_output or '[tool_use]'}],
        tags={
            **SPAN_TAGS,
            'llm.stop_reason':       body.get('stopReason', 'unknown'),
            'guardrail.action':      guardrail_action,
        },
    )
    return body


@tool
def execute_mcp_tool(client: DatadogMCPClient, name: str, args: dict) -> str:
    """Execute one Datadog MCP tool and return its text output."""
    LLMObs.annotate(
        input_data=json.dumps(args, indent=2),
        tags={**SPAN_TAGS, 'tool.name': name, 'tool.source': 'datadog_mcp'},
    )
    output = client.call_tool(name, args)
    LLMObs.annotate(
        output_data=output,
        tags={**SPAN_TAGS, 'tool.name': name, 'tool.status': 'success'},
    )
    return output


@task
def format_reply(content: list) -> str:
    """Extract and return the final assistant text from Nova content blocks."""
    text = extract_text(content)
    LLMObs.annotate(
        input_data=json.dumps(content, indent=2),
        output_data=text,
        tags={**SPAN_TAGS, 'task.name': 'format_reply'},
    )
    return text


@workflow
def agent_turn(
    user_message: str,
    conversation: list,
    mcp_client: DatadogMCPClient,
    dd_tools: list,
    tool_log_callback: callable | None = None,
) -> str:
    """
    Execute one full agentic turn (root span).

    Runs the Bedrock agentic loop until stopReason == 'end_turn':
      call_bedrock → tool_use? → execute_mcp_tool → loop back → format_reply

    Args:
        user_message:      The user's input text.
        conversation:      Mutable list of Bedrock message dicts (shared state).
        mcp_client:        Initialised DatadogMCPClient instance.
        dd_tools:          Filtered list of tool dicts to expose to the model.
        tool_log_callback: Optional callable(name, args, output) for UI display.

    Returns:
        The assistant's final text response.
    """
    LLMObs.annotate(
        input_data=user_message,
        tags={
            **SPAN_TAGS,
            'session.turn':  str(len([m for m in conversation if m['role'] == 'user'])),
            'session.tools': str(len(dd_tools)),
        },
    )

    conversation.append({'role': 'user', 'content': [{'text': user_message}]})
    final_answer = ''

    while True:
        body        = call_bedrock(conversation, dd_tools)
        stop_reason = body['stopReason']
        content     = body['output']['message']['content']

        conversation.append({'role': 'assistant', 'content': content})

        if stop_reason == 'end_turn':
            final_answer = format_reply(content)
            break

        elif stop_reason == 'tool_use':
            tool_results: list = []
            for block in content:
                if 'toolUse' in block:
                    tu        = block['toolUse']
                    tool_name = tu['name']
                    tool_args = tu['input']
                    output    = execute_mcp_tool(mcp_client, tool_name, tool_args)
                    if tool_log_callback:
                        tool_log_callback(tool_name, tool_args, output)
                    tool_results.append({
                        'toolResult': {
                            'toolUseId': tu['toolUseId'],
                            'content':   [{'text': output}],
                        }
                    })
            conversation.append({'role': 'user', 'content': tool_results})

        else:
            # Unknown stopReason — surface whatever text is available and exit
            final_answer = format_reply(content)
            break

    LLMObs.annotate(
        output_data=final_answer,
        tags={**SPAN_TAGS, 'workflow.status': 'complete'},
    )
    return final_answer
