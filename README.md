# DeepSeek Web Local API (`deepseek-web-local-api`)

Turn an active **DeepSeek Web (`https://chat.deepseek.com`)** browser session into a **Local OpenAI-Compatible API Server (`http://127.0.0.1:8765/v1`)** purpose-built for **Pigent (`pi` coding agent)** and other AI coding assistants.

---

## ✨ Key Features

- **OpenAI `/v1/chat/completions` Compatible**: Supports both `stream: true` and `stream: false`, streaming live `reasoning_content` (`<think>` tokens) in real time to `pi`'s TUI.
- **In-Browser WASM PoW Solver (`DeepSeekHashV1` SHA-3)**: Solves DeepSeek's proof-of-work challenge directly inside the authenticated `chat.deepseek.com` tab—no manual cookie extraction required, automatically inheriting Cloudflare clearance and `SMSdk` device fingerprint headers.
- **Stateful Session Chaining (`parent_message_id`)**: Automatically chains multi-turn agent steps onto the active DeepSeek Web `chat_session_id`. Only the initial `ROOT` turn sends the full System Prompt and Skills; subsequent `CHAIN` turns send only incremental `[TOOL_RESULT]` deltas, preserving the full **128K context window** and minimizing latency.
- **Multi-Format Tool Calling Adapter for `pi` Agent**:
  - Parses standard OpenAI-style JSON (`{"name": "...", "arguments": {...}}`), flat JSON (`{"name": "bash", "command": "..."}`), native DeepSeek DSML (`<｜DSML｜invoke>`, `<｜｜DSML｜｜ parameter>`), and hybrid JSON + DSML outputs.
  - Safely handles nested markdown code fences (```` ``` ````) inside `write` / `edit` payloads using `json.JSONDecoder().raw_decode` and coerces non-string parameters (`array`, `number`, `boolean`) against the tool's JSON Schema.
- **Smart Pacing & Automatic Rate-Limit Recovery**:
  - Paces rapid back-to-back tool execution turns so DeepSeek Web's burst limiter is avoided.
  - Automatically retries rate-limited requests with exponential backoff (`[5s, 10s, 16s, 24s, 35s]`) while keeping the stream alive, preventing multi-step coding tasks from aborting.
- **Interactive Thinking Level Control (`Shift+Tab` in `pi`)**:
  - Maps `pi` thinking levels directly to DeepSeek Web: `off` disables DeepThink (`thinking_enabled: false`), while `minimal`, `low`, `medium`, `high`, `xhigh`, and `max` enable DeepThink and steer reasoning depth.

---

## 📁 Project Structure

| File | Description |
| :--- | :--- |
| `server.py` | Local OpenAI API Server (`http://127.0.0.1:8765/v1`), WebSocket Hub (`ws://127.0.0.1:8765/ws`), SSE patch parser, and Tool Call adapter |
| `bridge.user.js` | Content Script / Userscript running on `https://chat.deepseek.com` that solves WASM PoW and streams SSE frames over WebSocket |
| `manifest.json` | Chrome Extension (Manifest V3) configuration for loading the project directly as an unpacked extension |

---

## 🚀 Quick Start

### Step 1: Install Python Dependencies
Only `fastapi`, `uvicorn`, and `websockets` are required:
```bash
pip install fastapi uvicorn websockets
```

Run the built-in self-test suite (verifies the SSE parser, session chaining, tool call extraction, and rate-limit matcher):
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

**Option B: Use Tampermonkey / Violentmonkey**
- Create a new userscript, paste the contents of `bridge.user.js`, save, and reload `https://chat.deepseek.com`.

---

### Step 3: Start the Local API Server

```bash
python3 server.py
```

Once the server is running and `https://chat.deepseek.com` is open in Chrome:
- The terminal will print: `[Local-AI] 🟢 Chrome tab connected to /ws`
- A green badge **`🟢 Local AI (API)`** will appear in the bottom-right corner of `chat.deepseek.com`.

Verify the server health status:
```bash
curl http://127.0.0.1:8765/health
```

---

## 🤖 Pigent (`pi` Coding Agent) Configuration

### 1. Register the Provider & Models in `~/.pi/agent/models.json`

Add the `deepseek-web-local` provider to `~/.pi/agent/models.json`:

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
          "input": [
            "text"
          ],
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          },
          "contextWindow": 131072,
          "maxTokens": 16384,
          "compat": {
            "requiresToolResultName": true
          }
        },
        {
          "id": "deepseek-chat",
          "name": "DeepSeek V3.2 Chat Fast (Web Local)",
          "reasoning": false,
          "input": [
            "text"
          ],
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          },
          "contextWindow": 131072,
          "maxTokens": 16384,
          "compat": {
            "requiresToolResultName": true
          }
        }
      ]
    }
  }
}
```

### 2. Set the Default Model in `~/.pi/agent/settings.json`

Update `~/.pi/agent/settings.json`:

```json
{
  "hideThinkingBlock": false,
  "defaultThinkingLevel": "medium",
  "httpIdleTimeoutMs": 0,
  "defaultProvider": "deepseek-web-local",
  "defaultModel": "deepseek-reasoner"
}
```

### 3. Usage in `pi`

Verify that `pi` recognizes the configured models:
```bash
pi --list-models deepseek
```

Launch `pi` in any project directory:
```bash
pi
```

- **Switch Models**: Type `/model` or press `Ctrl+P` to toggle between:
  - `deepseek-reasoner`: DeepThink enabled (streams live reasoning thoughts to `pi`).
  - `deepseek-chat`: DeepThink disabled for fast responses.
- **Adjust Thinking Level (`Shift+Tab`)**:
  - `off`: Disables DeepThink on `deepseek-reasoner` (`thinking_enabled: false`).
  - `minimal` / `low`: Enables DeepThink with concise reasoning (1–3 sentences or <150 words) before acting.
  - `medium`: Default native DeepThink depth.
  - `high` / `xhigh` / `max`: Deep architectural and edge-case analysis before executing tools.

---

## 📊 Context Limits & Supported Models

| Model ID | DeepThink | Web Search | Context Window | Max Output / Turn |
| :--- | :---: | :---: | :---: | :---: |
| `deepseek-reasoner` | ✅ Enabled | ❌ Disabled | `131,072` (128K) | `16,384` – `32,768` |
| `deepseek-chat` | ❌ Disabled | ❌ Disabled | `131,072` (128K) | `8,192` – `16,384` |
| `deepseek-reasoner-search` | ✅ Enabled | ✅ Enabled | `131,072` (128K) | `16,384` |
| `deepseek-chat-search` | ❌ Disabled | ✅ Enabled | `131,072` (128K) | `16,384` |
