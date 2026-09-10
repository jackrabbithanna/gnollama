"""Tool definitions and manual-result protocol handling; never executes tools."""
import copy
import json

from .structured import parse_json, schema_validator


EXAMPLE_TOOLS = json.dumps([
    {
        'type': 'function',
        'function': {
            'name': 'list_files',
            'description': 'List files and directories under a workspace-relative path.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Directory path, such as . or src.'},
                    'recursive': {'type': 'boolean', 'description': 'Whether to include files in subdirectories.'},
                },
                'required': ['path', 'recursive'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': 'Read the UTF-8 contents of a file in the workspace.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Workspace-relative file path.'},
                },
                'required': ['path'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_code',
            'description': 'Search for literal text in workspace files. Return matching file paths, line numbers, and lines.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Literal text to find.'},
                    'path': {'type': 'string', 'description': 'File or directory to search, relative to the workspace.'},
                },
                'required': ['query', 'path'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'replace_in_file',
            'description': 'Replace one exact occurrence of old_text with new_text. Return an error without changing the file if old_text is absent or occurs more than once.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Workspace-relative file path.'},
                    'old_text': {'type': 'string', 'minLength': 1},
                    'new_text': {'type': 'string'},
                },
                'required': ['path', 'old_text', 'new_text'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'run_command',
            'description': 'Run a program with an argument list in a workspace directory. Return exit_code, stdout, and stderr. Arguments are passed directly without shell expansion.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'argv': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'minItems': 1,
                        'description': 'Program followed by arguments, for example ["python3", "-m", "unittest", "-v"].',
                    },
                    'cwd': {'type': 'string', 'description': 'Working directory relative to the workspace, such as .'},
                },
                'required': ['argv', 'cwd'],
                'additionalProperties': False,
            },
        },
    },
], indent=2) + '\n'


class InvalidTools(ValueError):
    """Tool definitions need editing before sending."""


def parse_tools(text):
    try:
        if not text.strip():
            raise ValueError(_('Paste, import, or load the example tools, then click Apply.'))
        tools = parse_json(text)
        if not isinstance(tools, list) or not tools:
            raise ValueError(_('Provide a nonempty JSON array of function tools.'))
        names = set()
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict) or tool.get('type') != 'function':
                raise ValueError(_('Tool {0} must have type "function".').format(index + 1))
            function = tool.get('function')
            if not isinstance(function, dict):
                raise ValueError(_('Tool {0} needs a function object.').format(index + 1))
            name = function.get('name')
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError(_('Function names must be nonempty and unique.'))
            names.add(name)
            if 'description' in function and not isinstance(function['description'], str):
                raise ValueError(_('Function descriptions must be strings.'))
            parameters = function.get('parameters')
            if not isinstance(parameters, dict) or parameters.get('type') != 'object':
                raise ValueError(_('Parameters for {0} must be an object schema.').format(name))
            schema_validator(parameters)
        return tools
    except (ValueError, RecursionError) as exc:
        raise InvalidTools(str(exc)) from exc


def valid_call(call):
    if not isinstance(call, dict):
        return False
    try:
        json.dumps(call, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return False
    function = call.get('function')
    return (isinstance(function, dict) and isinstance(function.get('name'), str)
            and bool(function['name'].strip()) and isinstance(function.get('arguments'), dict)
            and ('index' not in function or (type(function['index']) is int and function['index'] >= 0))
            and ('id' not in call or isinstance(call['id'], str)))


def inspect_calls(calls, tools, status):
    definitions = {t['function']['name']: t['function']['parameters'] for t in tools or []}
    checks = []
    for call in calls:
        if not valid_call(call):
            checks.append({'status': 'malformed', 'message': _('Malformed tool call; continuation is unavailable.')})
            continue
        function = call['function']
        if function['name'] not in definitions:
            checks.append({'status': 'unknown', 'message': _('Unknown function; a mock result can still be supplied.')})
            continue
        try:
            error = next(schema_validator(definitions[function['name']]).iter_errors(function['arguments']), None)
            if error is None:
                checks.append({'status': 'valid', 'message': _('Arguments match the parameter schema.')})
            else:
                path = '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in error.absolute_path)
                checks.append({'status': 'mismatch', 'message': path + ': ' + error.message})
        except Exception as exc:
            checks.append({'status': 'validation_error', 'message': str(exc)})
    state = 'pending' if all(valid_call(c) for c in calls) else 'invalid'
    if status != 'complete':
        state = 'incomplete'
    return {'state': state, 'validation': checks, 'results': [None] * len(calls)}


def wire_calls(message):
    """Incomplete/malformed model output is retained locally, never replayed as calls."""
    calls = message.get('tool_calls') or []
    metadata = message.get('response_metadata') or {}
    if (metadata.get('status', 'complete') != 'complete'
            or metadata.get('tool_round', {}).get('state') in ('invalid', 'incomplete')
            or not all(valid_call(c) for c in calls)):
        return []
    return copy.deepcopy(calls)


def result_messages(message, cancel=False):
    round = message['response_metadata']['tool_round']
    if round['state'] != 'pending':
        raise ValueError(_('This tool round has already been submitted.'))
    results = round['results']
    if not cancel and any(result is None for result in results):
        raise ValueError(_('Save a result for every tool call before continuing.'))
    messages = []
    for index, call in enumerate(message['tool_calls']):
        result = results[index]
        cancelled = result is None
        if cancelled:
            result = _('Tool result not provided: tool round cancelled by user.')
        item = {'role': 'tool', 'content': result, 'tool_name': call['function']['name'],
                'response_metadata': {'call_index': index, 'cancelled': cancelled}}
        if call.get('id'):
            item['tool_call_id'] = call['id']
        messages.append(item)
    return messages
