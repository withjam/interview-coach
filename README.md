# Sidework

Listen to a selected audio input, transcribe speech in real time with [Speechmatics](https://speechmatics.com/) streaming, and stream a chat session to [Ollama Cloud](https://ollama.com/).

## Prerequisites (macOS)

- **macOS** (Intel or Apple Silicon)
- **UV** — Python package and project manager ([uv.pydata.org](https://uv.pydata.org/))

## Install UV on Mac

Using the official installer (recommended):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or with Homebrew:

```sh
brew install uv
```

Restart your terminal or run `source ~/.zshrc` so the `uv` command is available.

## Install and run with UV

From the project directory:

```sh
# Create a virtual environment and install dependencies from requirements.txt
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt

# Run the app
python coach.py
```

Or use `uv run` to create/use a venv and run in one step (no manual activate):

```sh
uv run coach.py
```

`uv run` will use the project’s `.venv` if it exists, or create one and install dependencies as needed.

## Environment variables

Set these before running:

| Variable | Description |
|----------|-------------|
| `OLLAMA_API_KEY` | Ollama Cloud API key from [ollama.com/settings/keys](https://ollama.com/settings/keys) |
| `SPEECHMATICS_API_KEY` | Speechmatics API key from [speechmatics.com](https://speechmatics.com/) |
| `OLLAMA_CHAT_MODEL` | (optional) Chat model name; default: `llama3.2` |
| `SPEECHMATICS_LANGUAGE` | (optional) Language code; default: `en` |

Example:

```sh
export OLLAMA_API_KEY="your-ollama-key"
export SPEECHMATICS_API_KEY="your-speechmatics-key"
uv run coach.py
```

## Bun alternative

A JavaScript entrypoint is also available:

```sh
bun install
bun run coach.bun.js
```

See [bun.sh](https://bun.sh) for installing Bun on macOS.
