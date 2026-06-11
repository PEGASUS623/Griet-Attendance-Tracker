"""
GRIET Attendance Tracker - Production Ready Database Backend
Run: pip install flask flask-cors flask-sqlalchemy selenium webdriver-manager beautifulsoup4 selenium-stealth
Then: python app.py
"""

import os
import time
import math
import threading
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

app = Flask(__name__, static_folder='static')
CORS(app)  # Enables frontend connections globally

# --- DATABASE CONFIGURATION ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///griet_attendance.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Global runtime state for the background scraping thread
state = {"status": "idle", "data": None, "error": None, "log": []}

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_number = db.Column(db.String(20), unique=True, nullable=False)
    portal_password = db.Column(db.String(200), nullable=False)  # Saved securely hashed
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

# --- HELPERS & UTILITIES ---
def log(msg):
    print(msg)
    state["log"].append(msg)
    if len(state["log"]) > 20:
        state["log"].pop(0)

def calc_needed(total, attended):
    if total == 0 or attended / total >= 0.75: return 0
    return math.ceil((0.75 * total - attended) / 0.25)

def calc_can_skip(total, attended):
    if total == 0: return 0
    return max(math.floor((attended - 0.75 * total) / 0.25), 0)

def parse_attendance(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2: continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th","td"])]
        subj_idx   = next((i for i,h in enumerate(headers) if any(k in h for k in ("subject","course","paper","name"))), None)
        total_idx  = next((i for i,h in enumerate(headers) if any(k in h for k in ("total","conducted","held","classes"))), None)
        attend_idx = next((i for i,h in enumerate(headers) if any(k in h for k in ("present","attended","attend"))), None)
        pct_idx    = next((i for i,h in enumerate(headers) if "%" in h or "percent" in h), None)
        
        if subj_idx is None or (total_idx is None and attend_idx is None): continue
        for row in rows[1:]:
            cells = row.find_all(["td","th"])
            try:
                subject = cells[subj_idx].get_text(strip=True)
                if not subject or subject.lower() in ("total","grand total",""): continue
                total    = int(cells[total_idx].get_text(strip=True).replace(",",""))  if total_idx  is not None and total_idx  < len(cells) else 0
                attended = int(cells[attend_idx].get_text(strip=True).replace(",","")) if attend_idx is not None and attend_idx < len(cells) else 0
                pct = round(attended/total*100, 2) if total else 0
                if pct_idx is not None and pct_idx < len(cells):
                    try: pct = float(cells[pct_idx].get_text(strip=True).replace("%","").strip())
                    except: pass
                needed    = calc_needed(total, attended)
                skippable = calc_can_skip(total, attended)
                results.append({
                    "subject": subject, "total": total, "attended": attended,
                    "percent": pct, "needed": needed, "skippable": skippable,
                    "status": "safe" if pct >= 75 else ("warning" if pct >= 65 else "risk")
                })
            except (ValueError, IndexError): continue
        if results: break
    return results

def wait_for_table_in_iframe(driver, timeout=30):
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
                    if any(k in text for k in ("present","attended","conducted","held")):
                        return html, "table_found"
                radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if radios:
                    return html, "form_found"
                driver.switch_to.default_content()
            except:
                driver.switch_to.default_content()
        time.sleep(1.5)
    return None, "timeout"

# --- CORE BACKGROUND SCRAPER WORKER ---
def scrape_worker(username, password):
    state["status"] = "running"
    state["error"]  = None
    state["log"]    = []

    options = webdriver.ChromeOptions()
    # Production ready: Headless with full anti-bot stealth mechanisms attached
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1200,800")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    try:
        log("Launching headless engine with cloud mitigation shields...")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        
        # Inject stealth mappings
        stealth(driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
        
        # Step 1: Login via official WebPros India domain mappings
        log("Navigating to WebPros India GRIET Login page...")
        driver.get("https://www.webprosindia.com/gokaraju/default.aspx")
        time.sleep(2)
        
        log("Submitting student portal authentication parameters...")
        driver.find_element(By.NAME, "txtId2").send_keys(username)
        driver.find_element(By.NAME, "txtPwd2").send_keys(password)
        driver.find_element(By.NAME, "imgBtn2").click()
        time.sleep(4)

        if "default.aspx" in driver.current_url.lower():
            state["status"] = "error"
            state["error"]  = "Login verification rejected. Double check your credentials."
            return
        log("Session connection successfully verified!")

        # Step 2: Load dashboard internal views
        log("Requesting internal student master profiles...")
        driver.get("https://www.webprosindia.com/gokaraju/StudentMaster.aspx")
        time.sleep(5)

        # Step 3: Trigger navigation into Attendance module
        log("Resolving interactive main elements...")
        clicked = False
        try:
            links = driver.find_elements(By.TAG_NAME, "a")
            for link in links:
                if link.text.strip().upper() == "ATTENDANCE":
                    driver.execute_script("arguments[0].click();", link)
                    clicked = True
                    log("Attendance panel link triggered dynamically!")
                    time.sleep(4)
                    break
        except Exception as e:
            log(f"Menu click interaction warn: {e}")

        if not clicked:
            log("Explicit navigation fallback initiated...")
            driver.execute_script("""
                var iframe = document.getElementById('capIframeId');
                if (iframe) {
                    iframe.src = 'Academics/StudentAttendance.aspx?crid=3&showtype=SA';
                }
            """)
            time.sleep(4)

        # Step 4: Parse child secure iFrames
        log("Syncing child context subframes...")
        html, result = wait_for_table_in_iframe(driver, timeout=15)

        if result == "timeout" or html is None:
            log("Running primary frame context fallback queries...")
            driver.switch_to.default_content()
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            if not iframes:
                state["status"] = "error"
                state["error"]  = "Context canvas elements could not be verified."
                return
            driver.switch_to.frame(iframes[0])
            html = driver.page_source
            result = "form_found"

        # Step 5: Execute form tracking filters
        if result == "form_found":
            log("Resolving metrics filtering radios...")
            radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
            if radios:
                driver.execute_script("arguments[0].click();", radios[-1])
                time.sleep(1)

            log("Executing query submission button...")
            all_btns = driver.find_elements(By.CSS_SELECTOR,
                "input[type='submit'], input[type='image'], input[type='button'], button")
            for btn in all_btns:
                txt = (btn.get_attribute("value") or btn.text or "").lower()
                src = (btn.get_attribute("src") or "").lower()
                if "show" in txt or "show" in src:
                    driver.execute_script("arguments[0].click();", btn)
                    log("Query dispatched successfully.")
                    time.sleep(4)
                    break
            else:
                if all_btns:
                    driver.execute_script("arguments[0].click();", all_btns[0])
                    time.sleep(4)

            log("Fetching processed updates layout structural tables...")
            html, result = wait_for_table_in_iframe(driver, timeout=20)
            if html is None:
                html = driver.page_source

        # Step 6: Parse extracted structural payloads & Sync local DB
        log("Compiling document logs...")
        with open("attendance_raw.html", "w", encoding="utf-8") as f:
            f.write(html or "")

        data = parse_attendance(html or "")

        if data:
            total_att = sum(s["attended"] for s in data)
            total_cls = sum(s["total"]    for s in data)
            overall   = round(total_att / total_cls * 100, 2) if total_cls else 0
            
            # --- DATABASE PERSISTENCE WRITING ---
            with app.app_context():
                # Wipe outdated local records
                AttendanceRecord.query.filter_by(roll_number=username.upper()).delete()
                
                # Push active subject maps
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
                
                # Register meta profile logs
                u = User.query.filter_by(roll_number=username.upper()).first()
                if not u:
                    u = User(roll_number=username.upper(), portal_password=generate_password_hash(password))
                    db.session.add(u)
                u.last_updated = datetime.now().strftime("%Y-%m-%d")
                db.session.commit()

            state["data"] = {
                "subjects": data, "overall": overall,
                "total_attended": total_att, "total_classes": total_cls,
                "fetched_at": datetime.now().strftime("%d %b %Y, %I:%M %p")
            }
            state["status"] = "done"
            log(f"Successfully processed metrics mapping tracking {len(data)} items!")
        else:
            state["status"] = "error"
            state["error"]  = "Parsing structural engine mismatch. Check logs data metrics mapping formatting structures inside local raw output HTML files."

    except Exception as e:
        state["status"] = "error"
        state["error"]  = str(e)
        log(f"Core Thread Error Exception: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

# --- ROUTINGS & CONTROLLERS API ---
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/scrape", methods=["POST"])
def scrape():
    body = request.json
    username = body.get("username", "").strip().upper()
    password = body.get("password", "").strip()
    
    if not username or not password:
        return jsonify({"error": "Fields validation parameters required."}), 400

    # ⚡ DATABASE CACHE HIT LOOKUP
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
            
            # Serve instant database response bypassing WebPros entirely
            state["status"] = "done"
            state["data"] = {
                "subjects": subjects_list,
                "overall": round(total_att / total_cls * 100, 2) if total_cls else 0,
                "total_attended": total_att, "total_classes": total_cls,
                "fetched_at": datetime.now().strftime("%d %b %Y, %I:%M %p") + " (Local Cached Storage)"
            }
            return jsonify({"status": "started", "cached": True})

    if state["status"] == "running":
        return jsonify({"error": "Processing request already active on core system."}), 400
        
    t = threading.Thread(target=scrape_worker, args=(username, password))
    t.daemon = True
    t.start()
    return jsonify({"status": "started", "cached": False})

@app.route("/api/status")
def status():
    return jsonify({
        "status": state["status"],
        "data":   state["data"],
        "error":  state["error"],
        "log":    state["log"]
    })

@app.route("/api/simulate", methods=["POST"])
def simulate():
    body      = request.json
    total     = int(body.get("total", 0))
    attended  = int(body.get("attended", 0))
    bunk      = int(body.get("bunk", 1))
    new_total = total + bunk
    new_pct   = round(attended / new_total * 100, 2) if new_total else 0
    return jsonify({
        "new_total": new_total, "attended": attended,
        "new_pct": new_pct,
        "needed":    calc_needed(new_total, attended),
        "skippable": calc_can_skip(new_total, attended),
        "status": "safe" if new_pct >= 75 else ("warning" if new_pct >= 65 else "risk")
    })

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    print("\n" + "="*55)
    print("  GRIET Official Production Backend Online")
    print("  Listening locally on http://localhost:5000")
    print("="*55 + "\n")
    app.run(debug=False, port=5000, threaded=True)