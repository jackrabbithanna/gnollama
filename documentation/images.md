# Image generation

The **Images** section generates images from a prompt.

Image models are offered by OpenAI, OpenRouter, and the Vercel AI Gateway (see [what each host type supports](hosts-and-providers.md#what-each-host-type-supports)). They are listed in this section only. Other host types appear in the host selector with the reason they cannot be used.

## Generating

Choose **New Image** (Ctrl+Shift+G). Enter a prompt, attach reference images if the model takes them, and choose any of the settings the model offers:

- aspect ratio and resolution
- quality
- output format and background
- number of images
- seed

A setting left at **Model default** is not sent. The output format is PNG unless you choose another. Anything the model cannot take holds **Generate** back and explains why. References other than PNG and JPEG are converted to PNG before they are sent.

## Results

Each run shows its prompt, references, settings, any text the model wrote, and the cost where the server reports it. Every image can be:

- saved exactly as it came
- copied
- used as a reference for the next run

**Stop** cancels a run.

## History

Sessions appear in the **Images** section under **Recent images**, with drafts, search, pinning, rename, deletion, and Markdown/JSON export. Reopening a session sends nothing.
