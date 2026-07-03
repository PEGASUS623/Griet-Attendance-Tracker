"""
GRIET Attendance Tracker - Production Ready Database Backend (Multi-User Safe)
Run: pip install flask flask-cors flask-sqlalchemy selenium webdriver-manager beautifulsoup4 selenium-stealth
Then: python app.py
"""

import os
import time
import math
import uuid
import threading
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

app = Flask(__name__, static_folder='static')
CORS(app)  # Enables frontend connections globally

# --- DATABASE CONFIGURATION ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///griet_attendance.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- PER-JOB STATE (fixes the multi-user bug) ---
# Each scrape request gets its own job_id and its own isolated state entry.
# This replaces the old single global `state` dict that leaked data between users.
jobs = {}
jobs_lock = threading.Lock()  # protects the `jobs` dict itself from race conditions

# --- CONCURRENCY LIMIT ---
# Each Selenium/Chromium instance uses ~200-300MB RAM. Render's free tier has
# ~512MB total, so we cap concurrent scrapes to avoid the server crashing.
MAX_CONCURRENT_SCRAPES = 2
scrape_semaphore = threading.Semaphore(MAX_CONCURRENT_SCRAPES)

JOB_TTL_MINUTES = 15  # stale jobs older than this get cleaned up automatically


# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_number = db.Column(db.String(20), unique=True, nullable=False)
    portal_password = db.Column(db.String(200), nullable=False)  # stored hashed, never used for login
    last_updated = db.Column(db.String(20))


class AttendanceRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_number = db.Column(db.String(20), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    total_classes = db.Column(db.Integer, nullable=False)
    attended_classes = db.Column(db.Integer, nullable=False)
    percentage = db.Column(db.Float, nullable=False)
    needed = db.Column(db.Integer, nullable=False)
    skippable = db.Column(db.Integer, nullable=False)
    status_tag = db.Column(db.String(20), nullable=False)


# Auto-generate database tables
with app.app_context():
    db.create_all()


# --- JOB HELPERS ---
def new_job():
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "idle",
            "data": None,
            "error": None,
            "log": [],
            "created_at": datetime.now(),
        }
    return job_id


def log(job_id, msg):
    print(f"[{job_id[:8]}] {msg}")
    job = jobs.get(job_id)
    if job is None:
        return
    job["log"].append(msg)
    if len(job["log"]) > 20:
        job["log"].pop(0)


def cleanup_old_jobs():
    """Remove finished/stale jobs so the `jobs` dict doesn't grow forever."""
    cutoff = datetime.now() - timedelta(minutes=JOB_TTL_MINUTES)
    with jobs_lock:
        stale_ids = [jid for jid, j in jobs.items() if j["created_at"] < cutoff]
        for jid in stale_ids:
            del jobs[jid]


# --- HELPERS & UTILITIES ---
def calc_needed(total, attended):
    if total == 0 or attended / total >= 0.75:
        return 0
    return math.ceil((0.75 * total - attended) / 0.25)


def calc_can_skip(total, attended):
    if total == 0:
        return 0
    return max(math.floor((attended - 0.75 * total) / 0.25), 0)


