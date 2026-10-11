# DeepSeek Web Local API (`deepseek-web-local-api`)

Biến phiên đăng nhập sẵn trên **DeepSeek Web (`https://chat.deepseek.com`)** thành một **Local OpenAI-Compatible API Server (`http://127.0.0.1:8765/v1`)** tối ưu chuyên biệt cho **Pigent (`pi` coding agent)** và các công cụ lập trình AI.

---

## ✨ Tính năng nổi bật

- **Tương thích chuẩn OpenAI `/v1/chat/completions`**: Hỗ trợ cả `stream: true` và `stream: false`, truyền trực tiếp luồng suy luận (`reasoning_content`) theo thời gian thực lên TUI của `pi`.
- **Giải PoW (`DeepSeekHashV1` SHA-3 WASM) ngay trong tab trình duyệt**: Không cần trích xuất cookie thủ công, tự động tận dụng phiên Cloudflare & thiết bị (`SMSdk`) của tab `chat.deepseek.com`.
- **Stateful Session Chaining (`parent_message_id`)**: Tự động nối chuỗi hội thoại trên server DeepSeek — chỉ lượt đầu tiên (`ROOT`) gửi toàn bộ System Prompt & Skills, các lượt gọi tool tiếp theo (`CHAIN`) chỉ gửi phần `[TOOL_RESULT]` mới phát sinh, giúp tận dụng trọn vẹn **128K context window** và tăng tốc phản hồi.
- **Bộ chuyển đổi Tool Calling đa định dạng cho `pi` agent**:
  - Hỗ trợ chuẩn JSON `{"name": "...", "arguments": {...}}`, Flat JSON `{"name": "bash", "command": "..."}`, Native DeepSeek DSML (`<｜DSML｜invoke>`, `<｜｜DSML｜｜ parameter>`), và Hybrid JSON + DSML.
  - Tự động xử lý trường hợp code/markdown chứa dấu ```` ``` ```` lồng trong lệnh `write` / `edit` và tự động ép kiểu tham số (`array`, `number`, `boolean`) theo JSON Schema của tool.
- **Chống Rate-Limit tự động (`Smart Pacing` + `Exponential Backoff`)**:
  - Tự động điều tiết nhịp gửi giữa các bước tool call siêu nhanh của `pi`.
  - Khi DeepSeek Web báo `"Tin nhắn quá thường xuyên. Vui lòng thử lại sau."`, server tự động chờ theo lịch `[5s, 10s, 16s, 24s, 35s]` và gửi lại mà không làm đứt chuỗi tác vụ của `pi`.
- **Điều khiển mức độ suy nghĩ (`Shift+Tab` trong `pi`)**:
  - Hỗ trợ bật/tắt nhanh DeepThink (`off` $\rightarrow$ tắt DeepThink, `minimal`/`low`/`medium`/`high`/`xhigh`/`max` $\rightarrow$ bật DeepThink kèm điều chỉnh độ sâu suy luận).

---

## 📁 Cấu trúc thư mục

| File | Vai trò |
| :--- | :--- |
| `server.py` | Local OpenAI API Server (`http://127.0.0.1:8765/v1`) + WebSocket Hub (`ws://127.0.0.1:8765/ws`) + SSE Parser + Tool Call Adapter |
| `bridge.user.js` | Content Script / Userscript chạy trên `https://chat.deepseek.com`, tự giải WASM PoW và đọc stream SSE |
| `manifest.json` | Manifest Chrome Extension (MV3) giúp nạp trực tiếp thư mục này vào Chrome ở chế độ Developer Mode (không cần cài Tampermonkey) |

---

## 🚀 Hướng dẫn cài đặt từ đầu

### Bước 1: Cài thư viện Python
Chỉ cần `fastapi`, `uvicorn` và `websockets`:
```bash
pip install fastapi uvicorn websockets
```

Kiểm tra nhanh toàn bộ parser & bộ chuyển đổi tool bằng self-test tích hợp sẵn:
```bash
python3 server.py --test
```

