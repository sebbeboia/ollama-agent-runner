# ollama-agent-runner

A local, tool-using agent loop for [Ollama](https://ollama.com) models, built for
Kali Linux. Give it a task, and it plans, calls real tools (shell, files, web,
recon, git, Python, …), reads the results, and iterates until done — all against
a locally hosted model.

It is designed to run **reliably regardless of model choice**: whether the model
speaks Ollama's native function-calling channel, emits `<tool>` XML, or writes
tool calls as JSON in plain text, the runner parses and executes them.

## Architecture

| File | Role |
|------|------|
| `agent_core.py` | The engine — tool definitions, the agent loop, Ollama client, safety guard, memory/self-learning. |
| `agent_run.py`  | CLI entry point. `--model`, task as args, or interactive mode. |
| `agent_llama3.py` | Lightweight runner for models that only do `<tool>` XML (e.g. `llama3-agent`). |
| `ai` | Bash launcher that picks a model and lists skills/models. |
| `Modelfile*` | Ollama model definitions (system prompt + tuned parameters). |

## How it works

Each round, the loop:

1. Sends the conversation to the model (streaming tokens live).
2. Extracts tool calls using a **three-layer parser** (first match wins):
   - **native** — Ollama's structured `tool_calls`
   - **XML** — `<tool name="...">args</tool>` in the text
   - **JSON** — `{"name": "...", "arguments": {...}}` in the text
3. Executes the tool and feeds the real result back to the model.
4. Repeats until the model writes `<DONE>` or `MAX_ITERATIONS` (40) is reached.

### Reliability & speed features

- **Retry with backoff** — transient Ollama failures (connection reset,
  model-reload timeout) are retried up to 3× (2s → 4s); real 4xx errors are
  raised immediately.
- **Streaming** — tokens are printed as they are generated instead of waiting
  for the full response. Behaviour-preserving: `tool_calls` are captured
  identically in streaming and non-streaming mode.
- **Tool-support auto-fallback** — models that reject the `tools` field with
  `400 does not support tools` are automatically retried in plain-text mode, so
  the runner degrades gracefully instead of crashing.
- **Loop detection** — the same tool call repeated 3× triggers a nudge, 4×
  stops the agent.
- **`<DONE>` vs. tool-call precedence** — a real tool call always runs before a
  same-round `<DONE>`, so the agent never finishes on a hallucinated result.

## Usage

```bash
# One-shot task
python3 agent_run.py --model qwen2.5-coder:7b "Summarize the files in /var/log"

# Interactive session
python3 agent_run.py --model qwen2.5-coder:7b

# List available tools and exit
python3 agent_run.py --tools

# Via the launcher
./ai "your prompt here"      # combined model
./ai --code "your prompt"    # local coder model
./ai --models                # list installed models
./ai --list                  # list skills
```

### Recommended models

`agent_core.py` works with any model, but native function-calling is cleanest
with tool-supporting models: **`qwen2.5-coder:7b`**, **`mistral`**,
**`llama3.2:3b`**, and the `*-sec` variants built from them. Models like
`llama3-agent` (no native tool support) run fine via the XML path thanks to the
auto-fallback.

## Tools

`bash`, `python_exec`, `read_file`, `write_file`, `append_file`, `list_dir`,
`find_files`, `grep_file`, `copy_move`, `delete_path`, `diff_text`,
`web_search`, `web_fetch`, `http_headers`, `dns_lookup`, `whois_lookup`,
`port_scan`, `subdomain_enum`, `json_query`, `regex_extract`, `sqlite_query`,
`hash_tool`, `base64_tool`, `git_tool`, `plan`, `analyze_directory`, `learn`,
`recall`, `self_improve`, `orchestrate`, `list_skills`, `skill`.

A safety guard blocks obviously destructive shell patterns (`rm -rf /`, `mkfs`,
`dd if=`, fork bombs, piping remote scripts to a shell, etc.).

## Ollama tuning (host)

These service-level settings keep a 7B/8B model warm on a 6 GB GPU and stable:

```
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
OLLAMA_KEEP_ALIVE=10m
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
```

The client also pins `num_ctx=8192` (not 32768 — larger forces heavy CPU/RAM
offload) and low temperature for stable tool formatting.

## Setup

Requires Python 3.11+, a running Ollama server, and:

```bash
pip install requests rich
```

Some tools shell out to external binaries (`dig`, `whois`, `subfinder`, `nmap`)
that must be on `PATH`; they degrade gracefully if missing.

> **Note:** secrets (API keys), virtualenvs, and runtime state are excluded via
> `.gitignore` — never commit an `api`/`.env` file.
