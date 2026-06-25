"""
Sudo Make (Me A) Sandwich — Streamlit Frontend
Datadog AIOps Chatbot powered by Amazon Nova Micro + Datadog MCP

Datadog tagging strategy:
  env        → DD_ENV        (e.g. dev / staging / prod)
  service    → DD_SERVICE    (antek-asing)
  version    → DD_VERSION    (1.0.0)
  team       → custom tag    (aiops)

These propagate to every LLM span, tool span, workflow span, and
trace so all telemetry is filterable in the DD UI by environment.
"""

import os
import json
import time
import requests
import streamlit as st
from dotenv import load_dotenv

# ── Load env before any DD import ────────────────────────────
load_dotenv()

# ── Datadog unified service tagging ──────────────────────────
# Set before ddtrace initialises so every span inherits them.
os.environ.setdefault('DD_SERVICE',  'antek-asing')
os.environ.setdefault('DD_ENV',      os.environ.get('DD_ENV', 'dev'))
os.environ.setdefault('DD_VERSION',  '1.0.0')

import boto3
from ddtrace import tracer
from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.decorators import llm, tool, workflow, task

# ── Configure ddtrace tracer tags (service / env / version) ──
tracer.set_tags({
    'env':              os.environ['DD_ENV'],
    'service':          os.environ['DD_SERVICE'],
    'version':          os.environ['DD_VERSION'],
    'team':             'aiops',
    'app.name':         'antek-asing',
    'app.component':    'chatbot-ui',
    'runtime.platform': 'streamlit',
})

# ── Enable LLM Observability (idempotent — safe across reruns) ─
if not LLMObs._instance:
    LLMObs.enable(
        ml_app=os.environ['DD_LLMOBS_ML_APP'],
        agentless_enabled=True,
        api_key=os.environ['DD_API_KEY'],
        site=os.environ.get('DD_SITE', 'datadoghq.com'),
    )

# ── AWS Bedrock client ────────────────────────────────────────
bedrock = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

# ── Constants ─────────────────────────────────────────────────
MODEL_ID    = 'amazon.nova-micro-v1:0'
DD_MCP_URL  = 'https://mcp.datadoghq.com/api/unstable/mcp-server/mcp'
DD_HEADERS  = {
    'Content-Type':   'application/json',
    'DD-API-KEY':     os.environ['DD_API_KEY'],
    'DD-APPLICATION-KEY': os.environ['DD_APP_KEY'],
}
ALLOWED_TOOLS = {
    'search_datadog_monitors',
    'get_apm_traces',
    'get_apm_trace_details',
    'list_apm_services',
}

# ── Common span tags applied to every manual annotation ───────
SPAN_TAGS = {
    'env':           os.environ['DD_ENV'],
    'service':       os.environ['DD_SERVICE'],
    'version':       os.environ['DD_VERSION'],
    'team':          'aiops',
    'app.component': 'chatbot-ui',
    'model.id':      MODEL_ID,
    'interface':     'streamlit',
}


# ─────────────────────────────────────────────────────────────
# Datadog MCP Client
# ─────────────────────────────────────────────────────────────
class DatadogMCPClient:
    def __init__(self):
        self._id = 0
        self._session_id = None

    def _post(self, method: str, params: dict = None) -> dict:
        self._id += 1
        payload = {
            'jsonrpc': '2.0',
            'id': self._id,
            'method': method,
            'params': params or {},
        }
        headers = {**DD_HEADERS, 'Accept': 'application/json, text/event-stream'}
        if self._session_id:
            headers['Mcp-Session-Id'] = self._session_id
        response = requests.post(DD_MCP_URL, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        if 'Mcp-Session-Id' in response.headers:
            self._session_id = response.headers['Mcp-Session-Id']
        return response.json()

    def initialize(self):
        self._post('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'antek-asing-ui', 'version': '1.0'},
        })

    def list_tools(self) -> list:
        result = self._post('tools/list')
        return result.get('result', {}).get('tools', [])

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._post('tools/call', {'name': name, 'arguments': arguments})
        content = result.get('result', {}).get('content', [])
        return content[0].get('text', '') if content else ''


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def extract_text(content: list) -> str:
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


