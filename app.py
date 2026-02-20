from flask import Flask, render_template, jsonify, request, session, redirect, url_for
import requests
import re
import threading
import time
import os
from datetime import datetime
from functools import wraps
import logging
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'change-me-in-production-abc123')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class Config:
    CALTRANS_URL = 'https://roads.dot.ca.gov/roadscell.php?roadnumber=80'
    ZAPIER_WEBHOOK_URL = os.getenv(
        'ZAPIER_WEBHOOK_URL',
        'https://hooks.zapier.com/hooks/catch/22340835/uck413m/')
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 300))  # seconds
    SITE_PASSWORD = os.getenv('SITE_PASSWORD', 'tahoe')

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
monitoring_active = False
monitoring_thread = None
current_road_status = {
    'status': 'unknown',
    'closed_eastbound': False,
    'closed_westbound': False,
    'closures': [],
    'chain_controls': [],
    'other_restrictions': [],
    'sierra_raw': '',
    'last_check': None,
}

# ---------------------------------------------------------------------------
# Caltrans Scraper & Parser
# ---------------------------------------------------------------------------
class RoadMonitor:

    def fetch_caltrans_page(self):
        """Fetch the Caltrans road conditions page for I-80 with robust retry logic."""
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # Try multiple approaches in order to handle Render network issues
        approaches = [
            ('Standard with close connection', self._fetch_with_close),
            ('Session with retry logic', self._fetch_with_retry),
            ('Simple fallback', self._fetch_simple)
        ]
        
        for approach_name, fetch_func in approaches:
            try:
                logger.info(f"Trying {approach_name}...")
                result = fetch_func()
                if result:
                    logger.info(f"Success with {approach_name}")
                    return result
                logger.warning(f"{approach_name} returned no data")
            except Exception as e:
                logger.error(f"{approach_name} failed: {e}")
                continue
        
        logger.error("All fetch approaches failed")
        return None
    
    def _fetch_with_close(self):
        """Approach 1: Force connection close to avoid hanging"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'close',  # Force connection close to avoid hanging
        }
        resp = requests.get(Config.CALTRANS_URL, headers=headers, timeout=8)
        resp.raise_for_status()
        return resp.text
    
    def _fetch_with_retry(self):
        """Approach 2: Session with retry strategy"""
        session = requests.Session()
        
        # Configure retries for network issues
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504, 524],
            allowed_methods=['HEAD', 'GET', 'OPTIONS']
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=1)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Connection': 'close'
        }
        session.headers.update(headers)
        
        resp = session.get(Config.CALTRANS_URL, timeout=8)
        resp.raise_for_status()
        return resp.text
    
    def _fetch_simple(self):
        """Approach 3: Minimal simple request"""
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; I80Monitor/1.0)'}
        resp = requests.get(Config.CALTRANS_URL, headers=headers, timeout=5)
        resp.raise_for_status()
        return resp.text

    def extract_sierra_section(self, html):
        """Pull out the NORTHERN CALIFORNIA / SIERRA NEVADA section text."""
        # Strip HTML tags to get plain text
        text = re.sub(r'<[^>]+>', '\n', html)
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

        # Find the Sierra Nevada section
        sierra_pattern = re.compile(
            r'\[IN THE NORTHERN CALIFORNIA AREA\s*&\s*SIERRA NEVADA\](.*?)(?:\[|$)',
            re.DOTALL | re.IGNORECASE)
        match = sierra_pattern.search(text)
        if match:
            return match.group(1).strip()
        return ''

    def parse_sierra_conditions(self, sierra_text):
        """Parse the Sierra section into closures, chain controls, and other.

        Returns dict with:
          closures: list of {text, direction} — full closures to all vehicles
          chain_controls: list of {text, direction}
          other_restrictions: list of str
          closed_eastbound: bool
          closed_westbound: bool
        """
        closures = []
        chain_controls = []
        other_restrictions = []
        closed_eb = False
        closed_wb = False

        if not sierra_text:
            return {
                'closures': closures,
                'chain_controls': chain_controls,
                'other_restrictions': other_restrictions,
                'closed_eastbound': False,
                'closed_westbound': False,
            }

        # Caltrans wraps long statements across multiple lines.
        # First join on double-newline to get paragraphs, then clean up.
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', sierra_text) if p.strip()]
        lines = []
        for p in paragraphs:
            # Collapse internal newlines into spaces
            line = re.sub(r'\s*\n\s*', ' ', p).strip()
            if line:
                lines.append(line)

        for line in lines:
            lower = line.lower()

            # --- Full closure (all vehicles) ---
            # Pattern: "Is closed from X to Y" WITHOUT "tractor-semitrailer"
            #          or "closed to all tractor-semitrailer" (truck-only)
            is_full_closure = (
                'is closed' in lower
                and 'tractor-semitrailer' not in lower
                and 'semi' not in lower.split('closed')[0]  # not "closed to semis"
            )

            if is_full_closure:
                direction = self._detect_direction(line)
                closures.append({'text': line, 'direction': direction})
                if direction in ('eastbound', 'both'):
                    closed_eb = True
                if direction in ('westbound', 'both'):
                    closed_wb = True
                # If no specific direction, it applies to both
                if direction == 'both' or direction == 'unknown':
                    closed_eb = True
                    closed_wb = True
                continue

            # --- Chain controls ---
            if 'chains are required' in lower:
                direction = 'unknown'
                if 'eastbound' in lower:
                    direction = 'eastbound'
                elif 'westbound' in lower:
                    direction = 'westbound'
                # Clean up the **For prefix
                clean = re.sub(r'^\*\*For\s+(East|West)bound\s+Traffic:\s*', '', line).strip()
                chain_controls.append({'text': clean or line, 'direction': direction})
                continue

            # --- Truck-only closures and other restrictions ---
            if ('tractor-semitrailer' in lower or 'trucks' in lower
                    or 'advisory' in lower or 'brake check' in lower
                    or 'permit' in lower or 'not recommended' in lower):
                other_restrictions.append(line)
                continue

            # Skip Caltrans boilerplate
            if 'please research' in lower or 'mapquest' in lower:
                continue

            # Anything else with substance
            if len(line) > 20:
                other_restrictions.append(line)

        return {
            'closures': closures,
            'chain_controls': chain_controls,
            'other_restrictions': other_restrictions,
            'closed_eastbound': closed_eb,
            'closed_westbound': closed_wb,
        }

    def _detect_direction(self, text):
        """Detect direction from a closure line. If no direction keyword,
        assume both directions."""
        lower = text.lower()
        has_eb = 'eastbound' in lower
        has_wb = 'westbound' in lower
        if has_eb and has_wb:
            return 'both'
        if has_eb:
            return 'eastbound'
        if has_wb:
            return 'westbound'
        return 'both'  # no direction = assume full closure both ways

    def determine_status(self, parsed):
        """Map parsed conditions to a single status string."""
        eb = parsed['closed_eastbound']
        wb = parsed['closed_westbound']
        if eb and wb:
            return 'closed_both'
        if eb:
            return 'closed_eastbound'
        if wb:
            return 'closed_westbound'
        if parsed['chain_controls']:
            return 'chains_required'
        return 'open'

    # -- Alerts -------------------------------------------------------------

    def send_alert(self, msg_body):
        """Send alert via Zapier webhook."""
        if not Config.ZAPIER_WEBHOOK_URL:
            logger.warning("No Zapier webhook configured — would send: " + msg_body)
            return False
        try:
            resp = requests.post(Config.ZAPIER_WEBHOOK_URL, json={
                'message': msg_body,
                'status': current_road_status.get('status', 'unknown'),
                'timestamp': datetime.now().isoformat(),
            }, timeout=10)
            logger.info(f"Zapier webhook fired: {resp.status_code}")
            return True
        except Exception as e:
            logger.error(f"Zapier alert failed: {e}")
            return False

    # -- Main check ---------------------------------------------------------

    def check_road_conditions(self):
        """Scrape Caltrans, parse Sierra section, detect changes, alert."""
        global current_road_status
        logger.info("Checking I-80 Sierra Nevada conditions...")

        html = self.fetch_caltrans_page()
        if not html:
            logger.error("Failed to fetch Caltrans page")
            # Update status to indicate fetch failure
            current_road_status.update({
                'status': 'fetch_error',
                'last_check': datetime.now().isoformat(),
            })
            return

        sierra_text = self.extract_sierra_section(html)
        parsed = self.parse_sierra_conditions(sierra_text)
        new_status = self.determine_status(parsed)
        previous_status = current_road_status.get('status', 'unknown')

        current_road_status.update({
            'status': new_status,
            'closed_eastbound': parsed['closed_eastbound'],
            'closed_westbound': parsed['closed_westbound'],
            'closures': parsed['closures'],
            'chain_controls': parsed['chain_controls'],
            'other_restrictions': parsed['other_restrictions'],
            'sierra_raw': sierra_text,
            'last_check': datetime.now().isoformat(),
        })

        # Alert on status change (skip initial unknown → X transition)
        if previous_status != 'unknown' and previous_status != new_status:
            self._send_status_change_alert(previous_status, new_status, parsed)

        logger.info(f"I-80 Sierra Status: {new_status} | "
                     f"EB={'CLOSED' if parsed['closed_eastbound'] else 'open'} "
                     f"WB={'CLOSED' if parsed['closed_westbound'] else 'open'} | "
                     f"{len(parsed['closures'])} closure(s), "
                     f"{len(parsed['chain_controls'])} chain control(s)")

    def _build_sms(self, headline, parsed):
        """Build an SMS-friendly alert under 153 chars. No emojis (they
        force UCS-2 encoding which drops the limit to 67 chars)."""
        eb = parsed.get('closed_eastbound', False)
        wb = parsed.get('closed_westbound', False)
        dirs = f"EB:{'CLOSED' if eb else 'open'} WB:{'CLOSED' if wb else 'open'}"

        # First closure reason (e.g. "zero visibility")
        reason = ''
        for c in parsed.get('closures', []):
            text = c.get('text', '')
            if ' - ' in text:
                reason = text.split(' - ', 1)[1].strip()[:40]
                break

        chains = 'Chains in effect. ' if parsed.get('chain_controls') else ''
        ts = datetime.now().strftime('%I:%M%p').lstrip('0')

        msg = f"I-80 SIERRA: {headline} | {dirs}"
        if reason:
            msg += f" | {reason}"
        if chains:
            msg += f" | {chains}"
        msg += f" ({ts})"

        return msg[:153]

    def _send_status_change_alert(self, old_status, new_status, parsed):
        headlines = {
            'open':              'OPEN - no closures',
            'chains_required':   'CHAINS REQUIRED',
            'closed_eastbound':  'CLOSED EASTBOUND',
            'closed_westbound':  'CLOSED WESTBOUND',
            'closed_both':       'CLOSED BOTH DIRS',
        }
        headline = headlines.get(new_status, new_status.upper())
        self.send_alert(self._build_sms(headline, parsed))

    def _build_test_alert(self):
        """Build a compact test alert using current road status."""
        return self._build_sms('TEST ALERT', current_road_status)

# ---------------------------------------------------------------------------
# Initialise
# ---------------------------------------------------------------------------
road_monitor = RoadMonitor()

def monitoring_loop():
    global monitoring_active
    while monitoring_active:
        try:
            road_monitor.check_road_conditions()
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}")
        time.sleep(Config.CHECK_INTERVAL)

# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == Config.SITE_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        error = 'Wrong password'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Routes — Main
# ---------------------------------------------------------------------------
@app.route('/')
@login_required
def index():
    return render_template('index.html',
                           status=current_road_status,
                           monitoring=monitoring_active,
                           check_interval=Config.CHECK_INTERVAL,
                           zapier_configured=bool(Config.ZAPIER_WEBHOOK_URL))

@app.route('/api/status')
def get_status():
    return jsonify(current_road_status)

# ---------------------------------------------------------------------------
# Routes — Monitoring controls
# ---------------------------------------------------------------------------
@app.route('/api/start_monitoring', methods=['POST'])
@login_required
def start_monitoring():
    global monitoring_active, monitoring_thread
    if monitoring_active:
        return jsonify({'status': 'error', 'message': 'Monitoring already active'})
    monitoring_active = True
    monitoring_thread = threading.Thread(target=monitoring_loop, daemon=True)
    monitoring_thread.start()
    logger.info("Monitoring started")
    return jsonify({'status': 'success', 'message': 'Monitoring started'})

@app.route('/api/stop_monitoring', methods=['POST'])
@login_required
def stop_monitoring():
    global monitoring_active
    monitoring_active = False
    logger.info("Monitoring stopped")
    return jsonify({'status': 'success', 'message': 'Monitoring stopped'})

@app.route('/api/check_now', methods=['POST'])
@login_required
def check_now():
    try:
        road_monitor.check_road_conditions()
        if current_road_status.get('status') == 'fetch_error':
            return jsonify({
                'status': 'error', 
                'message': 'Unable to fetch road conditions - Caltrans website may be unavailable',
                'data': current_road_status
            })
        return jsonify({'status': 'success', 'message': 'Check completed', 'data': current_road_status})
    except Exception as e:
        logger.error(f"Manual check failed: {e}")
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/test_alert', methods=['POST'])
@login_required
def test_alert():
    try:
        msg = road_monitor._build_test_alert()
        road_monitor.send_alert(msg)
        return jsonify({'status': 'success', 'message': 'Test alert sent'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

if __name__ == '__main__':
    try:
        road_monitor.check_road_conditions()
    except Exception as e:
        logger.error(f"Initial check failed: {e}")
    app.run(host='0.0.0.0', port=5001, debug=True)
