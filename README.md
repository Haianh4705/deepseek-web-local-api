# DeepSeek Web Local API (`deepseek-web-local-api`)

Turn an active **DeepSeek Web (`https://chat.deepseek.com`)** browser session into a **Local OpenAI-Compatible API Server (`http://127.0.0.1:8765/v1`)** purpose-built for **DeepSeek Harness (`dsh`)**, **Pigent (`pi` coding agent)**, and other agentic coding assistants.

---

## ✨ Key Features

- **OpenAI `/v1/chat/completions` & `/v1/models` Compatible**:
  - Supports both `stream: true` and `stream: false`, streaming live `reasoning_content` (`<think>` tokens) in real time to the CLI / TUI.
  - Exposes all standard DeepSeek models (`deepseek-reasoner`, `deepseek-chat`, `deepseek-v4-pro`, `deepseek-r1`, etc.).
- **In-Browser WASM PoW Solver (`DeepSeekHashV1` SHA-3)**:
  - Solves DeepSeek's proof-of-work challenge directly inside the authenticated `chat.deepseek.com` tab in milliseconds.
  - Automatically inherits Cloudflare clearance, session cookies, and `SMSdk` device fingerprint headers.
- **Dynamic Cryptographic Signature (`x-hif-leim`) Capture**:
  - Dynamically extracts DeepSeek's anti-bot cryptographic signature via Rspack internals (`addSSEHeader()`), ensuring genuine browser request parity and avoiding backend authorization drops.
- **Concurrency & Anti-Ban Subagent Guard**:
  - **Full Turn Locking (`hub.lock`)**: Serializes concurrent requests (subagent tasks, model probes, parallel tool calls) through an asynchronous FIFO queue. Prevents overlapping browser requests that trigger rate-limit bans or session corruptions.
  - **Per-Request Abort Controller Tracking**: Maps abort signals per request ID (`activeControllers = new Map()`), eliminating `[Local-AI Error: signal is aborted without reason]` when concurrent requests arrive.
  - **Anti-Ban Directives**: Automatically injects concurrency safeguards into prompts to instruct subagents to work sequentially and avoid high-frequency ping-pong loops.
- **Stateful Multi-Branch Session Chaining (`SessionState`)**:
  - Automatically chains multi-turn agent turns onto DeepSeek Web `chat_session_id`.
  - Multi-session branch caching keeps subagent threads isolated so interleaved calls don't discard active session IDs, preserving the **128K context window** and minimizing input token churn.
- **Self-Healing Tool Calling & JSON Repair**:
  - Robust parser for standard OpenAI JSON, flat JSON (`{"name": "bash", "command": "..."}`), Anthropic `tool_use` blocks, and native DeepSeek DSML (`<｜DSML｜invoke>`).
  - **`_repair_json_brackets` Stack Balancer**: Automatically fixes nested JSON outputs (e.g. `ask_user_question` with 8+ nested options) when models produce unmatched closing brackets (`]}` vs `}]`).
- **Resilient Auto-Recovery & Smart Backoff**:
  - Adaptive pacing between rapid agent turns (2.8s–6.0s spacing).
  - Multi-tier exponential backoff (`[6s, 12s, 20s, 30s, 45s, 60s]`) for both burst rate-limiting (`"Tin nhắn quá thường xuyên"`) and temporary DeepSeek server outages (`"Server is temporarily unavailable."` / `backend_err`).
- **Seamless Server Daemon & Port Management (`--restart`)**:
  - Automatically detects when port `8765` is in use.
  - Run `python3 server.py --restart` to cleanly kill existing listening daemons without disrupting Chrome browser connections.

---

## 📁 Project Structure

| File | Description |
| :--- | :--- |
| `server.py` | Local OpenAI API Server (`http://127.0.0.1:8765/v1`), WebSocket Hub (`ws://127.0.0.1:8765/ws`), SSE parser, tool adapter, and queue manager |
| `bridge.user.js` | Browser bridge content script for `https://chat.deepseek.com` that solves WASM PoW, signs headers, and streams SSE to WebSocket |
| `manifest.json` | Chrome Extension (Manifest V3) configuration for loading the bridge directly as an unpacked extension |

