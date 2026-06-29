"""
app.py  –  Jira → Sangraha Portal Automation Dashboard
========================================================
Features:
  • Credentials popup (st.dialog) for Sangraha login — no .env editing needed
  • Two data-source modes:
      MODE 1 – Upload a task sheet (CSV / Excel)
      MODE 2 – Enter a Jira project/CR URL → auto-fetch tasks → fill portal
  • Real-time Playwright bot log stream (subprocess-based, thread-safe)
  • Session reuse — bot saves cookies after first login, skips login next time
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv, set_key

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sangraha Automation",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

load_dotenv()

# ── Session-state defaults ─────────────────────────────────────────────────────
DEFAULTS = {
    "portal_username":   os.getenv("PORTAL_USERNAME", ""),
    "portal_password":   os.getenv("PORTAL_PASSWORD", ""),
    "jira_server":       os.getenv("JIRA_SERVER", ""),
    "jira_email":        os.getenv("JIRA_EMAIL", ""),
    "jira_api_token":    os.getenv("JIRA_API_TOKEN", ""),
    "creds_configured":  bool(os.getenv("PORTAL_USERNAME", "")),
    "jira_tasks":        [],
    "uploaded_tasks":    [],
    "bot_logs":          [],
    "bot_running":       False,
    "last_fetched":      None,
    "active_mode":       "jira",   # "jira" | "upload"
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

if st.session_state.get("show_saved_toast"):
    st.toast("Credentials saved successfully!", icon="✅")
    st.session_state.show_saved_toast = False

if st.session_state.get("show_reset_toast"):
    st.toast("Credentials reset successfully!", icon="🗑️")
    st.session_state.show_reset_toast = False

# ── Helpers ────────────────────────────────────────────────────────────────────

def session_file_exists() -> bool:
    path = os.getenv("SESSION_STATE_FILE", "session_state.json")
    return Path(path).is_file()


def save_creds_to_env(username: str, password: str,
                      jira_server: str, jira_email: str, jira_token: str):
    """Persist credentials into the .env file so they survive restarts."""
    env_path = Path(__file__).parent / ".env"
    env_path.touch(exist_ok=True)
    for key, val in [
        ("PORTAL_URL",      "https://sangraha.ltfinance.com"),
        ("PORTAL_USERNAME",  username),
        ("PORTAL_PASSWORD",  password),
        ("JIRA_SERVER",      jira_server),
        ("JIRA_EMAIL",       jira_email),
        ("JIRA_API_TOKEN",   jira_token),
        ("SESSION_STATE_FILE", "session_state.json"),
    ]:
        if val:
            set_key(str(env_path), key, val)


def _colour_log_line(line: str) -> str:
    lower = line.lower()
    if any(k in lower for k in ("✅", "🎉", "success", "[info]")):
        return f'<span class="ll-ok">{line}</span>'
    if any(k in lower for k in ("⚠️", "warn")):
        return f'<span class="ll-warn">{line}</span>'
    if any(k in lower for k in ("❌", "error", "failed", "fatal")):
        return f'<span class="ll-err">{line}</span>'
    return line


def build_log_html(lines: list) -> str:
    inner = "\n".join(_colour_log_line(l) for l in lines)
    return f'<div class="log-box">{inner}</div>'


def _parse_bot_question(line: str) -> dict | None:
    """
    Parse a [BOT_QUESTION] line emitted by automation_bot.py.
    Format:  [BOT_QUESTION] field=<name> | wanted=<value> | options=1:opt1|2:opt2|...
    Returns a dict with keys: field, wanted, options (list of str)
    """
    if not line.startswith("[BOT_QUESTION]"):
        return None
    payload = line[len("[BOT_QUESTION]"):].strip()
    parts = {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip()
             for p in payload.split("|") if "=" in p}
    raw_opts = parts.get("options", "")
    # options format: "1:optA|2:optB|..." (but | was already split above, so re-join)
    # Re-parse directly from payload since | is used as delimiter for opts too
    # Re-do: find everything after "options="
    opts_match = payload.split("options=", 1)
    options = []
    if len(opts_match) > 1:
        for item in opts_match[1].split("|"):
            if ":" in item:
                options.append(item.split(":", 1)[1].strip())
            elif item.strip():
                options.append(item.strip())
    return {
        "field":   parts.get("field", "Field"),
        "wanted":  parts.get("wanted", ""),
        "options": options,
    }


def run_bot_subprocess(tasks: list, week: str, category: str,
                       force_relogin: bool, log_ph) -> bool:
    """
    Launch automation_bot.py as an isolated subprocess and stream its stdout.

    Interactive protocol:
      • Bot prints [BOT_QUESTION] field=... | wanted=... | options=1:X|2:Y|...
      • This function shows a Streamlit UI prompt for the user to pick an option.
      • User's choice (number) is written to the bot's stdin so it can continue.
    """
    env_copy = os.environ.copy()
    env_copy["PORTAL_USERNAME"] = st.session_state.portal_username
    env_copy["PORTAL_PASSWORD"] = st.session_state.portal_password
    env_copy["PORTAL_URL"]      = "https://sangraha.ltfinance.com"
    env_copy["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(__file__).parent / ".ms-playwright")



    cmd = [
        sys.executable,
        str(Path(__file__).parent / "automation_bot.py"),
        "--tasks",    json.dumps(tasks, default=str),
        "--week",     week,
        "--category", category,
    ]
    if force_relogin:
        cmd.append("--force-relogin")

    st.session_state.bot_logs.clear()
    st.session_state.bot_running = True
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,     # ← open stdin for interactive replies
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
            env=env_copy,
        )

        question_ph = st.empty()   # placeholder for interactive question UI

        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue

            # ── Interactive question from bot ──────────────────────────────
            question = _parse_bot_question(line)
            if question:
                field   = question["field"]
                wanted  = question["wanted"]
                options = question["options"]

                st.session_state.bot_logs.append(
                    f"⏸️  Bot needs your help: '{field}' (wanted: '{wanted}') "
                    f"— could not auto-match."
                )
                log_ph.markdown(build_log_html(st.session_state.bot_logs),
                                unsafe_allow_html=True)

                with question_ph.container():
                    st.warning(
                        f"**🤔 Bot needs your input!**\n\n"
                        f"**Field:** `{field}`  \n"
                        f"**Excel value:** `{wanted}`  \n"
                        f"No exact match was found in the portal dropdown.",
                        icon="⏸️",
                    )
                    skip_opt = "⏭️  Skip this field"
                    all_opts = [skip_opt] + options
                    choice = st.selectbox(
                        f"Choose the correct option for **{field}**:",
                        all_opts,
                        key=f"bot_q_{field}_{wanted}_{len(st.session_state.bot_logs)}",
                    )
                    if st.button("✅ Confirm & Continue", key=f"bot_q_btn_{len(st.session_state.bot_logs)}"):
                        question_ph.empty()
                        if choice == skip_opt:
                            answer = "skip"
                        else:
                            # Send the 1-based index
                            idx = options.index(choice) + 1
                            answer = str(idx)
                        proc.stdin.write(answer + "\n")
                        proc.stdin.flush()
                        st.session_state.bot_logs.append(
                            f"✅ You chose: '{choice}' → sent to bot."
                        )
                        log_ph.markdown(build_log_html(st.session_state.bot_logs),
                                        unsafe_allow_html=True)
                continue  # don't add [BOT_QUESTION] raw line to the log display

            # ── Normal log line ────────────────────────────────────────────
            st.session_state.bot_logs.append(line)
            log_ph.markdown(build_log_html(st.session_state.bot_logs),
                            unsafe_allow_html=True)

        proc.wait()
        question_ph.empty()
        return proc.returncode == 0
    except Exception as exc:
        st.session_state.bot_logs.append(f"❌ FATAL: {exc}")
        return False
    finally:
        st.session_state.bot_running = False
        if 'proc' in locals() and proc.poll() is None:
            # User interrupted (e.g. clicked Stop) or Streamlit rerun
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif;}

/* Header */
.main-header{
  background:linear-gradient(135deg,#0d1b2a 0%,#1b2838 50%,#16213e 100%);
  padding:1.8rem 2.2rem;border-radius:16px;margin-bottom:1.4rem;
  border:1px solid rgba(99,179,237,.18);box-shadow:0 8px 32px rgba(0,0,0,.35);}
.main-header h1{color:#e2e8f0;font-weight:700;font-size:1.9rem;margin:0;letter-spacing:-.5px;}
.main-header p{color:#94a3b8;margin:.35rem 0 0;font-size:.9rem;}

/* Metric cards */
.mc{background:linear-gradient(135deg,#1e293b,#0f172a);
  border:1px solid rgba(148,163,184,.15);border-radius:12px;
  padding:1rem 1.4rem;text-align:center;margin-bottom:.6rem;}
.mc .lbl{color:#64748b;font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;}
.mc .val{color:#e2e8f0;font-size:1.8rem;font-weight:700;line-height:1.2;}

/* Mode cards */
.mode-card{
  border:2px solid rgba(148,163,184,.15);border-radius:14px;
  padding:1.4rem 1.6rem;cursor:pointer;transition:all .2s;
  background:#0f172a;margin-bottom:.5rem;}
.mode-card.active{border-color:#3b82f6;background:#1e3a5f;}
.mode-card h3{margin:0 0 .4rem;font-size:1.05rem;color:#e2e8f0;}
.mode-card p{margin:0;color:#64748b;font-size:.83rem;}

/* Log box */
.log-box{background:#0d1117;border:1px solid #30363d;border-radius:8px;
  padding:1rem 1.2rem;font-family:'Courier New',monospace;font-size:.8rem;
  color:#c9d1d9;max-height:400px;overflow-y:auto;white-space:pre-wrap;
  word-break:break-all;line-height:1.7;}
.ll-ok{color:#56d364;} .ll-warn{color:#e3b341;} .ll-err{color:#f85149;}

/* Buttons */
div.stButton>button[kind="primary"]{
  background:linear-gradient(135deg,#3b82f6,#1d4ed8);border:none;
  border-radius:8px;color:#fff;font-weight:600;letter-spacing:.03em;
  transition:all .2s;box-shadow:0 4px 15px rgba(59,130,246,.35);}
div.stButton>button[kind="primary"]:hover{
  box-shadow:0 4px 24px rgba(59,130,246,.55);transform:translateY(-1px);}

/* Sidebar */
[data-testid="stSidebar"]{background:#0f172a;border-right:1px solid rgba(148,163,184,.1);}
[data-testid="stSidebar"] label{color:#94a3b8!important;font-size:.8rem;
  font-weight:600;text-transform:uppercase;letter-spacing:.06em;}

/* Session dot */
.dot-ok{color:#34d399;font-weight:700;} .dot-err{color:#f87171;font-weight:700;}

/* Dialog styling helper */
.cred-hint{background:#1e293b;border-radius:8px;padding:.8rem 1rem;
  margin-top:.8rem;font-size:.82rem;color:#94a3b8;}
hr{border-color:rgba(148,163,184,.15);}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS DIALOG
# ══════════════════════════════════════════════════════════════════════════════
@st.dialog("🔐 Portal & Jira Credentials", width="large")
def credentials_dialog():
    st.markdown("Enter your credentials below. They are stored only in your local `.env` file — never sent anywhere else.")

    st.markdown("#### 🌐 Sangraha Portal Login")
    col1, col2 = st.columns(2)
    with col1:
        username = st.text_input("Sangraha User ID",
                                 value=st.session_state.portal_username,
                                 placeholder="e.g. john.doe@ltfinance.com",
                                 key="dlg_username")
    with col2:
        password = st.text_input("Sangraha Password",
                                 value=st.session_state.portal_password,
                                 type="password",
                                 placeholder="••••••••",
                                 key="dlg_password")

    st.markdown("---")
    st.markdown("#### 🔷 Jira Configuration *(optional — only for Jira mode)*")
    jira_server = st.text_input("Jira Server URL",
                                value=st.session_state.jira_server,
                                placeholder="https://yourcompany.atlassian.net",
                                key="dlg_jira_server")
    col3, col4 = st.columns(2)
    with col3:
        jira_email = st.text_input("Jira Email",
                                   value=st.session_state.jira_email,
                                   placeholder="you@company.com",
                                   key="dlg_jira_email")
    with col4:
        jira_token = st.text_input("Jira API Token",
                                   value=st.session_state.jira_api_token,
                                   type="password",
                                   placeholder="Generate at id.atlassian.com",
                                   key="dlg_jira_token")

    st.markdown(
        '<div class="cred-hint">💡 Get a Jira API token at '
        '<a href="https://id.atlassian.com/manage-profile/security/api-tokens" '
        'target="_blank">id.atlassian.com → Security → API tokens</a></div>',
        unsafe_allow_html=True,
    )

    st.markdown("")
    save_col, cancel_col = st.columns([2, 1])
    with save_col:
        if st.button("💾  Save & Continue", type="primary", use_container_width=True):
            if not username or not password:
                st.error("Sangraha User ID and Password are required.")
            else:
                # Persist into session state
                st.session_state.portal_username  = username
                st.session_state.portal_password  = password
                st.session_state.jira_server      = jira_server
                st.session_state.jira_email       = jira_email
                st.session_state.jira_api_token   = jira_token
                st.session_state.creds_configured = True
                # Write to .env so they survive restarts
                save_creds_to_env(username, password, jira_server, jira_email, jira_token)
                st.session_state.show_saved_toast = True
                st.rerun()
    with cancel_col:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# Auto-open dialog on first load if credentials not set
if not st.session_state.creds_configured:
    credentials_dialog()


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## ⚙️ Configuration")
    st.markdown("---")

    # Credentials status + edit button
    if st.session_state.creds_configured:
        st.markdown(
            f'<p class="dot-ok">● Credentials set</p>'
            f'<small style="color:#475569">User: <strong>{st.session_state.portal_username}</strong></small>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<p class="dot-err">● Credentials not set</p>', unsafe_allow_html=True)

    col_edit, col_reset = st.columns([1, 1])
    with col_edit:
        if st.button("✏️ Edit", use_container_width=True):
            credentials_dialog()
    with col_reset:
        if st.button("🗑️ Reset", use_container_width=True):
            save_creds_to_env("", "", "", "", "")
            for k in ["portal_username", "portal_password", "jira_server", "jira_email", "jira_api_token"]:
                st.session_state[k] = ""
            st.session_state.creds_configured = False
            st.session_state.show_reset_toast = True
            st.rerun()

    st.markdown("---")

    # Portal options
    st.markdown("### 🌐 Portal Options")
    import datetime
    today = datetime.date.today()
    week_labels = []
    for delta in range(-4, 22):
        monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=delta)
        friday = monday + datetime.timedelta(days=4)
        wn = monday.isocalendar()[1]
        week_labels.append(f"Week {wn} ({monday.strftime('%b %d')} – {friday.strftime('%b %d, %Y')})")
    selected_week = st.selectbox("📅 Select Week", week_labels, index=4)

    task_categories = [
        "Development", "Testing / QA", "Code Review", "Documentation",
        "Meetings / Planning", "DevOps / Deployment", "Bug Fixes",
        "Research / Spike", "Support",
    ]
    selected_category = st.selectbox("🗂️ Task Category", task_categories)

    st.markdown("---")

    # Session status
    st.markdown("### 🤖 Bot Session")
    if session_file_exists():
        st.markdown('<p class="dot-ok">● Saved session found</p>', unsafe_allow_html=True)
        st.caption("Bot will reuse cookies — login will be skipped.")
        if st.button("🗑️  Clear session", use_container_width=True):
            path = os.getenv("SESSION_STATE_FILE", "session_state.json")
            try: Path(path).unlink()
            except: pass
            st.rerun()
    else:
        st.markdown('<p class="dot-err">● No saved session</p>', unsafe_allow_html=True)
        st.caption("Bot will login and save cookies on first run.")

    force_relogin = st.checkbox("Force fresh login", value=False)

    st.markdown("---")

    # ── Stop button ──────────────────────────────────────────────────────────
    if st.session_state.get("bot_running", False):
        if st.button("🛑 Stop Automation", type="primary", use_container_width=True):
            st.session_state.bot_running = False
            # Streamlit will automatically trigger a rerun here, interrupting the
            # subprocess thread. The `finally` block in run_bot_subprocess will
            # execute and kill the Playwright bot.
            pass

    st.markdown("---")
    st.markdown(
        "<small style='color:#475569'>Sangraha Automation Tool<br>"
        "Streamlit + Playwright · LTFinance</small>",
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AREA
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<div class="main-header">
  <h1>⚡ Sangraha Portal Automation</h1>
  <p>Choose a data source → review tasks → let the bot fill in the portal for you.</p>
</div>
""", unsafe_allow_html=True)

