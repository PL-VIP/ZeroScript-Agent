# ZeroScript Workspace Agent

ZeroScript Workspace Agent turns a supported browser AI chat into a local coding agent. The model can inspect, search and edit one user-selected project directory through a local bridge, without receiving general access to the computer.

The project is a fork of ZeroScript Free. Version 2.1 uses a filesystem workspace sandbox instead of the mandatory Roblox Studio backend.

## What it can do

- list project files and directories;
- read complete UTF-8 files or selected 1-based line ranges;
- search file names and text;
- inspect Git status and diffs without modifying Git;
- apply transactional multi-file unified diffs;
- replace specific lines using a required file revision;
- move files or send them to recoverable application trash after local approval;
- propose test/build commands;
- run a command only after the user approves that exact invocation.

Every path used by the model is relative to the selected workspace. Absolute paths, `..` traversal, escapes through symlinks/junctions, NTFS alternate streams and configured secret files are rejected.

## Architecture

```text
Supported AI chat
      │
      ▼
browser extension and provider adapter
      │ WebSocket on 127.0.0.1
      ▼
agent_bridge.py
      ├─ workspace identity and sessions
      ├─ one-time approval tokens
      ├─ operation audit log
      └─ workspace tools
             ├─ safe path guard
             ├─ read/search/Git
             ├─ revisions and atomic edits
             └─ approved command runner
```

The existing provider adapters for ChatGPT, DeepSeek, Gemini, Kimi, GLM, Qwen, Arena and Meta AI are retained. The extension still uses plain command blocks in model responses, so no model API key is required.

## Requirements

- Windows or macOS/Linux;
- Python 3.10 or newer;
- Microsoft Edge or Google Chrome;
- a supported AI site account;
- optional: Git for `git_status` and `git_diff`;
- optional: Docker for container command execution.

The launchers install the single Python dependency, `websockets`, when it is missing.

## Installation

### 1. Load the extension