def parse_attendance(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        subj_idx = next((i for i, h in enumerate(headers) if any(k in h for k in ("subject", "course", "paper", "name"))), None)
        total_idx = next((i for i, h in enumerate(headers) if any(k in h for k in ("total", "conducted", "held", "classes"))), None)
        attend_idx = next((i for i, h in enumerate(headers) if any(k in h for k in ("present", "attended", "attend"))), None)
        pct_idx = next((i for i, h in enumerate(headers) if "%" in h or "percent" in h), None)

        if subj_idx is None or (total_idx is None and attend_idx is None):
            continue
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            try:
                subject = cells[subj_idx].get_text(strip=True)
                if not subject or subject.lower() in ("total", "grand total", ""):
                    continue
                total = int(cells[total_idx].get_text(strip=True).replace(",", "")) if total_idx is not None and total_idx < len(cells) else 0
                attended = int(cells[attend_idx].get_text(strip=True).replace(",", "")) if attend_idx is not None and attend_idx < len(cells) else 0
                pct = round(attended / total * 100, 2) if total else 0
                if pct_idx is not None and pct_idx < len(cells):
                    try:
                        pct = float(cells[pct_idx].get_text(strip=True).replace("%", "").strip())
                    except:
                        pass
                needed = calc_needed(total, attended)
                skippable = calc_can_skip(total, attended)
                results.append({
                    "subject": subject, "total": total, "attended": attended,
                    "percent": pct, "needed": needed, "skippable": skippable,
                    "status": "safe" if pct >= 75 else ("warning" if pct >= 65 else "risk")
                })
            except (ValueError, IndexError):
                continue
        if results:
            break
    return results


def wait_for_table_in_iframe(driver, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        driver.switch_to.default_content()
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for iframe in iframes:
            try:
                driver.switch_to.frame(iframe)
                html = driver.page_source
                soup = BeautifulSoup(html, "html.parser")
                for table in soup.find_all("table"):
                    text = table.get_text().lower()
                    if any(k in text for k in ("present", "attended", "conducted", "held")):
                        return html, "table_found"
                radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if radios:
                    return html, "form_found"
                driver.switch_to.default_content()
            except:
                driver.switch_to.default_content()
        time.sleep(0.5)  # short poll interval instead of one long fixed sleep
    return None, "timeout"


def wait_until(condition_fn, timeout=8, poll=0.3):
    """Generic short-poll wait: retries condition_fn() until True or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if condition_fn():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


# --- CORE BACKGROUND SCRAPER WORKER ---
def scrape_worker(username, password, job_id):
    # Blocks here if MAX_CONCURRENT_SCRAPES are already running.
    # Keeps status visible as "queued" while waiting for a free slot.
    jobs[job_id]["status"] = "queued"
    with scrape_semaphore:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["error"] = None

        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1200,800")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        if os.environ.get('RENDER'):
            possible_paths = ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
            for path in possible_paths:
                if os.path.exists(path):
                    options.binary_location = path
                    break

        driver = None
        try:
            log(job_id, "Launching headless engine with cloud mitigation shields...")

            if os.environ.get('RENDER'):
                driver = webdriver.Chrome(options=options)
            else:
                driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

            stealth(driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )

            # Step 1: Login
            log(job_id, "Navigating to WebPros India GRIET Login page...")
            driver.get("https://www.webprosindia.com/gokaraju/default.aspx")
            wait_until(lambda: driver.find_elements(By.NAME, "txtId2"), timeout=10)

            log(job_id, "Submitting student portal authentication parameters...")
            driver.find_element(By.NAME, "txtId2").send_keys(username)
            driver.find_element(By.NAME, "txtPwd2").send_keys(password)
            driver.find_element(By.NAME, "imgBtn2").click()

            # Wait until the URL actually changes away from the login page,
            # instead of blindly sleeping for a fixed 4 seconds.
            wait_until(lambda: "default.aspx" not in driver.current_url.lower(), timeout=10)

            if "default.aspx" in driver.current_url.lower():
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "Login verification rejected. Double check your credentials."
                return
            log(job_id, "Session connection successfully verified!")

            # Step 2: Load dashboard
            log(job_id, "Requesting internal student master profiles...")
            driver.get("https://www.webprosindia.com/gokaraju/StudentMaster.aspx")
            wait_until(lambda: driver.find_elements(By.TAG_NAME, "a"), timeout=10)

            # Step 3: Navigate into Attendance module
            log(job_id, "Resolving interactive main elements...")
            clicked = False
            try:
                links = driver.find_elements(By.TAG_NAME, "a")
                for link in links:
                    if link.text.strip().upper() == "ATTENDANCE":
                        driver.execute_script("arguments[0].click();", link)
                        clicked = True
                        log(job_id, "Attendance panel link triggered dynamically!")
                        wait_until(lambda: driver.find_elements(By.TAG_NAME, "iframe"), timeout=8)
                        break
            except Exception as e:
                log(job_id, f"Menu click interaction warn: {e}")

            if not clicked:
                log(job_id, "Explicit navigation fallback initiated...")
                driver.execute_script("""
                    var iframe = document.getElementById('capIframeId');
                    if (iframe) {
                        iframe.src = 'Academics/StudentAttendance.aspx?crid=3&showtype=SA';
                    }
                """)
                wait_until(lambda: driver.find_elements(By.TAG_NAME, "iframe"), timeout=8)

            # Step 4: Parse child iframes
            log(job_id, "Syncing child context subframes...")
            html, result = wait_for_table_in_iframe(driver, timeout=15)

            if result == "timeout" or html is None:
                log(job_id, "Running primary frame context fallback queries...")
                driver.switch_to.default_content()
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                if not iframes:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = "Context canvas elements could not be verified."
                    return
                driver.switch_to.frame(iframes[0])
                html = driver.page_source
                result = "form_found"

            # Step 5: Execute form filters
            if result == "form_found":
                log(job_id, "Resolving metrics filtering radios...")
                radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if radios:
                    driver.execute_script("arguments[0].click();", radios[-1])
                    time.sleep(0.5)

                log(job_id, "Executing query submission button...")
                all_btns = driver.find_elements(By.CSS_SELECTOR,
                    "input[type='submit'], input[type='image'], input[type='button'], button")
                clicked_btn = False
                for btn in all_btns:
                    txt = (btn.get_attribute("value") or btn.text or "").lower()
                    src = (btn.get_attribute("src") or "").lower()
                    if "show" in txt or "show" in src:
                        driver.execute_script("arguments[0].click();", btn)
                        log(job_id, "Query dispatched successfully.")
                        clicked_btn = True
                        break
                if not clicked_btn and all_btns:
                    driver.execute_script("arguments[0].click();", all_btns[0])
                    clicked_btn = True

                if clicked_btn:
                    wait_until(lambda: driver.find_elements(By.TAG_NAME, "iframe"), timeout=8)

                log(job_id, "Fetching processed updates layout structural tables...")
                html, result = wait_for_table_in_iframe(driver, timeout=20)
                if html is None:
                    html = driver.page_source

            # Step 6: Parse & persist
            log(job_id, "Compiling document logs...")
            with open(f"attendance_raw_{job_id[:8]}.html", "w", encoding="utf-8") as f:
                f.write(html or "")

            data = parse_attendance(html or "")

            if data:
                total_att = sum(s["attended"] for s in data)
                total_cls = sum(s["total"] for s in data)
                overall = round(total_att / total_cls * 100, 2) if total_cls else 0

                with app.app_context():
                    AttendanceRecord.query.filter_by(roll_number=username.upper()).delete()
                    for s in data:
                        rec = AttendanceRecord(
                            roll_number=username.upper(),
                            subject_name=s["subject"],
                            total_classes=s["total"],
                            attended_classes=s["attended"],
                            percentage=s["percent"],
                            needed=s["needed"],
                            skippable=s["skippable"],
                            status_tag=s["status"]
                        )
                        db.session.add(rec)

                    u = User.query.filter_by(roll_number=username.upper()).first()
                    if not u:
                        u = User(roll_number=username.upper(), portal_password=generate_password_hash(password))
                        db.session.add(u)
                    u.last_updated = datetime.now().strftime("%Y-%m-%d")
                    db.session.commit()

                jobs[job_id]["data"] = {
                    "subjects": data, "overall": overall,
                    "total_attended": total_att, "total_classes": total_cls,
                    "fetched_at": datetime.now().strftime("%d %b %Y, %I:%M %p")
                }
                jobs[job_id]["status"] = "done"
                log(job_id, f"Successfully processed metrics mapping tracking {len(data)} items!")
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "Parsing structural engine mismatch. Check the raw output HTML file for this job."

        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
            log(job_id, f"Core Thread Error Exception: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass


# --- ROUTES & CONTROLLERS ---
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/scrape", methods=["POST"])
def scrape():
    cleanup_old_jobs()

    body = request.json or {}
    username = body.get("username", "").strip().upper()
    password = body.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "Fields validation parameters required."}), 400

    job_id = new_job()

    # --- DATABASE CACHE HIT LOOKUP (per-user, unaffected by other users now) ---
    today = datetime.now().strftime("%Y-%m-%d")
    cached_user = User.query.filter_by(roll_number=username).first()

    if cached_user and cached_user.last_updated == today:
        records = AttendanceRecord.query.filter_by(roll_number=username).all()
        if records:
            subjects_list = []
            total_att = 0
            total_cls = 0
            for r in records:
                total_att += r.attended_classes
                total_cls += r.total_classes
                subjects_list.append({
                    "subject": r.subject_name, "total": r.total_classes, "attended": r.attended_classes,
                    "percent": r.percentage, "needed": r.needed, "skippable": r.skippable, "status": r.status_tag
                })

            jobs[job_id]["status"] = "done"
            jobs[job_id]["data"] = {
                "subjects": subjects_list,
                "overall": round(total_att / total_cls * 100, 2) if total_cls else 0,
                "total_attended": total_att, "total_classes": total_cls,
                "fetched_at": datetime.now().strftime("%d %b %Y, %I:%M %p") + " (Local Cached Storage)"
            }
            return jsonify({"status": "started", "cached": True, "job_id": job_id})

    # Each user's scrape now runs as its own thread with its own job_id.
    # The semaphore inside scrape_worker caps how many run concurrently,
    # so this is safe to kick off immediately instead of rejecting the request.
    t = threading.Thread(target=scrape_worker, args=(username, password, job_id))
    t.daemon = True
    t.start()
    return jsonify({"status": "started", "cached": False, "job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Invalid or expired job_id"}), 404
    return jsonify({
        "status": job["status"],
        "data": job["data"],
        "error": job["error"],
        "log": job["log"]
    })


@app.route("/api/simulate", methods=["POST"])
def simulate():
    body = request.json or {}
    total = int(body.get("total", 0))
    attended = int(body.get("attended", 0))
    bunk = int(body.get("bunk", 1))
    new_total = total + bunk
    new_pct = round(attended / new_total * 100, 2) if new_total else 0
    return jsonify({
        "new_total": new_total, "attended": attended,
        "new_pct": new_pct,
        "needed": calc_needed(new_total, attended),
        "skippable": calc_can_skip(new_total, attended),
        "status": "safe" if new_pct >= 75 else ("warning" if new_pct >= 65 else "risk")
    })


if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)

    port = int(os.environ.get("PORT", 5000))

    print("\n" + "=" * 55)
    print("  GRIET Official Production Backend Online (Multi-User)")
    print(f"  Listening globally on http://0.0.0.0:{port}")
    print(f"  Max concurrent scrapes: {MAX_CONCURRENT_SCRAPES}")
    print("=" * 55 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