# ── Metric row ─────────────────────────────────────────────────────────────────
tasks_in_state = (
    st.session_state.jira_tasks
    if st.session_state.active_mode == "jira"
    else st.session_state.uploaded_tasks
)
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown(f'<div class="mc"><div class="lbl">Total Tasks</div>'
                f'<div class="val">{len(tasks_in_state)}</div></div>', unsafe_allow_html=True)
with m2:
    in_prog = sum(1 for t in tasks_in_state if "progress" in str(t.get("status","")).lower())
    st.markdown(f'<div class="mc"><div class="lbl">In Progress</div>'
                f'<div class="val">{in_prog}</div></div>', unsafe_allow_html=True)
with m3:
    sp = sum(float(t.get("story_points") or 0) for t in tasks_in_state)
    st.markdown(f'<div class="mc"><div class="lbl">Story Points</div>'
                f'<div class="val">{sp:.0f}</div></div>', unsafe_allow_html=True)
with m4:
    sess = "✅ Active" if session_file_exists() else "❌ None"
    st.markdown(f'<div class="mc"><div class="lbl">Bot Session</div>'
                f'<div class="val" style="font-size:1rem;padding-top:.5rem">{sess}</div></div>',
                unsafe_allow_html=True)

st.markdown("")

# ══════════════════════════════════════════════════════════════════════════════
# MODE SELECTION TABS
# ══════════════════════════════════════════════════════════════════════════════
tab_upload, tab_jira = st.tabs([
    "📄  Mode 1 — Upload Task Sheet",
    "🔷  Mode 2 — Fetch from Jira / CR URL",
])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — JIRA / CR URL
# ─────────────────────────────────────────────────────────────────────────────
with tab_jira:
    st.session_state.active_mode = "jira"

    st.markdown("### 🔷 Fetch Tasks from Jira")
    st.markdown(
        "Enter a **Jira project URL**, a **Change Request (CR) URL**, or a **JQL query**. "
        "The bot will fetch all relevant tasks and display them below."
    )

    input_type = st.radio(
        "Input type",
        ["JQL Query", "Jira Issue / CR URL"],
        horizontal=True,
        label_visibility="collapsed",
    )

    col_inp, col_btn = st.columns([5, 1])
    with col_inp:
        if input_type == "JQL Query":
            jql_input = st.text_area(
                "JQL Query",
                value='assignee = currentUser() AND status = "In Progress"',
                height=80,
                label_visibility="collapsed",
                placeholder='e.g. project = "MYPROJ" AND status = "In Progress"',
            )
            jira_url_input = ""
        else:
            jira_url_input = st.text_input(
                "Jira URL",
                label_visibility="collapsed",
                placeholder="https://yourcompany.atlassian.net/browse/PROJ-123  or  CR link",
            )
            jql_input = ""

    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        fetch_clicked = st.button(
            "🔄 Fetch",
            type="primary",
            use_container_width=True,
            disabled=st.session_state.bot_running,
        )

    max_results = st.slider("Max results", 5, 100, 20, 5, key="jira_max")

    # ── Fetch logic ────────────────────────────────────────────────────────────
    if fetch_clicked:
        if not st.session_state.jira_server:
            st.warning("⚠️  Please set your Jira credentials first — click **Edit Credentials** in the sidebar.", icon="🔑")
        else:
            # Build JQL from URL if needed
            jql_to_run = jql_input
            if input_type == "Jira Issue / CR URL" and jira_url_input.strip():
                url = jira_url_input.strip()
                # Extract issue key from URL  e.g. /browse/PROJ-123
                import re
                m = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", url)
                if m:
                    key = m.group(1)
                    # If it looks like a parent/epic, pull all child issues too
                    jql_to_run = f'issue = {key} OR parent = {key} OR "Epic Link" = {key}'
                else:
                    st.error("Could not extract an issue key from that URL. Please check the format or use a JQL query instead.")
                    jql_to_run = None

            if jql_to_run:
                with st.spinner("🔍 Connecting to Jira…"):
                    try:
                        # Temporarily set env vars from session state for jira_client
                        os.environ["JIRA_SERVER"]    = st.session_state.jira_server
                        os.environ["JIRA_EMAIL"]     = st.session_state.jira_email
                        os.environ["JIRA_API_TOKEN"] = st.session_state.jira_api_token

                        from jira_client import JiraClient, JiraClientError
                        client = JiraClient()
                        issues = client.fetch_issues(jql=jql_to_run, max_results=max_results)
                        st.session_state.jira_tasks  = issues
                        st.session_state.last_fetched = time.strftime("%Y-%m-%d %H:%M:%S")
                        if not issues:
                            st.warning("Query succeeded but returned 0 issues. Check your JQL or filters.")
                    except Exception as exc:
                        st.error(f"❌ Jira Error: {exc}")
                        st.session_state.jira_tasks = []
                st.rerun()

    # ── Task table ──────────────────────────────────────────────────────────────
    if st.session_state.jira_tasks:
        st.markdown(f"**{len(st.session_state.jira_tasks)} task(s) fetched** "
                    f"— last updated `{st.session_state.last_fetched}`")
        try:
            import pandas as pd
            df = pd.DataFrame(st.session_state.jira_tasks)
            cols = [c for c in ["key","summary","status","priority","story_points",
                                 "assignee","updated","labels"] if c in df.columns]
            df_show = df[cols].copy()
            df_show.columns = [c.replace("_"," ").title() for c in cols]
            st.dataframe(df_show, use_container_width=True,
                         height=min(60+36*len(df_show), 450), hide_index=True)
        except ImportError:
            # Fallback if pandas/numpy still broken — show as JSON
            st.json(st.session_state.jira_tasks[:5])

        st.markdown("---")
        ja_col1, ja_col2 = st.columns([2, 3])
        with ja_col1:
            start_jira = st.button(
                "🚀  Start Automation (Jira Tasks)",
                type="primary", use_container_width=True,
                disabled=st.session_state.bot_running or not st.session_state.creds_configured,
            )
        with ja_col2:
            if not st.session_state.creds_configured:
                st.warning("Set credentials first (sidebar → Edit Credentials)")
    else:
        st.info("No tasks loaded yet. Enter a query above and click **🔄 Fetch**.", icon="ℹ️")
        start_jira = False

    # ── Automation run (Jira mode) ──────────────────────────────────────────────
    if start_jira and st.session_state.jira_tasks:
        st.markdown("### 🤖 Automation Progress")
        st.info(
            f"**Bot running** · {len(st.session_state.jira_tasks)} tasks · "
            f"Week: `{selected_week}` · Category: `{selected_category}`",
            icon="🚀",
        )
        log_ph = st.empty()
        log_ph.markdown(build_log_html(["Initialising Playwright bot…"]), unsafe_allow_html=True)

        with st.status("Running automation bot…", expanded=True, state="running") as sw:
            ok = run_bot_subprocess(
                tasks=st.session_state.jira_tasks,
                week=selected_week,
                category=selected_category,
                force_relogin=force_relogin,
                log_ph=log_ph,
            )
            sw.update(
                label="✅ Automation completed!" if ok else "❌ Automation encountered an error.",
                state="complete" if ok else "error",
            )
        if ok:
            st.success("🎉 All tasks submitted to the portal! Please verify entries.")
            st.balloons()
        else:
            st.error("Bot exited with an error. Review the log above.")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — UPLOAD TASK SHEET