1. Open `edge://extensions` or `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the `zeroscript-extension` directory from this repository.

Reload supported AI tabs after updating the extension.

### 2. Start the bridge with a workspace

Windows:

```powershell
start.bat "C:\path\to\your\project"
```

macOS/Linux:

```bash
./MacOS_Start.command "/path/to/your/project"
```

If no path is supplied, `workspace.root` from `config.json` is used. The included configuration uses `.` and therefore selects this repository.

The `ZS_WORKSPACE` environment variable can also select the directory:

```powershell
$env:ZS_WORKSPACE = "C:\path\to\project"
python agent_bridge.py
```

### 3. Start an agent session

1. Open a new, empty conversation on a supported AI site.
2. Confirm that the ZeroScript status dot is green and shows the workspace name.
3. Click **Start workspace agent**.
4. Wait for the model to call `list_commands` and report that it is ready.

## Command approval

`run_command` never executes immediately. The bridge first issues a one-time approval request and the extension displays:

- the exact executable and argument array;
- working directory;
- host or Docker runner mode;
- requested network access;
- timeout;
- a risk warning.

The user can choose only **OK for this invocation** or **Cancel**. Approval is tied to the exact workspace, command arguments and request token. It expires after 60 seconds, is removed when the socket disconnects and cannot be reused. An identical later command requires another approval.

`move_path`, `trash_path` and permanent `delete_file` use the same local approval mechanism. `delete_file` additionally requires the exact current file revision.

## Command runner modes

Configure `runner.mode` in `config.json`.

### Host mode

```json
{
  "runner": {
    "mode": "host",
    "timeout_ms": 120000
  }
}
```

Host mode is broadly compatible, but the process itself is not an OS sandbox and may access resources or the network outside the workspace. The approval dialog clearly warns about this. The runner passes a reduced environment by default instead of forwarding tokens and arbitrary environment variables. Use host mode only for commands you understand.

### Docker mode

```json
{
  "runner": {
    "mode": "docker",
    "image": "python:3.12-slim",
    "timeout_ms": 120000,
    "memory": "1g",
    "pids_limit": 256
  }
}
```

Docker mode mounts the selected workspace at `/workspace`, applies memory/process limits and disables networking unless `network: true` was explicitly requested and approved. Pick an image containing the toolchain required by the project.

Set the mode to `disabled` to prevent all command execution:

```json
{"runner": {"mode": "disabled"}}
```

## Workspace configuration

The important `config.json` fields are:

```json
{
  "workspace": {
    "root": ".",
    "max_file_bytes": 250000,
    "exclude": [".git", "node_modules", ".venv", "dist", "build"],
    "secret_patterns": [".env", ".env.*", "*.pem", "*.key"]
  },
  "limits": {
    "max_output_bytes": 200000
  },
  "runner": {
    "mode": "host",
    "timeout_ms": 120000
  }
}
```

The bridge stores audit records and recoverable trash outside the workspace:

- Windows: `%LOCALAPPDATA%\ZeroScript-Agent\workspaces\<workspace-id>`;
- macOS: `~/Library/Application Support/ZeroScript-Agent/workspaces/<workspace-id>`;
- Linux: `$XDG_STATE_HOME/zeroscript-agent/workspaces/<workspace-id>`.

Patch and file contents are redacted from audit argument records.

## Agent tools

| Tool | Purpose | Approval |
|---|---|---|
| `workspace_info` | Workspace and runtime capabilities | No |
| `list_files` | Bounded directory listing | No |
| `read_file` | Line-range read plus revision | No |
| `batch_read` | Up to 10 safe read-only calls, including translated DSML | No |
| `search_files` | Glob-based file search | No |
| `search_text` | Literal text search | No |
| `stat_path` | File/directory metadata | No |
| `git_status` | Branch and porcelain status | No |
| `git_diff` | Staged or unstaged diff | No |
| `apply_patch` | Transactional unified diff | No |
| `replace_lines` | Revision-checked line replacement | No |
| `move_path` | Move inside workspace | Every call |
| `create_directory` | Create a directory and optional parents | No |
| `delete_file` | Permanently delete one revision-checked file | Every call |
| `trash_path` | Recoverable removal | Every call |
| `run_command` | Host or Docker command | Every call |

`apply_patch` does not delete files. Prefer recoverable `trash_path`; use `delete_file` only for deliberate permanent deletion. Both require approval.

## Editing guarantees

- `read_file` returns readable numbered lines and a SHA-256 revision without JSON-escaped newlines.
- `replace_lines` refuses stale revisions.
- `apply_patch` validates every hunk before writing any file.
- multi-file patches roll back if a write fails;
- writes use a temporary file and atomic replacement;
- UTF-8 BOM and CRLF/LF are preserved for existing files;
- binary files are rejected by text tools;
- operation IDs prevent automatic duplicate execution after a retry.

## Development and tests

Run all Python tests:

```bash
python -m unittest discover -s tests -v
```

Run extension smoke tests:

```bash
node zeroscript-extension/test-workspace-parser.js
node zeroscript-extension/test-chatgpt.js
```

Check Python and JavaScript syntax:

```bash
python -m py_compile agent_bridge.py zeroscript_agent/*.py
node --check zeroscript-extension/core/workspace-config.js
node --check zeroscript-extension/core/workspace-parser.js
node --check zeroscript-extension/core/main.js
node --check zeroscript-extension/background.js
```

## Current limitations

- Text tools support UTF-8 files. Other encodings and binary editing are intentionally rejected.
- Host runner mode is approval-gated but not OS-isolated. Use Docker mode for stronger command isolation.
- External MCP addon management from the extension is disabled in 2.1 while the workspace security model is stabilized.
- Browser-site DOM changes can require updates to individual provider adapters.

## License and attribution

GPL-3.0-or-later. This fork retains the license and provider integration work from [ZeroScript Free](https://github.com/sebattfg/ZeroScript-Free).