---

## 🚀 Quick Start

### Step 1: Install Python Dependencies
Only `fastapi`, `uvicorn`, and `websockets` are required (pure Python standard library for everything else):
```bash
pip install fastapi uvicorn websockets
```

Run the built-in self-test suite (verifies SSE patch parser, session chaining, tool extraction, bracket repairing, and rate-limit recovery):
```bash
python3 server.py --test
```

---

### Step 2: Load the Browser Bridge in Chrome

**Option A: Load as an Unpacked Chrome Extension (Recommended)**
1. Open Chrome and navigate to `chrome://extensions/`
2. Enable **Developer mode** in the top-right corner.
3. Click **Load unpacked** and select this repository folder (`deepseek-web-local-api`).
4. Open `https://chat.deepseek.com`, sign in to your DeepSeek account, and refresh the page (`F5` / `Cmd+R`).

**Option B: Tampermonkey / Violentmonkey**
- Create a new userscript, paste the contents of `bridge.user.js`, save, and reload `https://chat.deepseek.com`.

---

### Step 3: Start the Local API Server

Start the server:
```bash
python3 server.py
```

Or restart if an existing background server is holding port `8765`:
```bash
python3 server.py --restart
```

Once running and `https://chat.deepseek.com` is open in Chrome:
- Terminal prints: `[Local-AI] 🟢 Chrome tab connected to /ws`
- A green badge **`🟢 Local AI (API)`** appears in the bottom-right corner of `chat.deepseek.com`.

Verify server health:
```bash
curl http://127.0.0.1:8765/health
# {"ok":true,"bridge_connected":true,"authenticated":true,"session_id":null,"turns_in_session":0,"queue_waiting":0}
```

Test a quick CLI question:
```bash
python3 server.py --ask "Hello DeepSeek, are you ready?"
```

---

## 🪢 DeepSeek Harness (`dsh`) Configuration

