import boto3, json, os, requests
from dotenv import load_dotenv
from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.decorators import llm, tool, workflow, task

load_dotenv()

# ── Enable LLM Observability ─────────────────────────────────
LLMObs.enable(
    ml_app=os.environ['DD_LLMOBS_ML_APP'],
    agentless_enabled=True,
    api_key=os.environ['DD_API_KEY'],
    site=os.environ.get('DD_SITE', 'datadoghq.com')
)

bedrock = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION', 'us-east-1'))

DD_MCP_URL = 'https://mcp.datadoghq.com/api/unstable/mcp-server/mcp'
DD_HEADERS = {
    'Content-Type': 'application/json',
    'DD-API-KEY': os.environ['DD_API_KEY'],
    'DD-APPLICATION-KEY': os.environ['DD_APP_KEY'],
}

# Model to use — Nova Micro for speed and cost efficiency
MODEL_ID = 'amazon.nova-micro-v1:0'

# Tools we want to expose from the Datadog MCP server
# monitors/alerts  → search_datadog_monitors
# traces           → get_apm_trace, get_apm_trace_details (or similar APM tools)
ALLOWED_TOOLS = {
    'search_datadog_monitors',   # monitor & alert data
    'get_apm_traces',            # trace listing
    'get_apm_trace_details',     # individual trace detail
    'list_apm_services',         # supporting context for traces
}


# ── Minimal JSON-RPC client for the Datadog MCP server ────────
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
            'params': params or {}
        }
        headers = {
            **DD_HEADERS,
            'Accept': 'application/json, text/event-stream'
        }
        if self._session_id:
            headers['Mcp-Session-Id'] = self._session_id
        response = requests.post(DD_MCP_URL, json=payload, headers=headers)
        response.raise_for_status()
        if 'Mcp-Session-Id' in response.headers:
            self._session_id = response.headers['Mcp-Session-Id']
        return response.json()

    def initialize(self):
        self._post('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'antek-asing-chatbot', 'version': '1.0'}
        })

    def list_tools(self) -> list:
        result = self._post('tools/list')
        return result.get('result', {}).get('tools', [])

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._post('tools/call', {'name': name, 'arguments': arguments})
        content = result.get('result', {}).get('content', [])
        return content[0].get('text', '') if content else ''


# ── Helper: extract clean text from Nova content blocks ───────
def extract_text(content: list) -> str:
    return '\n'.join(b['text'] for b in content if b.get('text')).strip()


# ── @llm — single Bedrock call with tool support ─────────────
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
                )
            }
            for m in messages
        ]
    )

    body_payload = {
        'messages': messages,
        'inferenceConfig': {'max_new_tokens': 2048}
    }

    # Only include toolConfig when tools are available
    if tools:
        body_payload['toolConfig'] = {
            'tools': [
                {
                    'toolSpec': {
                        'name': t['name'],
                        'description': t.get('description', ''),
                        'inputSchema': {'json': t['input_schema']}
                    }
                }
                for t in tools
            ]
        }

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body_payload),
        contentType='application/json'
    )
    body = json.loads(response['body'].read())
    text_output = extract_text(body['output']['message']['content'])
    LLMObs.annotate(
        output_data=[{'role': 'assistant', 'content': text_output or '[tool_use]'}]
    )
    return body


# ── @tool — execute a Datadog MCP tool ───────────────────────
@tool
def execute_mcp_tool(client: DatadogMCPClient, name: str, args: dict) -> str:
    LLMObs.annotate(
        input_data=json.dumps(args, indent=2),
        tags={'tool.name': name, 'tool.source': 'datadog_mcp'}
    )
    output = client.call_tool(name, args)
    LLMObs.annotate(output_data=output)
    return output


# ── @task — format and print the assistant reply ─────────────
@task
def format_reply(content: list) -> str:
    text = extract_text(content)
    LLMObs.annotate(input_data=json.dumps(content, indent=2), output_data=text)
    return text