---

### Bước 2: Nạp Bridge vào trình duyệt Chrome

**Cách 1: Nạp trực tiếp làm Chrome Extension (Khuyên dùng)**
1. Mở Chrome, truy cập `chrome://extensions/`
2. Bật công tắc **Developer mode (Chế độ dành cho nhà phát triển)** ở góc phải trên.
3. Nhấn **Load unpacked (Tải tiện ích đã giải nén)** $\rightarrow$ chọn thư mục chứa repo này (`deepseek-web-local-api`).
4. Mở tab `https://chat.deepseek.com`, đăng nhập tài khoản DeepSeek và nhấn `F5` (Reload).

**Cách 2: Dùng Tampermonkey / Violentmonkey**
- Tạo script mới trong Tampermonkey, dán nội dung file `bridge.user.js` vào và lưu lại, sau đó tải lại trang `https://chat.deepseek.com`.

---

### Bước 3: Chạy Local API Server

```bash
python3 server.py
```

Khi server chạy và tab `https://chat.deepseek.com` đang mở:
- Terminal sẽ hiện: `[Local-AI] 🟢 Chrome tab connected to /ws`
- Góc phải dưới trang `chat.deepseek.com` sẽ hiện nút xanh: **`🟢 Local AI (API)`**

Kiểm tra trạng thái kết nối:
```bash
curl http://127.0.0.1:8765/health
```

---

## 🤖 Cấu hình cho Pigent (`pi` Coding Agent)

### 1. Khai báo Provider & Models trong `~/.pi/agent/models.json`

Mở (hoặc tạo mới) file `~/.pi/agent/models.json` và thêm provider `deepseek-web-local`:

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

### 2. Đặt làm Model mặc định trong `~/.pi/agent/settings.json`

Cập nhật `~/.pi/agent/settings.json`:

```json
{
  "hideThinkingBlock": false,
  "defaultThinkingLevel": "medium",
  "httpIdleTimeoutMs": 0,
  "defaultProvider": "deepseek-web-local",
  "defaultModel": "deepseek-reasoner"
}
```

### 3. Sử dụng trong `pi`

Kiểm tra danh sách model đã nhận:
```bash
pi --list-models deepseek
```

Khởi chạy `pi` trong thư mục dự án bất kỳ:
```bash
pi
```

- **Chuyển đổi giữa các model**: Gõ `/model` hoặc nhấn `Ctrl+P` để đổi giữa:
  - `deepseek-reasoner`: Bật DeepThink (hiển thị luồng suy nghĩ trực tiếp trên `pi`).
  - `deepseek-chat`: Tắt DeepThink, tốc độ phản hồi nhanh.
- **Điều chỉnh mức độ suy nghĩ (`Shift+Tab`)**:
  - `off`: Tắt hẳn DeepThink ngay trên `deepseek-reasoner`.
  - `minimal` / `low`: Suy nghĩ ngắn gọn (1–3 câu hoặc <150 từ) rồi gọi tool ngay.
  - `medium`: Độ sâu suy nghĩ tự nhiên mặc định của DeepSeek Web.
  - `high` / `xhigh` / `max`: Phân tích sâu kiến trúc và kiểm tra kỹ các edge cases trước khi hành động.

---

## 📊 Thông số giới hạn Context & Modelos hỗ trợ

| Model ID | DeepThink | Web Search | Context Window | Max Output / Lượt |
| :--- | :---: | :---: | :---: | :---: |
| `deepseek-reasoner` | ✅ Bật | ❌ Tắt | `131,072` (128K) | `16,384` – `32,768` |
| `deepseek-chat` | ❌ Tắt | ❌ Tắt | `131,072` (128K) | `8,192` – `16,384` |
| `deepseek-reasoner-search` | ✅ Bật | ✅ Bật | `131,072` (128K) | `16,384` |
| `deepseek-chat-search` | ❌ Tắt | ✅ Bật | `131,072` (128K) | `16,384` |
