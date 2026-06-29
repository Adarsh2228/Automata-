"""
automation_bot.py
=================
Target portal: https://sangraha.ltfinance.com/

A highly-optimised Playwright automation script that:

  1. NETWORK INTERCEPTION  – Blocks all non-essential resources (images, fonts,
     stylesheets, trackers) so the browser only downloads HTML/JS/XHR.

  2. SESSION REUSE         – Saves cookies + localStorage to `session_state.json`
     after first login. All subsequent runs skip the login page entirely.

  3. HEADLESS MODE         – Runs in headless=True for maximum speed.

  4. GRACEFUL FALLBACK     – If the saved session is expired or invalid, the
     bot automatically falls back to a full credential-based login and then
     saves a fresh session.

This script is designed to be called as an isolated subprocess from app.py,
which avoids asyncio / Streamlit event-loop conflicts entirely.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO FIND YOUR PORTAL'S HTML SELECTORS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Open your portal in Chrome/Edge.
2. Right-click the element (input field, button, dropdown) → "Inspect".
3. Look for unique attributes in this priority order:
     a) data-testid="..."    → page.get_by_test_id("...")       ← BEST
     b) id="..."             → page.locator("#your-id")
     c) name="..."           → page.locator("[name='your-name']")
     d) aria-label="..."     → page.get_by_label("...")
     e) class="..."          → page.locator(".your-class")      ← LAST RESORT
4. You can also run Playwright's codegen to auto-record selectors:
       playwright codegen https://your-portal.example.com
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import json
import logging
import os
import re
import sys
import time

# Force utf-8 stdout to avoid Windows charmap errors with emojis
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, Route, Request

# ─────────────────────────────────────────────────────────────────────────────
# Logging – every message goes to stdout so the parent Streamlit process can
# capture and display it in real-time through subprocess pipes.
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,   # <── IMPORTANT: stdout so Streamlit can read it
    force=True,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Load .env
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

PORTAL_URL          = os.getenv("PORTAL_URL", "").rstrip("/")
PORTAL_USERNAME     = os.getenv("PORTAL_USERNAME", "")
PORTAL_PASSWORD     = os.getenv("PORTAL_PASSWORD", "")
SESSION_STATE_FILE  = os.getenv("SESSION_STATE_FILE", "session_state.json")


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMISATION 1 – Network resource blocklist
# ─────────────────────────────────────────────────────────────────────────────
# These patterns are matched against every outgoing request URL.
# Blocking them prevents the browser from wasting time downloading
# megabytes of assets that the bot never needs to see.

BLOCKED_RESOURCE_TYPES = {
    "image",        # <img> tags and CSS background images
    "font",         # Web fonts (woff, woff2, ttf…)
    "media",        # Video and audio
    "websocket",    # Real-time push connections the bot won't use
}

# URL pattern matching – block analytics / tracker endpoints
BLOCKED_URL_PATTERNS = re.compile(
    r"(analytics|tracker|mixpanel|hotjar|fullstory|segment|"
    r"doubleclick|googletagmanager|facebook\.net|cdn\.heapanalytics)",
    re.IGNORECASE,
)

# File-extension based blocking
BLOCKED_EXTENSIONS = re.compile(
    r"\.(png|jpe?g|svg|gif|webp|ico|css|woff2?|ttf|eot|mp4|mp3|webm)(\?.*)?$",
    re.IGNORECASE,
)


def _should_block(route: Route, request: Request) -> bool:
    """
    Return True if this request should be aborted.

    Called for EVERY outgoing network request while Playwright is running.
    Keeping this function fast is critical – no I/O, no regex compilation.
    """
    # Block by resource type (fastest check, no string ops)
    if request.resource_type in BLOCKED_RESOURCE_TYPES:
        return True

    url = request.url

    # Block analytics / tracker services by URL keyword
    if BLOCKED_URL_PATTERNS.search(url):
        return True

    # Block static asset extensions
    # (catches resources served as type "other" or "fetch")
    if BLOCKED_EXTENSIONS.search(url):
        return True

    return False


def _intercept(route: Route, request: Request) -> None:
    """Playwright route handler: abort blocked requests, continue the rest."""
    if _should_block(route, request):
        route.abort()
    else:
        route.continue_()


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMISATION 2 – Session state save / load
# ─────────────────────────────────────────────────────────────────────────────

def save_session(context: BrowserContext, path: str = SESSION_STATE_FILE) -> None:
    """
    Persist the full browser session (cookies + origins/localStorage) to disk.

    Call this once right after a successful login.  The file will be loaded
    on the next run to skip the login screen entirely.
    """
    storage = context.storage_state()
    Path(path).write_text(json.dumps(storage, indent=2), encoding="utf-8")
    log.info("✅ Session saved → %s", path)


def session_exists(path: str = SESSION_STATE_FILE) -> bool:
    """Return True if a saved session file exists on disk."""
    return Path(path).is_file()


# ─────────────────────────────────────────────────────────────────────────────
# Browser launch helpers
# ─────────────────────────────────────────────────────────────────────────────

def _launch_context(playwright, use_session: bool) -> tuple:
    """
    Launch a Chromium browser and create a context.

    Args:
        playwright:   The sync Playwright instance.
        use_session:  If True, load cookies from SESSION_STATE_FILE.

    Returns:
        (browser, context) tuple.
    """
    # ── Browser args that further speed up page loads ─────────────────────
    browser_args = [
        "--disable-blink-features=AutomationControlled",   # reduces detection
        "--disable-infobars",
        "--disable-notifications",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",                   # GPU not needed in headless
        "--disable-extensions",
        "--disable-default-apps",
        "--mute-audio",
        "--blink-settings=imagesEnabled=false",            # disable images at engine level
    ]

    # Automatically run headless on Render (servers have no display for headed browsers)
    is_server = os.getenv("RENDER") is not None
    
    browser = playwright.chromium.launch(
        headless=is_server,     # True on Render, False locally so user can see it
        args=browser_args,
        timeout=30_000,
    )

    context_kwargs = {
        "viewport":          {"width": 1280, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "ignore_https_errors": True,
        "java_script_enabled": True,
    }

    if use_session and session_exists():
        # OPTIMISATION 2 – inject saved cookies so we skip the login page
        log.info("🔑 Loading saved session from → %s", SESSION_STATE_FILE)
        context_kwargs["storage_state"] = SESSION_STATE_FILE

    context = browser.new_context(**context_kwargs)

    # OPTIMISATION 1 – register the network interception handler on the context
    # so it applies to EVERY page opened in this context automatically.
    # context.route("**/*", _intercept)
    log.info("🚦 Network interceptor temporarily disabled for testing.")

    return browser, context


# ─────────────────────────────────────────────────────────────────────────────
# Portal login logic
# ─────────────────────────────────────────────────────────────────────────────

def _do_login(page: Page) -> None:
    """
    Perform a full credential-based login on the portal.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ⚠️  CUSTOMISE THIS FUNCTION FOR YOUR PORTAL
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Replace the placeholder selectors below with real ones from your
    Replace the placeholder selectors below with real ones from your
    portal using the instructions at the top of this file.
    """
    log.info("🔐 Navigating to Sangraha login page…")

    # Navigate to the portal root – Sangraha serves the login form at /
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)

    log.info("⌨️  Filling in credentials…")

    # ── SANGRAHA PORTAL SELECTORS (confirmed from live HTML) ──────────────
    # Username field: <input id="UserId" name="UserId" ...>
    page.locator("#UserId").wait_for(state="visible", timeout=15_000)
    page.locator("#UserId").fill(PORTAL_USERNAME)

    # Password field: <input id="passwordInput" name="Password" ...>
    page.locator("#passwordInput").fill(PORTAL_PASSWORD)

    # Submit button: <input type="submit" value="Login" class="btn btn-login">
    page.locator("input[type='submit'][value='Login']").click(no_wait_after=True)

    # After login Sangraha redirects away from the root / page.
    page.wait_for_function(
        "() => !window.location.pathname.endsWith('/') || document.querySelector('#UserId') === null",
        timeout=20_000,
    )
    log.info("✅ Login successful! Current URL: %s", page.url)


def _is_session_valid(page: Page) -> bool:
    """
    Navigate to the home/dashboard page and check if we're still logged in.
    Returns True if the current URL looks like an authenticated page,
    False if we were redirected to a login screen.
    """
    log.info("🔍 Verifying Sangraha session validity…")
    try:
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=20_000)
        login_input_visible = page.locator("#UserId").count() > 0
        if login_input_visible:
            log.warning("⚠️  Session expired or never saved – will perform fresh login.")
            return False
        log.info("✅ Session is valid (URL: %s)", page.url)
        return True
    except Exception as exc:
        log.warning("⚠️  Session check failed (%s) – will re-login.", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Core automation – form filling
# ─────────────────────────────────────────────────────────────────────────────

def _select2_dropdown(page: Page, label_text: str, search_text: str, field_name: str = "Field") -> bool:
    """
    Selects an option in a Select2 custom dropdown widget.

    How Select2 works in this portal (confirmed from screenshot):
      1. There is a container div (the visible dropdown with arrow).
      2. Clicking it opens a search box + list of options.
      3. You TYPE in the search box to filter.
      4. Click the FIRST result that appears.

    Args:
        label_text:         The text of the label above the dropdown (e.g. 'Project Name')
        search_text:        The value to search for (from Excel)
        field_name:         Human-readable label for logging
    """
    try:
        log.info(f"    > Opening Select2 dropdown for '{field_name}'...")

        # Find the label in the newest row (the last one), go up to its parent, then find the select2 container
        # Playwright supports ".." in xpath to get parent.
        label_loc = page.locator(f"label:has-text('{label_text}')").last
        try:
            container = label_loc.locator("xpath=..").locator(".select2-container").first
            container.wait_for(state="visible", timeout=3000)
        except Exception:
            # Fallback if label is nested differently
            container = label_loc.locator("xpath=../..").locator(".select2-container").first
            container.wait_for(state="visible", timeout=3000)

        container.scroll_into_view_if_needed()
        container.click()
        page.wait_for_timeout(500)

        # The search box that appears inside the opened dropdown
        # Select2 renders a .select2-search__field input
        search_input = page.locator(".select2-search__field").last
        search_input.wait_for(state="visible", timeout=5000)

        # Clear and type the search text (use Excel value, replacing _ with space)
        clean_text = search_text.strip().replace("_", " ")
        search_input.fill("")
        search_input.type(clean_text, delay=50)
        page.wait_for_timeout(800)  # Wait for results to filter

        # Get the first visible result from the dropdown list
        first_result = page.locator(".select2-results__option").first
        first_result.wait_for(state="visible", timeout=5000)

        # Log what we're about to select
        result_text = first_result.text_content().strip()
        log.info(f"    ✔ Select2 top result: '{result_text}'")

        # Click the first result
        first_result.click()
        page.wait_for_timeout(500)
        return True

    except Exception as e:
        log.warning(f"    ⚠️ Select2 failed for '{field_name}' (value='{search_text}'): {e}")
        # Press Escape to close any open dropdown before moving on
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

        # Interactive fallback!
        print(f"[BOT_QUESTION] field={field_name} | wanted={search_text} | options=I have manually selected it on the portal", flush=True)
        sys.stdout.flush()
        log.info(f"    ⏸️  Waiting for your input for '{field_name}'…")
        try:
            user_input = input().strip()
        except EOFError:
            pass

        return False


def _fill_hours_input(page: Page, day: str, value: str, row_index: int) -> bool:
    """
    Fill the hours input for a specific day column in the LAST (newest) timesheet row.

    The portal shows day headers (Sun, Mon, Tue...) and input boxes below them.
    We use JavaScript to find the input that is:
      - horizontally aligned with the day label
      - in the bottom-most row (the newly added entry row)
    """
    try:
        js = f"""() => {{
            const dayFull = '{day}'.toLowerCase();
            const dayShort = '{day[:3]}'.toLowerCase();
            
            // Find the day header element. It might contain dates, e.g., 'Mon 05/12'
            const headers = Array.from(document.querySelectorAll('th, td, label, span, div'))
                .filter(el => {{
                    // Only consider elements that actually contain text
                    if (!el.textContent) return false;
                    const t = el.textContent.trim().toLowerCase();
                    // Match 'monday', 'mon', 'mon 12/05', etc.
                    const isDayText = t === dayFull || t === dayShort || 
                                      t.startsWith(dayFull + ' ') || t.startsWith(dayFull + '\\n') || 
                                      t.startsWith(dayShort + ' ') || t.startsWith(dayShort + '\\n');
                    
                    const r = el.getBoundingClientRect();
                    // Must be visible, and ideally a leaf-ish node (or at least not the whole page)
                    return isDayText && r.width > 0 && r.width < 300 && r.height > 0;
                }});

            if (!headers.length) return {{ found: false, reason: 'no header for {day}' }};

            // Get the header's position
            const hdr = headers[headers.length - 1];
            const hRect = hdr.getBoundingClientRect();

            // Find all visible, editable inputs
            const inputs = Array.from(document.querySelectorAll(
                'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([readonly]):not([disabled])'
            )).filter(inp => {{
                const r = inp.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.top > hRect.bottom - 10;
            }});

            // Pick the one horizontally aligned with the day header AND bottom-most
            let best = null;
            let bestTop = -Infinity;

            const leftBound = hRect.left - 30;
            const rightBound = hRect.right + 30;

            inputs.forEach(inp => {{
                const r = inp.getBoundingClientRect();
                const center = r.left + r.width / 2;
                
                if (center >= leftBound && center <= rightBound) {{
                    if (r.top > bestTop) {{
                        bestTop = r.top;
                        best = inp;
                    }}
                }}
            }});

            if (best) {{
                best.setAttribute('data-bot-day', '{day}');
                return {{ found: true, top: bestTop }};
            }}
            return {{ found: false, reason: 'no aligned input' }};
        }}"""

        result = page.evaluate(js)
        if result.get("found"):
            inp = page.locator(f"input[data-bot-day='{day}']")
            inp.scroll_into_view_if_needed()
            inp.click(force=True)
            page.wait_for_timeout(100)
            inp.fill("")
            inp.fill(value)
            inp.press("Tab")
            page.wait_for_timeout(100)
            log.info(f"    ✔ Set {day} = {value}")
            return True
        else:
            log.warning(f"    ⚠️ Could not find input for {day}: {result.get('reason')}")
            return False

    except Exception as e:
        log.warning(f"    ⚠️ Exception filling {day}: {e}")
        return False


def _fill_timesheet(page: Page, tasks: list[dict], week: str, category: str) -> None:
    """
    Main form-filling routine.
    Uses Select2 search-based dropdowns as seen in the Sangraha portal.
    """
    log.info("📋 Moving to Timesheet page…")

    log.info("🖱️ Clicking the 'Timesheet' tab…")
    try:
        page.get_by_text("Timesheet", exact=True).first.click()
    except Exception:
        page.locator("text='Timesheet'").first.click()

    page.wait_for_timeout(2000)

    for idx, task in enumerate(tasks, start=1):
        log.info(f"📝 --- Processing row {idx}/{len(tasks)} ---")

        try:
            # ── Click "Add New Entry" ─────────────────────────────────────────
            log.info("➕ Clicking the 'Add New Entry' button…")
            try:
                page.locator("text='Add New Entry'").first.click()
            except Exception:
                page.get_by_role("button", name=re.compile("Add New Entry", re.I)).first.click()

            page.wait_for_timeout(1500)  # Let form row appear

            # ── 1. Project Name - L1  (Select2 dropdown) ─────────────────────
            project_val = str(task.get("Project Name - L1", "")).strip()
            if project_val and project_val.lower() not in ("nan", "none", "null", ""):
                log.info(f"  > Setting Project Name: {project_val}")
                ok = _select2_dropdown(
                    page,
                    label_text="Project Name",
                    search_text=project_val,
                    field_name="Project Name - L1",
                )
                if ok:
                    page.wait_for_timeout(2500)  # Sub-project options load after project chosen

            # ── 2. Sub Project - L2  (Select2 dropdown) ──────────────────────
            subproject_val = str(task.get("Sub Project - L2", "")).strip()
            if subproject_val and subproject_val.lower() not in ("nan", "none", "null", ""):
                log.info(f"  > Setting Sub Project: {subproject_val}")
                ok = _select2_dropdown(
                    page,
                    label_text="Sub Project",
                    search_text=subproject_val,
                    field_name="Sub Project - L2",
                )
                if ok:
                    page.wait_for_timeout(1500)

            # ── 3. Task Type  (Select2 dropdown) ─────────────────────────────
            task_type_val = str(task.get("Task Type", "")).strip()
            if task_type_val and task_type_val.lower() not in ("nan", "none", "null", ""):
                log.info(f"  > Setting Task Type: {task_type_val}")
                ok = _select2_dropdown(
                    page,
                    label_text="Task Type",
                    search_text=task_type_val,
                    field_name="Task Type",
                )
                if ok:
                    page.wait_for_timeout(1000)

            # ── 4. Task Description ───────────────────────────────────────────
            task_desc = str(task.get("Task Description", "")).strip()
            if task_desc and task_desc.lower() not in ("nan", "none", "null", ""):
                log.info(f"  > Setting Task Description: {task_desc[:50]}…")
                try:
                    page.get_by_placeholder("Enter task description", exact=False).last.fill(task_desc)
                except Exception as e:
                    log.warning(f"  ⚠️ Error setting Task Description: {e}")

            # ── 5. JIRA ID / SR ───────────────────────────────────────────────
            jira_id = str(task.get("JIRA ID / SR", "")).strip()
            if jira_id and jira_id.lower() not in ("nan", "none", "null", ""):
                log.info(f"  > Setting JIRA ID: {jira_id}")
                try:
                    page.get_by_placeholder("e.g., PROJ-001", exact=False).last.fill(jira_id)
                except Exception as e:
                    log.warning(f"  ⚠️ Error setting JIRA ID: {e}")

            # ── 6. Hours per day ─────────────────────────────────────────────
            days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
            for day in days:
                val = str(task.get(day, "0")).strip()
                if val.endswith(".0"):
                    val = val[:-2]
                if val and val not in ("", "nan", "none", "null", "0", "0.0"):
                    _fill_hours_input(page, day, val, idx)

            # ── 7. Save the row ───────────────────────────────────────────────
            log.info("💾 Clicking 'Save' button…")
            saved = False
            for save_selector in [
                lambda: page.get_by_role("button", name=re.compile(r"save", re.I)).first.click(timeout=5000),
                lambda: page.locator("button:has-text('Save')").first.click(timeout=5000),
                lambda: page.locator("text='Save'").first.click(timeout=5000),
            ]:
                try:
                    save_selector()
                    saved = True
                    break
                except Exception:
                    continue
            if not saved:
                log.warning("  ⚠️ Could not click Save button.")

        except Exception as row_exc:
            import traceback as _tb
            log.error(f"❌ Unhandled error in row {idx}: {row_exc}")
            log.error(_tb.format_exc())
            log.warning("⚠️ Skipping to next row…")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        page.wait_for_timeout(2000)  # Brief pause between rows

    log.info("🎉 All rows processed. Review the portal before submitting.")
    page.wait_for_timeout(600_000)
    return




# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_bot(
    tasks: list[dict],
    week: str,
    category: str,
    force_relogin: bool = False,
) -> None:
    """
    Orchestrates the full automation flow:
      1. Launch browser with network interception.
      2. Load saved session OR perform fresh login.
      3. Fill the timesheet form.
      4. Save session on fresh logins.
      5. Close browser.

    Args:
        tasks:         List of task dicts (from JiraClient.fetch_issues).
        week:          The week string to select in the portal dropdown.
        category:      The task category to select.
        force_relogin: If True, ignore any saved session and log in fresh.
    """
    if not PORTAL_URL:
        log.error("❌ PORTAL_URL is not set in your .env file – aborting.")
        sys.exit(1)

    log.info("🤖 Starting Playwright bot (headless=False)")
    start_time = time.perf_counter()

    with sync_playwright() as pw:
        use_saved_session = session_exists() and not force_relogin
        browser, context = _launch_context(pw, use_saved_session)

        try:
            page = context.new_page()

            # Maximise default timeouts; individual operations override as needed
            page.set_default_timeout(30_000)
            page.set_default_navigation_timeout(30_000)

            # ── Session validation / login ─────────────────────────────────
            if use_saved_session:
                valid = _is_session_valid(page)
                if not valid:
                    # Saved session expired → re-login and save fresh session
                    _do_login(page)
                    save_session(context)
            else:
                # No saved session → fresh login
                _do_login(page)
                save_session(context)

            # ── Form filling ───────────────────────────────────────────────
            if not tasks:
                log.warning("⚠️  No tasks were passed to the bot – nothing to fill.")
            else:
                _fill_timesheet(page, tasks, week, category)

        finally:
            elapsed = time.perf_counter() - start_time
            log.info("⏱️  Total bot runtime: %.1f seconds", elapsed)
            context.close()
            browser.close()
            log.info("🛑 Browser closed cleanly.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point (called as a subprocess by app.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    When Streamlit calls this script via subprocess, it passes the task data
    as a JSON string and the portal options as arguments.

    Example invocation (from app.py):
        python automation_bot.py \
            --tasks '[{"key":"PROJ-1","summary":"Fix bug","story_points":3}]' \
            --week  "Week 22 (Jun 2–6, 2025)" \
            --category "Development" \
            --force-relogin
    """
    parser = argparse.ArgumentParser(description="Jira Portal Automation Bot")
    parser.add_argument(
        "--tasks",
        type=str,
        required=True,
        help="JSON string of task list (output from JiraClient.fetch_issues)",
    )
    parser.add_argument(
        "--week",
        type=str,
        required=True,
        help="Week label to select in the portal dropdown",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="Development",
        help="Task category to select in the portal dropdown",
    )
    parser.add_argument(
        "--force-relogin",
        action="store_true",
        default=False,
        help="Ignore saved session and perform a fresh login",
    )

    args = parser.parse_args()

    try:
        task_list = json.loads(args.tasks)
    except json.JSONDecodeError as exc:
        log.error("❌ Failed to parse --tasks JSON: %s", exc)
        sys.exit(2)

    run_bot(
        tasks=task_list,
        week=args.week,
        category=args.category,
        force_relogin=args.force_relogin,
    )