# ─────────────────────────────────────────────────────────────
# Agent core (decorated with ddtrace spans + env/service tags)
# ─────────────────────────────────────────────────────────────
@llm(model_name='nova-micro', model_provider='bedrock')
def call_bedrock(messages: list, tools: list) -> dict:
    LLMObs.annotate(
        input_data=[
            {
                'role': m['role'],
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

    body_payload: dict = {
        'messages': messages,
        'inferenceConfig': {'max_new_tokens': 2048},
    }
    if tools:
        body_payload['toolConfig'] = {
            'tools': [
                {
                    'toolSpec': {
                        'name': t['name'],
                        'description': t.get('description', ''),
                        'inputSchema': {'json': t['input_schema']},
                    }
                }
                for t in tools
            ]
        }

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body_payload),
        contentType='application/json',
    )
    body = json.loads(response['body'].read())
    text_output = extract_text(body['output']['message']['content'])
    LLMObs.annotate(
        output_data=[{'role': 'assistant', 'content': text_output or '[tool_use]'}],
        tags={**SPAN_TAGS, 'llm.stop_reason': body.get('stopReason', 'unknown')},
    )
    return body


@tool
def execute_mcp_tool(client: DatadogMCPClient, name: str, args: dict) -> str:
    LLMObs.annotate(
        input_data=json.dumps(args, indent=2),
        tags={
            **SPAN_TAGS,
            'tool.name':   name,
            'tool.source': 'datadog_mcp',
        },
    )
    output = client.call_tool(name, args)
    LLMObs.annotate(
        output_data=output,
        tags={**SPAN_TAGS, 'tool.name': name, 'tool.status': 'success'},
    )
    return output


@task
def format_reply(content: list) -> str:
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
    tool_log_callback=None,
) -> str:
    """
    One full agentic loop turn.
    tool_log_callback(tool_name, args, output) lets the UI show tool calls live.
    """
    LLMObs.annotate(
        input_data=user_message,
        tags={
            **SPAN_TAGS,
            'session.turn':   str(len([m for m in conversation if m['role'] == 'user'])),
            'session.tools':  str(len(dd_tools)),
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
            tool_results = []
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
            final_answer = format_reply(content)
            break

    LLMObs.annotate(
        output_data=final_answer,
        tags={**SPAN_TAGS, 'workflow.status': 'complete'},
    )
    return final_answer


# ─────────────────────────────────────────────────────────────
# Session-state initialisation (runs once per browser session)
# ─────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner='Connecting to Datadog MCP…')
def init_mcp() -> tuple[DatadogMCPClient, list]:
    """Cache the MCP client + tool list so we don't reconnect on every rerun."""
    client = DatadogMCPClient()
    client.initialize()
    raw_tools = client.list_tools()
    dd_tools = [
        {
            'name':         t['name'],
            'description':  t.get('description', ''),
            'input_schema': t['inputSchema'],
        }
        for t in raw_tools
        if t['name'] in ALLOWED_TOOLS
    ]
    return client, dd_tools


def init_session():
    if 'conversation' not in st.session_state:
        sys_ctx = build_system_prompt()
        st.session_state.conversation = [
            {
                'role':    'user',
                'content': [{'text': f'[System context]\n{sys_ctx}'}],
            },
            {
                'role':    'assistant',
                'content': [{'text': 'Understood. I am Sudo Make (Me A) Sandwich, ready to help with Datadog monitors, alerts, and traces.'}],
            },
        ]
    if 'chat_messages' not in st.session_state:
        st.session_state.chat_messages = []   # list of {'role', 'content', 'tools_used'}
    if 'tool_calls' not in st.session_state:
        st.session_state.tool_calls = []


# ─────────────────────────────────────────────────────────────
# Page layout & styling
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title='Sudo Make (Me A) Sandwich — AIOps Chatbot',
    page_icon='🐶',
    layout='wide',
    initial_sidebar_state='expanded',
)

st.markdown("""
<style>
/* ── global ── */
[data-testid="stAppViewContainer"] { background: #0f0f14; }
[data-testid="stSidebar"]          { background: #16161f; border-right: 1px solid #2a2a3a; }

/* ── header ── */
.aa-header {
    display: flex; align-items: center; gap: 12px;
    padding: 16px 0 8px;
}
.aa-header h1 { margin: 0; font-size: 1.5rem; color: #f0f0ff; }
.aa-header .badge {
    background: #6b21a8; color: #e9d5ff;
    padding: 2px 10px; border-radius: 9999px; font-size: 0.75rem;
}
.aa-header .badge-green {
    background: #14532d; color: #86efac;
    padding: 2px 10px; border-radius: 9999px; font-size: 0.75rem;
}

/* ── chat bubbles ── */
.bubble-user {
    background: #1e1b4b; border: 1px solid #3730a3;
    border-radius: 12px 12px 2px 12px;
    padding: 12px 16px; margin: 6px 0; color: #e0e0ff;
    max-width: 85%; margin-left: auto;
}
.bubble-bot {
    background: #1a1a2e; border: 1px solid #2a2a4a;
    border-radius: 12px 12px 12px 2px;
    padding: 12px 16px; margin: 6px 0; color: #e0e0e0;
    max-width: 85%;
}
.bubble-tool {
    background: #0c1a0c; border: 1px solid #166534;
    border-radius: 8px; padding: 8px 12px; margin: 4px 0;
    color: #86efac; font-size: 0.8rem; font-family: monospace;
    max-width: 85%;
}
.role-label {
    font-size: 0.7rem; font-weight: 600; letter-spacing: 0.05em;
    margin-bottom: 4px; opacity: 0.6; text-transform: uppercase;
}

/* ── sidebar tags ── */
.tag-pill {
    display: inline-block;
    background: #1e293b; color: #94a3b8;
    border: 1px solid #334155;
    border-radius: 9999px; padding: 2px 10px;
    font-size: 0.72rem; margin: 2px 3px;
}
.tag-pill-purple {
    background: #2d1b69; color: #c4b5fd; border-color: #5b21b6;
}
.tag-pill-green {
    background: #052e16; color: #86efac; border-color: #166534;
}
.tag-pill-orange {
    background: #431407; color: #fdba74; border-color: #9a3412;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('## 🐶 Sudo Make (Me A) Sandwich')
    st.caption('AIOps Chatbot · Nova Micro · Datadog MCP')

    st.divider()

    # ── Datadog environment tags (visible to operator) ────────
    st.markdown('**Datadog Tags**')
    env_val     = os.environ.get('DD_ENV',     'dev')
    svc_val     = os.environ.get('DD_SERVICE', 'antek-asing')
    ver_val     = os.environ.get('DD_VERSION', '1.0.0')
    ml_app_val  = os.environ.get('DD_LLMOBS_ML_APP', 'antek-asing')

    st.markdown(f"""
    <span class="tag-pill tag-pill-green">env:{env_val}</span>
    <span class="tag-pill tag-pill-purple">service:{svc_val}</span>
    <span class="tag-pill tag-pill-orange">version:{ver_val}</span>
    <span class="tag-pill">ml_app:{ml_app_val}</span>
    <span class="tag-pill">team:aiops</span>
    <span class="tag-pill">model:nova-micro</span>
    <span class="tag-pill">interface:streamlit</span>
    """, unsafe_allow_html=True)

    st.divider()

    # ── Active Datadog tools ───────────────────────────────────
    st.markdown('**Active Datadog MCP Tools**')
    try:
        _, sidebar_tools = init_mcp()
        if sidebar_tools:
            for t in sidebar_tools:
                st.markdown(f'`{t["name"]}`')
        else:
            st.warning('No tools loaded')
    except Exception as e:
        st.error(f'MCP init failed: {e}')

    st.divider()

    # ── Quick prompt shortcuts ─────────────────────────────────
    st.markdown('**Quick Prompts**')
    quick_prompts = {
        '🔴 Firing monitors':        'What monitors are currently alerting? Summarise and prioritise.',
        '🔍 Recent error traces':    'Show me recent APM traces with errors.',
        '🐌 Slowest services':       'Which services have the highest latency right now?',
        '📊 Full AIOps summary':     'Give me a full AIOps summary: firing monitors and top trace issues.',
    }
    for label, prompt in quick_prompts.items():
        if st.button(label, use_container_width=True):
            st.session_state['quick_prompt'] = prompt

    st.divider()

    # ── Session controls ──────────────────────────────────────
    if st.button('🗑️ Clear conversation', use_container_width=True):
        for key in ('conversation', 'chat_messages', 'tool_calls', 'quick_prompt'):
            st.session_state.pop(key, None)
        st.rerun()

    st.caption(f'DD Site: {os.environ.get("DD_SITE", "datadoghq.com")}')
    st.caption('Traces → app.datadoghq.com/llm/traces')


# ─────────────────────────────────────────────────────────────
# Main area
# ─────────────────────────────────────────────────────────────
init_session()

# Header
st.markdown("""
<div class="aa-header">
  <span style="font-size:2rem">🐶</span>
  <div>
    <h1>Sudo Make (Me A) Sandwich</h1>
    <span style="font-size:0.8rem;color:#888">Datadog AIOps · Amazon Nova Micro · MCP</span>
  </div>
</div>
""", unsafe_allow_html=True)

# Init MCP (cached)
try:
    mcp_client, dd_tools = init_mcp()
    tool_names = [t['name'] for t in dd_tools]
    if tool_names:
        st.success(f'Connected to Datadog MCP — {len(tool_names)} tools loaded', icon='✅')
    else:
        st.warning('Connected to Datadog MCP but no matching tools found. Check API keys.', icon='⚠️')
except Exception as e:
    st.error(f'Failed to connect to Datadog MCP: {e}', icon='❌')
    st.stop()

st.divider()

# ── Render chat history ────────────────────────────────────────
chat_container = st.container()
with chat_container:
    for msg in st.session_state.chat_messages:
        if msg['role'] == 'user':
            st.markdown(f"""
            <div class="bubble-user">
              <div class="role-label">You</div>
              {msg['content']}
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div class="bubble-bot">
              <div class="role-label">🐶 Sudo Make (Me A) Sandwich</div>
              {msg['content']}
            </div>
            """, unsafe_allow_html=True)

            # Show tool calls used for this response
            for tc in msg.get('tools_used', []):
                with st.expander(f'🔧 Tool called: `{tc["name"]}`', expanded=False):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.caption('Input args')
                        st.json(tc['args'])
                    with col2:
                        st.caption('Raw output (truncated)')
                        preview = tc['output'][:800] + '…' if len(tc['output']) > 800 else tc['output']
                        st.code(preview, language='json')

# ── Input area ────────────────────────────────────────────────
st.divider()

# Handle quick prompt injection from sidebar buttons
default_input = st.session_state.pop('quick_prompt', '')

user_input = st.chat_input(
    placeholder='Ask about monitors, alerts, or traces…',
)

# Also support the quick prompt if set
if default_input and not user_input:
    user_input = default_input

if user_input:
    # Add user bubble immediately
    st.session_state.chat_messages.append({
        'role':    'user',
        'content': user_input,
    })

    # Show thinking state
    with st.spinner('Sudo Make (Me A) Sandwich is thinking…'):
        tool_calls_this_turn: list[dict] = []

        def on_tool_call(name: str, args: dict, output: str):
            tool_calls_this_turn.append({'name': name, 'args': args, 'output': output})

        start_time = time.time()
        try:
            with tracer.trace(
                'chatbot.user_turn',
                service=os.environ['DD_SERVICE'],
                resource=user_input[:120],
            ) as span:
                span.set_tags({
                    **SPAN_TAGS,
                    'user.message_length': str(len(user_input)),
                    'session.history_len': str(len(st.session_state.chat_messages)),
                })
                reply = agent_turn(
                    user_input,
                    st.session_state.conversation,
                    mcp_client,
                    dd_tools,
                    tool_log_callback=on_tool_call,
                )
                span.set_tag('response.length',    str(len(reply)))
                span.set_tag('tools.calls_made',   str(len(tool_calls_this_turn)))
                span.set_tag('tools.names',        ','.join(tc['name'] for tc in tool_calls_this_turn))

        except requests.HTTPError as e:
            reply = f'⚠️ Datadog MCP error: {e}'
        except Exception as e:
            reply = f'⚠️ Unexpected error: {e}'

        elapsed = round(time.time() - start_time, 2)

    # Store assistant message with tool call metadata
    st.session_state.chat_messages.append({
        'role':       'assistant',
        'content':    reply,
        'tools_used': tool_calls_this_turn,
        'elapsed_s':  elapsed,
    })

    LLMObs.flush()
    st.rerun()
