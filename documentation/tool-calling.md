# Tool calling playground

The tool calling playground lets you test how a model uses function tools. You define the tools, the model calls them, and you supply the results by hand. Tureen never executes functions or connects to MCP servers, so you can safely test any tool definition.

Tool calling is available in Chat tabs on every host type, for models that support it.

## Trying it out

1. Open **New Chat**, select a tool-capable model, and enable **Tool Calling** below the image controls. The definition editor opens automatically when it's empty.
2. Click **Example**, then **Apply**. This loads five coding-harness tools: `list_files`, `read_file`, `search_code`, `replace_in_file`, and `run_command`.
3. Send: **Inspect calculator.py. Its add function subtracts instead of adding. Read the file, fix it with the available tools, then run python3 -m unittest -v and report the test result.**
4. Inspect each call and its argument check. Click **Enter Result…**, supply a mock result, and click **Save Result**.
   - For `read_file`, use the sample file contents below.
   - For a correct `replace_in_file` call, use `{"success":true,"replacements":1}`.
   - For `run_command`, use `{"exit_code":0,"stdout":"","stderr":"Ran 3 tests in 0.001s\n\nOK\n"}`.
5. Click **Continue** after each round. The model receives the original call and your mock result, and can answer or request another tool round. When there are several calls, save a result for each before continuing. Empty strings are valid results when explicitly saved.

Sample contents to supply for `read_file`:

```python
def add(a, b):
    return a - b
```

## Definitions

Definitions use an Ollama-style `tools` array with unique function names and self-contained object parameter schemas. Import and export definitions as JSON so you can reuse them across chats. Applied definitions and saved mock results persist per chat, including chats closed before the first prompt.

## Rounds

- Each continuation uses the currently selected host, model, definitions, thinking, and generation settings. Earlier calls keep their original arguments and validation.
- While a round is pending, supply its results or choose **Cancel Tool Round** before sending another prompt. Cancelling keeps the results you supplied and records cancellation text for unanswered calls; it does not contact the server.
- Reopening a pending round from history never starts a request automatically.
- Incomplete and malformed calls can be inspected but not continued.

## Combining with other features

Text, JSON, and JSON Schema output can all be combined with tools. Final-output validation applies to completed responses without tool calls, and server compatibility errors stay visible. Tool results accept any text and are not constrained by the output schema. Continuations with [knowledge sources](knowledge.md) reuse the same retrieval snapshot.

## What to expect

Tool-capability notices are advisory, so you can test how a server behaves. Model and server support vary: a model may answer without calling a tool, or a server may reject the `tools` parameter.