# ─────────────────────────────────────────────────────────────────────────────
with tab_upload:
    st.session_state.active_mode = "upload"

    st.markdown("### 📄 Upload a Task Sheet")
    st.markdown(
        "Upload a **CSV or Excel** file containing your tasks. "
        "The bot will read each row and fill it into the Sangraha portal."
    )

    with st.expander("📐 Required columns (click to expand)", expanded=False):
        st.markdown("""
| Column name | Required | Description |
|---|---|---|
| `Project Name - L1` | ✅ | e.g., MEL |
| `Sub Project - L2` | ✅ | e.g., BAU enhancement-CR |
| `Task Type` | ✅ | e.g., development and configuration |
| `Task Description` | ✅ | Max 100 characters |
| `Sunday` to `Saturday` | ✅ | Number of hours (e.g., 0, 4, 8) |
| `JIRA ID / SR` | ✅ | e.g., PROJ-001 |

You can download the template below ↓
        """)
        # Generate download template
        template_data = (
            "Project Name - L1,Sub Project - L2,Task Type,Task Description,Sunday,Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,JIRA ID / SR\n"
            "MEL,BAU enhancement-CR,development and configuration,Fix login UI bug,0,4,4,0,0,0,0,PROJ-101\n"
        )
        st.download_button(
            "⬇️  Download CSV Template",
            data=template_data,
            file_name="task_sheet_template.csv",
            mime="text/csv",
        )

    uploaded_file = st.file_uploader(
        "Drop your CSV or Excel file here",
        type=["csv", "xlsx", "xls"],
        label_visibility="collapsed",
    )

    if uploaded_file:
        try:
            import pandas as pd
            if uploaded_file.name.endswith(".csv"):
                df_up = pd.read_csv(uploaded_file)
            else:
                df_up = pd.read_excel(uploaded_file)

            # Keep exact column names
            df_up.columns = [str(c).strip() for c in df_up.columns]

            # ── Master-format detection ──────────────────────────────────────
            # The master Employee Timesheet Excel uses different column names.
            # If detected, remap cols F–P to the names the existing bot expects,
            # then drop irrelevant columns so the rest of the flow is untouched.
            MASTER_COL_MAP = {
                "ProjectName":    "Project Name - L1",
                "SubProjectName": "Sub Project - L2",
                "TaskTypeName":   "Task Type",
                "TaskDescription":"Task Description",
            }
            if "ProjectName" in df_up.columns:
                st.info("📊 Master timesheet format detected — remapping columns automatically.")
                df_up = df_up.rename(columns=MASTER_COL_MAP)

                # Forward-fill employee name so we can filter by logged-in user
                if "EmployeeName" in df_up.columns:
                    df_up["EmployeeName"] = df_up["EmployeeName"].ffill()
                    logged_in_user = st.session_state.get("portal_username", "")
                    # Try to match the employee name loosely against the portal username
                    first_name = logged_in_user.split(".")[0].strip().lower() if "." in logged_in_user else logged_in_user.strip().lower()
                    mask = df_up["EmployeeName"].str.strip().str.lower().str.contains(first_name, na=False)
                    if mask.any():
                        df_up = df_up[mask].copy()
                        st.success(f"👤 Filtered to **{df_up['EmployeeName'].iloc[0]}** ({len(df_up)} rows).")
                    else:
                        st.warning("⚠️ Could not match any employee name to your portal username — showing all rows.")

                # Drop rows that have no meaningful task data (empty project or task type)
                df_up = df_up[
                    df_up["Project Name - L1"].notna() &
                    (df_up["Project Name - L1"].astype(str).str.strip() != "") &
                    (df_up["Project Name - L1"].astype(str).str.strip().str.lower() != "nan")
                ].copy()

                # Add JIRA ID / SR as empty if not present (master file has no JIRA column)
                if "JIRA ID / SR" not in df_up.columns:
                    df_up["JIRA ID / SR"] = ""

                # Reset index after filtering
                df_up = df_up.reset_index(drop=True)
            # ── End master-format block ──────────────────────────────────────

            required_cols = {
                "Project Name - L1", "Sub Project - L2", "Task Type",
                "Task Description", "Sunday", "Monday", "Tuesday",
                "Wednesday", "Thursday", "Friday", "Saturday", "JIRA ID / SR"
            }
            missing = required_cols - set(df_up.columns)
            if missing:
                st.error(f"❌ Missing required columns: {', '.join(missing)}")
            else:
                tasks_from_file = df_up.to_dict("records")
                st.session_state.uploaded_tasks = tasks_from_file

                # Trigger automation automatically if it's a new file
                if st.session_state.get("last_uploaded_file") != uploaded_file.name:
                    st.session_state.last_uploaded_file = uploaded_file.name
                    st.session_state.auto_start_upload = True

                st.success(f"✅ Loaded **{len(tasks_from_file)}** task(s) from `{uploaded_file.name}`")
                st.dataframe(df_up, use_container_width=True,
                             height=min(60+36*len(df_up), 400), hide_index=True)

        except ImportError as e:
            st.error(
                f"❌ Import error detected: {e}\n"
                "Please ensure `openpyxl`, `pandas`, and `numpy` are installed correctly."
            )
        except Exception as exc:
            st.error(f"❌ Could not read file: {exc}")

    # Automation button for upload mode
    if st.session_state.uploaded_tasks:
        st.markdown("---")
        up_col1, up_col2 = st.columns([2, 3])
        with up_col1:
            start_upload = st.button(
                "🚀  Start Automation (Uploaded Tasks)",
                type="primary", use_container_width=True,
                disabled=st.session_state.bot_running or not st.session_state.creds_configured,
            )
        with up_col2:
            if not st.session_state.creds_configured:
                st.warning("Set credentials first (sidebar → Edit Credentials)")
            else:
                st.caption(
                    f"Week: `{selected_week}` · Category: `{selected_category}` · "
                    f"Session: `{'reuse' if session_file_exists() else 'fresh login'}`"
                )

        if st.session_state.get("auto_start_upload"):
            start_upload = True
            st.session_state.auto_start_upload = False

        if start_upload:
            st.markdown("### 🤖 Automation Progress")
            st.info(
                f"**Bot running** · {len(st.session_state.uploaded_tasks)} tasks from sheet · "
                f"Week: `{selected_week}` · Category: `{selected_category}`",
                icon="🚀",
            )
            log_ph2 = st.empty()
            log_ph2.markdown(build_log_html(["Initialising Playwright bot…"]), unsafe_allow_html=True)

            with st.status("Running automation bot…", expanded=True, state="running") as sw2:
                ok2 = run_bot_subprocess(
                    tasks=st.session_state.uploaded_tasks,
                    week=selected_week,
                    category=selected_category,
                    force_relogin=force_relogin,
                    log_ph=log_ph2,
                )
                sw2.update(
                    label="✅ Automation completed!" if ok2 else "❌ Automation encountered an error.",
                    state="complete" if ok2 else "error",
                )
            if ok2:
                st.success("🎉 All tasks submitted to the portal!")
                st.balloons()
            else:
                st.error("Bot exited with an error. Review the log above.")
    else:
        if not uploaded_file:
            st.markdown(
                """
                <div style="background:#0f172a;border:2px dashed rgba(148,163,184,.2);
                border-radius:12px;padding:2.5rem;text-align:center;margin-top:1rem;">
                  <div style="font-size:2.5rem;margin-bottom:.5rem">📁</div>
                  <div style="color:#475569;font-size:.95rem">
                    Upload a CSV or Excel file to get started
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        start_upload = False


# ── Persistent log panel ───────────────────────────────────────────────────────
if st.session_state.bot_logs:
    with st.expander("📜 Last bot run log", expanded=False):
        st.markdown(build_log_html(st.session_state.bot_logs), unsafe_allow_html=True)


# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    '<div style="text-align:center;color:#334155;font-size:.76rem;padding:.4rem 0 .8rem">'
    '⚡ <strong>Sangraha Automation Tool</strong> &nbsp;|&nbsp; '
    'Streamlit + Playwright · Session Reuse · Network Interception'
    '</div>',
    unsafe_allow_html=True,
)
