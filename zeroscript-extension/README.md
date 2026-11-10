# ZeroScript Workspace Agent extension

This Manifest V3 extension connects supported AI chat pages to the local ZeroScript Workspace Agent bridge.

## Loading locally

1. Open `edge://extensions` or `chrome://extensions`.
2. Enable Developer mode.
3. Click **Load unpacked**.
4. Select this `zeroscript-extension` directory.
5. Reload any already-open AI chat tab.

Start the local bridge separately:

```powershell
start.bat "C:\path\to\project"
```

Open a new supported chat and click **Start workspace agent**.

## Important files

```text
core/workspace-config.js   coding-agent prompt, feedback and tool categories
core/workspace-parser.js   JSON command and ###PATCH### parsing
core/main.js               agent loop, session UI and local approval dialog
providers/*.js             per-site DOM and send/receive adapters
background.js              resilient WebSocket connection to 127.0.0.1
```

`manifest.json` loads the workspace-specific prompt and parser modules on every supported site.

## Approval behavior

When the bridge returns `approval_required`, `core/main.js` displays the exact operation with `window.confirm`. The decision is sent through `background.js` as `approve_operation`. The bridge owns and validates the one-time token; the extension cannot broaden or reuse it.

Every `run_command`, `move_path`, `trash_path` and `delete_file` invocation requires a fresh decision. Version 2.1 also translates DeepSeek DSML read calls into normal workspace calls.

## Smoke tests

```bash
node test-workspace-parser.js
node test-chatgpt.js
```

No npm dependencies are required for these tests.