# ── @workflow — one full agentic turn per user message ────────
@workflow
def agent_turn(user_message: str, conversation: list, mcp_client: DatadogMCPClient, dd_tools: list) -> str:
    LLMObs.annotate(input_data=user_message)

    # Append the new user message to the shared conversation history
    conversation.append({'role': 'user', 'content': [{'text': user_message}]})

    final_answer = ''

    # Agentic loop: keep calling Bedrock until it finishes or calls tools
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
                    tool_use = block['toolUse']
                    tool_name = tool_use['name']
                    tool_args = tool_use['input']
                    print(f'\n  [Calling Datadog MCP tool: {tool_name}]')
                    print(f'  args: {json.dumps(tool_args, indent=4)}')
                    output = execute_mcp_tool(mcp_client, tool_name, tool_args)
                    tool_results.append({
                        'toolResult': {
                            'toolUseId': tool_use['toolUseId'],
                            'content': [{'text': output}]
                        }
                    })
            conversation.append({'role': 'user', 'content': tool_results})

        else:
            # Unexpected stop reason — break to avoid infinite loop
            final_answer = format_reply(content)
            break

    LLMObs.annotate(output_data=final_answer)
    return final_answer


def build_system_prompt() -> str:
    return (
        'You are Sudo Make (Me A) Sandwich, an AIOps assistant with direct access to Datadog. '
        'You specialise in two areas:\n'
        '1. Monitors & Alerts — summarising firing monitors, their severity, and recommended investigation order.\n'
        '2. Traces — analysing APM traces, identifying slow services, errors, and latency hotspots.\n\n'
        'Always use the available Datadog tools to fetch live data before answering. '
        'Be concise, use bullet points, and highlight the most critical issues first. '
        'When asked to prioritise, consider alert severity, impacted services, and error rates.'
    )


def main():
    print('=' * 60)
    print('  Sudo Make (Me A) Sandwich — Datadog AIOps Chatbot')
    print('  Powered by Amazon Nova Micro + Datadog MCP')
    print('  Type "exit" or "quit" to leave.')
    print('=' * 60)

    # Initialise MCP client once and reuse across the session
    print('\n[Connecting to Datadog MCP...]')
    mcp_client = DatadogMCPClient()
    mcp_client.initialize()

    # Fetch and filter tools to monitors + traces only
    raw_tools = mcp_client.list_tools()
    dd_tools = [
        {
            'name': t['name'],
            'description': t.get('description', ''),
            'input_schema': t['inputSchema']
        }
        for t in raw_tools
        if t['name'] in ALLOWED_TOOLS
    ]

    available_names = [t['name'] for t in dd_tools]
    print(f'[Available Datadog tools: {available_names}]\n')

    if not dd_tools:
        print('[Warning] No matching Datadog tools found. Check your API keys and tool names.')

    # Conversation history shared across all turns (multi-turn memory)
    conversation: list = []

    # Optional: seed with a system-style user message since Nova uses user/assistant roles
    system_context = build_system_prompt()
    conversation.append({
        'role': 'user',
        'content': [{'text': f'[System context — follow these instructions throughout the conversation]\n{system_context}'}]
    })
    conversation.append({
        'role': 'assistant',
        'content': [{'text': 'Understood. I am Sudo Make (Me A) Sandwich, ready to help you with Datadog monitors, alerts, and traces.'}]
    })

    print('Sudo Make (Me A) Sandwich: Hello! I can help you with Datadog monitors, alerts, and traces.')
    print('            Try asking things like:')
    print('              - "What monitors are currently alerting?"')
    print('              - "Show me recent traces with errors."')
    print('              - "Which services have the highest latency?\n"')

    while True:
        try:
            user_input = input('You: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\n[Session ended]')
            break

        if not user_input:
            continue

        if user_input.lower() in ('exit', 'quit', 'bye'):
            print('Sudo Make (Me A) Sandwich: Goodbye! Stay on top of those alerts.')
            break

        print('\nSudo Make (Me A) Sandwich: ', end='', flush=True)
        try:
            reply = agent_turn(user_input, conversation, mcp_client, dd_tools)
            print(reply)
        except requests.HTTPError as e:
            print(f'[Datadog MCP error: {e}]')
        except Exception as e:
            print(f'[Error: {e}]')

        print()

    LLMObs.flush()
    print('[Traces flushed to Datadog LLM Observability]')


if __name__ == '__main__':
    main()
