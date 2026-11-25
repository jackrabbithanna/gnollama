# Structured output

Structured output asks the model to answer in JSON, optionally matching a JSON Schema, and checks the result locally.

It is available in Chat and Response tabs on hosts that support it; see [what each host type supports](hosts-and-providers.md#what-each-host-type-supports). Ollama Cloud does not support structured output; see [Ollama's documentation](https://docs.ollama.com/capabilities/structured-outputs).

## Choosing a format

Under **Options**, below the image controls, choose **Text**, **JSON**, or **JSON Schema**.

Selecting **JSON Schema** opens the schema editor when no schema has been applied. Paste or import a schema and click **Apply**; use **Edit Schema** to change it later. If the schema is missing or invalid when you send, the editor shows the problem and keeps your prompt intact.

Describe the JSON you want in your prompt as well. Tureen does not rewrite prompts or change the temperature automatically.

## Schemas

The schema editor accepts self-contained JSON Schema objects. It defaults to Draft 2020-12 and honors supported explicit `$schema` versions. References must resolve within the imported document; external schemas are not fetched.

Local validation does not guarantee that the host's generation engine supports every JSON Schema keyword.

## Results

Completed responses show JSON syntax or schema errors with their locations.

- Valid JSON can be copied or saved, even when it does not match the schema.
- Raw text can always be copied.
- Stopped or failed responses stay marked incomplete.
- JSON formatting rejects duplicate keys and non-finite or unrepresentably large floating-point values, so their meaning is never silently changed.

The raw response and its validation result are kept in the saved chat history.

Structured output can be combined with [tool calling](tool-calling.md) and with [knowledge sources](knowledge.md#answers-and-sources); source information appears in a separate panel rather than as extra schema fields.