[DeepSeek Harness (`dsh`)](https://github.com/deepseek-ai) can be configured to route both CLI commands and Web UI sessions through your local server without external API keys.

### 1. Patch Profiles (`cordis.patch.yml`)

Configure both headless and web profiles under `~/.dsh/profiles/headless/cordis.patch.yml` and `~/.dsh/profiles/web/cordis.patch.yml`:

```yaml
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      deepseek-web-local:
        displayName: DeepSeek Web Local
        api: openai-completions
        baseURL: http://127.0.0.1:8765/v1
        apiKeyEnv: DEEPSEEK_WEB_LOCAL_KEY
        models:
          - id: deepseek-reasoner
            name: DeepSeek R1 (DeepThink)
          - id: deepseek-chat
            name: DeepSeek V3 (Chat)
          - id: deepseek-v4-pro
            name: DeepSeek V4 Pro
          - id: deepseek-reasoner-search
            name: DeepSeek R1 (Search)
          - id: deepseek-chat-search
            name: DeepSeek V3 (Search)

- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: deepseek-web-local
    model: deepseek-reasoner

- id: web-search-deepseek
  name: "@deepseek-ai/dsh-web-search-deepseek"
  config:
    baseURL: http://127.0.0.1:8765/v1
    apiKeyEnv: DEEPSEEK_API_KEY
```

### 2. Configure Credentials in `~/.dsh/.credentials.yaml`

```yaml
version: 1
refs:
  DEEPSEEK_WEB_LOCAL_KEY: local
  DEEPSEEK_API_KEY: local
  OPENAI_API_KEY: local
  ANTHROPIC_API_KEY: local
```

### 3. Grant Plugin Risk Exemptions (for DSH 0.2.x)

```bash
dsh plugin --profile web allow-version @deepseek-ai/dsh-llm-pi-ai 0.2.0-rc.2 --accept-risk
dsh plugin --profile web allow-version @deepseek-ai/dsh-agent-default-model 0.2.0-rc.2 --accept-risk
dsh plugin --profile web allow-version @deepseek-ai/dsh-web-search-deepseek 0.2.0-rc.2 --accept-risk

dsh plugin --profile headless allow-version @deepseek-ai/dsh-llm-pi-ai 0.2.0-rc.2 --accept-risk
dsh plugin --profile headless allow-version @deepseek-ai/dsh-agent-default-model 0.2.0-rc.2 --accept-risk
dsh plugin --profile headless allow-version @deepseek-ai/dsh-web-search-deepseek 0.2.0-rc.2 --accept-risk
```

### 4. Running `dsh`

- **CLI One-Shot Execution**:
  ```bash
  dsh headless "explain the repository structure in 3 bullet points"
  ```
- **Web Interface**:
  ```bash
  dsh web
  ```

---

## 🤖 Pigent (`pi` Coding Agent) Configuration

### 1. Register Provider in `~/.pi/agent/models.json`

```json
{
  "providers": {
    "deepseek-web-local": {
      "baseUrl": "http://127.0.0.1:8765/v1",
      "api": "openai-completions",
      "apiKey": "local",
      "models": [
        {
          "id": "deepseek-reasoner",
          "name": "DeepSeek R1 / V3.2 Reasoner (Web Local)",
          "reasoning": true,
          "thinkingLevelMap": {
            "off": "none",
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max"
          },
          "input": ["text"],
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
          "contextWindow": 131072,
          "maxTokens": 16384,
          "compat": { "requiresToolResultName": true }
        },
        {
          "id": "deepseek-chat",
          "name": "DeepSeek V3.2 Chat Fast (Web Local)",
          "reasoning": false,
          "input": ["text"],
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
          "contextWindow": 131072,
          "maxTokens": 16384,
          "compat": { "requiresToolResultName": true }
        }
      ]
    }
  }
}
```

### 2. Set Default Model in `~/.pi/agent/settings.json`

```json
{
  "hideThinkingBlock": false,
  "defaultThinkingLevel": "medium",
  "httpIdleTimeoutMs": 0,
  "defaultProvider": "deepseek-web-local",
  "defaultModel": "deepseek-reasoner"
}
```

---

## 📊 Context Limits & Supported Models

| Model ID | DeepThink | Web Search | Context Window | Max Output / Turn |
| :--- | :---: | :---: | :---: | :---: |
| `deepseek-reasoner` / `deepseek-r1` | ✅ Enabled | ❌ Disabled | `131,072` (128K) | `16,384` – `32,768` |
| `deepseek-chat` / `deepseek-v3` / `deepseek-v3.2` | ❌ Disabled | ❌ Disabled | `131,072` (128K) | `8,192` – `16,384` |
| `deepseek-v4-pro` | ✅ Enabled | ❌ Disabled | `131,072` (128K) | `16,384` – `32,768` |
| `deepseek-flash` / `deepseek-v4-flash` | ❌ Disabled | ❌ Disabled | `131,072` (128K) | `8,192` – `16,384` |
| `deepseek-coder` | ❌ Disabled | ❌ Disabled | `131,072` (128K) | `8,192` – `16,384` |
| `deepseek-reasoner-search` | ✅ Enabled | ✅ Enabled | `131,072` (128K) | `16,384` |
| `deepseek-chat-search` | ❌ Disabled | ✅ Enabled | `131,072` (128K) | `16,384` |

---

## 🛡️ Anti-Ban Best Practices

1. **Avoid Running Too Many Parallel Subagents**:
   - DeepSeek Web rate-limits accounts that spawn multiple subagents simultaneously. The built-in FIFO lock (`hub.lock`) serializes requests, but delegating tasks sequentially rather than in large parallel batches keeps your account healthy.
2. **Consolidate Tool Turns**:
   - Combine file reads, bash operations, and multi-file edits into single consolidated tool calls to prevent rapid micro-turn ping-ponging.
3. **Automatic Outage & Rate-Limit Handling**:
   - If DeepSeek shows `"Server is temporarily unavailable"` or `"Tin nhắn quá thường xuyên"`, the server automatically pauses and retries with exponential backoff (`[6s, 12s, 20s, 30s, 45s, 60s]`). Do not force-cancel or repeatedly spam requests during backend outages.

---

## 📜 License
MIT License. Created for local research and AI agent pair programming.
