# Jira → Portal Automation Tool

A production-ready local automation tool that fetches tasks from **Jira Cloud**
via the REST API and automatically fills them into a third-party portal using
an aggressively-optimised **Playwright** bot.

---

## ✨ Features

| Feature | Detail |
|---|---|
| 🎨 Streamlit Dashboard | Attractive dark UI with metrics, live logs, and status badges |
| ⚡ Playwright Bot | Headless, network-intercepted, session-reusing automation |
| 🔑 Session Reuse | Cookies saved after first login; subsequent runs skip login entirely |
| 🚦 Network Interception | Blocks images, fonts, CSS, and analytics endpoints for max speed |
| 🔒 Secure Config | All secrets in `.env` — never committed to source control |
| 🧵 Thread-safe | Bot runs as a **subprocess** to avoid Streamlit ↔ asyncio conflicts |
| 📡 Live Logs | Real-time bot output streamed into the UI line-by-line |

---

## 📁 Project Structure

```
jira-automation-tool/
│
├── app.py                  # Streamlit dashboard (main entry point)
├── jira_client.py          # Jira Cloud REST API client
├── automation_bot.py       # Playwright bot (headless, optimised)
│
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .env                    # Your actual secrets (DO NOT COMMIT)
├── .gitignore
├── session_state.json      # Auto-created after first login (DO NOT COMMIT)
│
└── README.md
```

---

## 🚀 Quick Start

### 1. Prerequisites

- Python 3.11+
- pip

### 2. Install dependencies

```powershell
cd jira-automation-tool
pip install -r requirements.txt
playwright install chromium   # downloads the Chromium browser binary
```

### 3. Configure your environment

```powershell
copy .env.example .env
notepad .env   # fill in your real values
```

**Required `.env` values:**

```dotenv
JIRA_SERVER=https://yourcompany.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_api_token_here   # from https://id.atlassian.com/...
PORTAL_URL=https://your-portal.example.com
PORTAL_USERNAME=your_username
PORTAL_PASSWORD=your_password
```

Generate a Jira API token at:
👉 https://id.atlassian.com/manage-profile/security/api-tokens

### 4. Customise the bot selectors

Open `automation_bot.py` and update the two functions marked with
`⚠️ CUSTOMISE THIS FUNCTION`:

- **`_do_login()`** – fill in your portal's username/password field selectors
- **`_fill_timesheet()`** – map Jira task fields to your portal's form elements

> **Tip:** Run `playwright codegen https://your-portal.example.com` to
> auto-generate selector code by clicking through your portal.

### 5. Run the app

```powershell
streamlit run app.py
```

Open your browser at **http://localhost:8501**

---

## 🔄 Usage Workflow

```
Configure JQL in Sidebar
        │
        ▼
[Fetch Jira Tasks] ──→ Jira REST API ──→ Tasks shown in table
        │
        ▼
Select Week & Category in Sidebar
        │
        ▼
[Start Automation] ──→ Playwright Bot (subprocess)
        │
        ├── First run:  full login → save session_state.json
        └── Next runs:  load cookies → skip login → go straight to form
        │
        ▼
Live logs stream into the UI
        │
        ▼
✅ "Automation completed successfully!"
```

---

## ⚡ Playwright Optimisation Details

### 1. Headless Mode
```python
browser = playwright.chromium.launch(headless=True)
```
No GUI window → significantly lower resource usage.

### 2. Network Interception (Resource Blocking)
Every outgoing request is evaluated against:
- **Resource type blocklist**: `image`, `font`, `media`, `websocket`
- **URL pattern blocklist**: analytics, trackers, mixpanel, hotjar…
- **Extension blocklist**: `.png`, `.jpg`, `.css`, `.woff2`, etc.

Result: only HTML, JS, and XHR/Fetch requests go through — page loads are
often **3–5× faster**.

### 3. Session Reuse (Cookie Injection)
```python
# Save after login
context.storage_state() → session_state.json

# Load on next run
browser.new_context(storage_state="session_state.json")
```
Skipping login typically saves **5–15 seconds** per run.

### 4. Additional Browser Flags
- `--disable-gpu` – no GPU rendering needed in headless
- `--blink-settings=imagesEnabled=false` – disables images at engine level
- `--mute-audio` – silences any auto-playing media

---

## 🔧 Troubleshooting

| Problem | Solution |
|---|---|
| `JIRA_SERVER not set` | Check your `.env` file exists and is in the project root |
| `HTTP 401 from Jira` | Regenerate your API token; verify the email matches |
| Bot can't find element | Use `playwright codegen` to find the correct selector |
| `Session expired` warning | Check "Force fresh login" in the sidebar |
| `Port already in use` | Run `streamlit run app.py --server.port 8502` |

---

## 🔒 Security Notes

- Never commit `.env` or `session_state.json` to version control.
- Both files are already listed in `.gitignore`.
- API tokens can be revoked at any time from the Atlassian account settings.
