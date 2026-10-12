# Agent

[简体中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

Agent is a local-first AI assistant for your desktop. Give it a task in plain language: research a topic, make sense of your documents, organize files, or work through a longer task step by step.

Use Agent from the macOS app, an iPhone or iPad, or a web browser. Models can run on your Apple Silicon Mac or on another computer with MLX or CUDA. Which features are available depends on the model, connected services, and permissions you grant.

## What you can do

- **Research and read** — Ask questions about websites, PDFs, office documents, images, and audio.
- **Build a personal knowledge base** — Save material, organize it by topic, and find it again in later conversations.
- **Work through larger tasks** — Follow progress and review actions that need your approval; pause or stop a task when needed.
- **Get files and presentations** — Find generated files on the Files page and preview slides in Deck.
- **Schedule recurring work** — Set up regular tasks and review their results and history.
- **Connect your own services** — Add private API tools for services you choose.

For example, ask Agent to compare the conclusions in a few PDFs and draft a report, outline a presentation from your notes, or prepare a weekday news roundup.

## Getting started

### Use an app

The macOS app can run Agent on your Mac or connect to a separate computer for model processing. Open Settings and configure the services you want to use. For remote processing, enter the computer’s address and connection keys.

The iPhone and iPad apps are clients: they connect to an Agent that is already running elsewhere and do not load models on the device. For build and connection details, see the [Apple app guide](apple-app/README.md).

If Agent is already set up, open its web address in a browser. On the same computer, the default address is `http://localhost:5173`.

### Run Agent yourself

You’ll need Python 3.11–3.13, Node.js/npm, and enough memory for your chosen model. The first model load may download large files.

From the repository, install the dependencies:

```bash
./devctl.sh setup
```

Then choose a setup:

| Setup | Configure and start |
|---|---|
| One Mac | Set `inference.mode` to `inprocess` in `config.yaml`. Leave `rag.compute.host` empty and remove any `RAG_COMPUTE_HOST` override from `.env.local`. Then run `./devctl.sh start`. |
| A separate computer for model processing | Configure the addresses and keys on both computers. On the other computer, run `./devctl.sh start --profile gpu`. On the computer running Agent, run `./devctl.sh start --profile edge -h <other-computer-address>`. |

The checked-in `config.yaml` starts in remote mode with no address for the other computer. Remote mode will not start until you provide a valid address; it does not switch to local inference automatically. For address and key details, see [Deployment architecture](ARCHITECTURE.md). After startup, open `http://localhost:5173`. To check or stop services, use `./devctl.sh status` or `./devctl.sh stop`.

For logs, run `./devctl.sh logs backend`.

## Everyday use

Attach the material you want Agent to use and describe the result you need. Find generated files on the Files page; open Deck to preview slides.

When Agent asks you to approve an action, review what it will do before approving. Scheduled tasks do not automatically approve actions that require your confirmation.

Stopping a task prevents further work when it reaches a safe stopping point. It does not undo files already written or actions already sent to an external service. If you retry, Agent keeps the original attachments and generation settings, and may call tools again. A successful retry becomes the conversation’s current result; you can still view earlier attempts.

Sessions and their files have a retention period. Pin important sessions to keep them; the app shows the applicable retention details.

If you change the knowledge-base model, follow the prompt to rebuild the index. The macOS knowledge-base page can check its status and start a rebuild. If you choose “Later,” you can return to that page to rebuild when you’re ready. For a self-hosted setup, use `./devctl.sh model` to choose the chat model and `./devctl.sh rag-model` to choose the knowledge-base model tier.

To connect a private API, enter its details and key in the API tools section of the macOS or iOS app’s settings. After confirming a new tool, you can continue setting it up without starting over. Seeing a tool in the list does not mean its connection or key has been verified. A test call may perform a real operation, so review what it will do before running one.

## Data and privacy

Session records and the knowledge base are kept on the computer running Agent. If another computer handles model processing, the information needed for the task is sent there. Web search, private API tools, and the optional cloud advisor also contact external services when you use them.

The other computer may temporarily keep media used for inference. KV-cache persistence is off by default; if you enable it, session-related cache data can be saved there. See [Deployment architecture](ARCHITECTURE.md) for retention and clearing details.

Only expose Agent through a network you trust. The web entry point uses a key stored by the server, so anyone who can reach that entry point can use Agent, including its powerful tools. Do not expose it directly to the public internet.

## More information

- [Apple app guide](apple-app/README.md) — macOS, iPhone, and iPad setup and connections.
- [Knowledge base](rag/README.md) — managing sources, model tiers, and rebuilds.
- [Private API tools](api_tools/README.md) — connecting your own services.
- [Deployment architecture](ARCHITECTURE.md) — one-computer and split-compute setups.
