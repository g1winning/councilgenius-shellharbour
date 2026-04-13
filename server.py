#!/usr/bin/env python3
"""CouncilGenius Server — Shellharbour City Council — V9 Production"""
import os, json, re, csv, urllib.request, urllib.parse, time, hashlib
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file, Response

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

try:
    from shapely.geometry import Point, Polygon
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False
    print("[WARN] shapely not installed — bin zone lookup disabled. pip install shapely")

app = Flask(__name__)

# ── Configuration ──────────────────────────────────────────
COUNCIL_NAME = "Shellharbour City Council"
COUNCIL_DOMAIN = "shellharbour.nsw.gov.au"
MODEL = os.environ.get('MODEL', 'claude-sonnet-4-6')
MAX_TOKENS = int(os.environ.get('MAX_TOKENS', '1024'))
PORT = int(os.environ.get('PORT', '5000'))
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Fallback: read API key from file if env var not set
if not ANTHROPIC_API_KEY and os.path.exists('api_key.txt'):
    ANTHROPIC_API_KEY = open('api_key.txt').read().strip()

if not ANTHROPIC_API_KEY:
    print("[WARN] No ANTHROPIC_API_KEY set — set via environment variable or api_key.txt")

# Initialise Anthropic client
if Anthropic:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
else:
    client = None

# ── V9 Globals ────────────────────────────────────────────
PROMPT_VERSION = "1.0"
SERVER_START_TIME = time.time()
TOTAL_QUERIES = 0
KNOWLEDGE_HASH = ""
KNOWLEDGE_LINES = 0

# ── PII Filtering & JSONL Logging ──────────────────────────
PII_PATTERNS = {
    'email': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    'phone': r'\b(?:\+?61|0)(?:\s?[2-478])?(?:\s?\d{4}|\s?\d{2}\s?\d{2})(?:\s?\d{4})\b',
    'address': r'\b\d+\s+[A-Za-z]+(?:\s+[A-Za-z]+)*\s+(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Lane|Ln|Crescent|Cres|Way|Place|Pl)\b',
}

def filter_pii(text):
    """Filter PII from text using regex patterns."""
    filtered = text
    for pii_type, pattern in PII_PATTERNS.items():
        filtered = re.sub(pattern, f'[REDACTED_{pii_type.upper()}]', filtered, flags=re.IGNORECASE)
    return filtered

def log_query_basic(query, response, categories, classify_time_ms):
    """Log query to basic JSONL (truncated, PII-filtered)."""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "q": filter_pii(query)[:200],
            "cat": categories,
            "ms": round(classify_time_ms, 1)
        }
        with open("query_log_basic.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG_BASIC] Error: {e}")

def log_query_full(query, response, categories, system_prompt, raw_response, classify_time_ms):
    """Log query to full JSONL (PII-filtered, truncated response)."""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "q": filter_pii(query),
            "a": filter_pii(response)[:500],
            "cat": categories,
            "model": MODEL,
            "prompt_ver": PROMPT_VERSION
        }
        with open("query_log_full.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[LOG_FULL] Error: {e}")

# ── Bin Day Configuration ──────────────────────────────────
# Tier 0: KML zone polygons from shellharbourwaste.com.au
# Fallback to Tier 4 (URL referral) if shapely not available
BIN_ZONES_FILE = "bin_zones.json"
BIN_ZONES = []  # List of {name, day, group, polygon}
BIN_LOOKUP_MODE = 'none'  # Set after loading zones

def load_bin_zones():
    """Load pre-parsed bin collection zones from JSON."""
    global BIN_ZONES, BIN_LOOKUP_MODE
    if not SHAPELY_AVAILABLE:
        BIN_LOOKUP_MODE = 'none'
        print("[BIN] shapely not available — using Tier 4 (URL referral)")
        return
    if not os.path.exists(BIN_ZONES_FILE):
        BIN_LOOKUP_MODE = 'none'
        print(f"[BIN] {BIN_ZONES_FILE} not found — using Tier 4 (URL referral)")
        return
    try:
        with open(BIN_ZONES_FILE, 'r') as f:
            data = json.load(f)
        for zone in data.get('zones', []):
            coords = zone['coords']  # [[lng, lat], ...]
            polygon = Polygon(coords)
            BIN_ZONES.append({
                'name': zone['name'],
                'day': zone['day'],
                'group': zone['group'],
                'polygon': polygon
            })
        BIN_LOOKUP_MODE = 'geojson'
        print(f"[BIN] Loaded {len(BIN_ZONES)} zones — Tier 0 (KML polygon lookup)")
    except Exception as e:
        BIN_LOOKUP_MODE = 'none'
        print(f"[BIN] Error loading zones: {e} — using Tier 4 (URL referral)")

load_bin_zones()

def geocode_address(address):
    """Geocode an Australian address using Nominatim (OpenStreetMap, free)."""
    try:
        # Append Shellharbour NSW to improve geocoding accuracy
        query = address
        if 'shellharbour' not in query.lower() and 'nsw' not in query.lower():
            query += ', Shellharbour NSW'
        params = urllib.parse.urlencode({
            'q': query,
            'format': 'json',
            'limit': 1,
            'countrycodes': 'au'
        })
        url = f"https://nominatim.openstreetmap.org/search?{params}"
        req = urllib.request.Request(url, headers={
            'User-Agent': 'CouncilGenius/7.0 (council-chatbot)'
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            results = json.loads(resp.read())
        if results:
            return float(results[0]['lon']), float(results[0]['lat'])
    except Exception as e:
        print(f"[GEOCODE] Error: {e}")
    return None, None

def lookup_bin_zone(address):
    """Look up which bin collection zone an address falls in."""
    if not BIN_ZONES or BIN_LOOKUP_MODE != 'geojson':
        return None
    lng, lat = geocode_address(address)
    if lng is None:
        return None
    point = Point(lng, lat)
    for zone in BIN_ZONES:
        if zone['polygon'].contains(point):
            return {
                'zone': zone['name'],
                'day': zone['day'],
                'group': zone['group'],
                'address_geocoded': f"{lat:.6f}, {lng:.6f}"
            }
    # Point not in any zone — might be outside LGA
    return None

# ── Knowledge Base ─────────────────────────────────────────
KNOWLEDGE_FILE = "knowledge.txt"
KNOWLEDGE_BASE = ""

def load_knowledge():
    global KNOWLEDGE_BASE, KNOWLEDGE_HASH, KNOWLEDGE_LINES
    if os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE, 'r') as f:
            KNOWLEDGE_BASE = f.read()
        KNOWLEDGE_HASH = hashlib.sha256(KNOWLEDGE_BASE.encode()).hexdigest()
        KNOWLEDGE_LINES = len(KNOWLEDGE_BASE.split('\n'))
    print(f"[KNOWLEDGE] Loaded {len(KNOWLEDGE_BASE)} chars, {KNOWLEDGE_LINES} lines, hash={KNOWLEDGE_HASH[:8]}")

load_knowledge()

# ── Category Classification ───────────────────────────────
CATEGORIES = {
    'waste_bins': {
        'keywords': ['bin', 'collection', 'recycling', 'green waste', 'hard waste',
                     'rubbish', 'garbage', 'fogo', 'transfer station', 'tip', 'dump',
                     'landfill', 'waste', 'kerbside', 'bulk', 'mattress', 'dunmore',
                     'remondis', 'reviva'],
    },
    'rates': {
        'keywords': ['rates', 'payment', 'concession', 'rebate', 'due date',
                     'valuation', 'rate notice', 'instalment', 'bpay', 'direct debit',
                     'pensioner', 'rate peg'],
    },
    'planning': {
        'keywords': ['planning', 'permit', 'development', 'application', 'zone',
                     'heritage', 'overlay', 'building', 'construction', 'shed',
                     'extension', 'subdivision', 'da ', 'development application',
                     'complying development', 'cdc', 'planning portal', 'lep', 'dcp'],
    },
    'roads': {
        'keywords': ['pothole', 'road', 'maintenance', 'street light', 'graffiti',
                     'footpath', 'kerb', 'gutter', 'stormwater drain', 'flooding',
                     'road opening', 's138', 'driveway'],
    },
    'parking': {
        'keywords': ['parking', 'fine', 'ticket', 'infringement', 'meter'],
    },
    'pets': {
        'keywords': ['pet', 'dog', 'cat', 'animal', 'registration', 'microchip',
                     'barking', 'off-lead', 'off lead', 'dangerous dog', 'lost pet',
                     'found pet', 'adopt', 'desex'],
    },
    'property': {
        'keywords': ['property', 'land', 'certificate', '10.7', '149', 'intramaps',
                     'mapping', 'search'],
    },
    'family': {
        'keywords': ['kindergarten', 'kinder', 'childcare', 'maternal', 'immunisation',
                     'playgroup', 'family', 'children', 'youth'],
    },
    'community': {
        'keywords': ['library', 'pool', 'community', 'centre', 'program',
                     'recreation', 'swimming', 'sport', 'venue', 'hire', 'stadium',
                     'aqua', 'swim school', 'netball', 'civic centre', 'museum',
                     'imaginarium', 'gallery'],
    },
    'food_business': {
        'keywords': ['food', 'business', 'registration', 'supplier', 'tender',
                     'procurement', 'vendor', 'food safety', 'hair', 'beauty',
                     'skin penetration'],
    },
    'contact': {
        'keywords': ['phone', 'email', 'address', 'hours', 'contact', 'office',
                     'civic centre', 'justice of the peace', 'jp'],
    },
    'environment': {
        'keywords': ['stormwater', 'contaminated', 'environment', 'septic',
                     'wastewater', 'climate', 'bushfire', 'fire', 'emergency',
                     'coastal', 'lake illawarra', 'bushcare', 'landcare', 'solar',
                     'renewables', 'nursery', 'trees', 'wildlife', 'flood'],
    },
    'legal': {
        'keywords': ['appeal', 'complaint', 'legal', 'ombudsman', 'foi',
                     'freedom of information', 'privacy', 'whistleblower', 'gipa'],
    },
    'grants': {
        'keywords': ['grant', 'funding', 'support', 'community fund', 'financial assistance',
                     'sponsorship', 'donation'],
    },
    'local_laws': {
        'keywords': ['local law', 'bylaw', 'burning', 'burn off', 'camping',
                     'livestock', 'noise', 'neighbour dispute', 'good neighbour'],
    },
    'forms': {
        'keywords': ['form', 'application form', 'download', 'online form', 'portal',
                     'eservices'],
    },
    'tourism': {
        'keywords': ['visit', 'tourism', 'beach', 'pool', 'park', 'playground',
                     'shell cove', 'waterfront', 'marina', 'links', 'golf',
                     'airport', 'cycling', 'ebike', 'whats on', "what's on",
                     'event', 'beachside holiday'],
    },
    'employment': {
        'keywords': ['job', 'jobs', 'career', 'employment', 'work', 'cadetship',
                     'apprenticeship', 'traineeship', 'volunteer', 'work experience'],
    },
    'citizenship': {
        'keywords': ['citizenship', 'ceremony', 'citizen'],
    },
    'cemetery': {
        'keywords': ['cemetery', 'bereavement', 'burial', 'grave', 'memorial',
                     'albion park cemetery', 'shellharbour cemetery'],
    },
    'potential_api_abuse': {
        'keywords': ['api', 'endpoint', 'json', 'curl', 'hack', 'inject',
                     'sql', 'script', 'exploit', 'prompt'],
    },
    'off_topic': {
        'keywords': ['weather forecast', 'football score', 'recipe', 'joke', 'song lyrics',
                     'write me a poem', 'tell me a story'],
    },
}

def classify(text):
    """Classify user message into a service category."""
    text_lower = text.lower()
    scores = {}
    for cat, info in CATEGORIES.items():
        score = sum(1 for kw in info['keywords'] if kw in text_lower)
        scores[cat] = score
    top = max(scores, key=scores.get) if max(scores.values()) > 0 else 'general'
    return top

# ── Address Detection ──────────────────────────────────────
def detect_address(text):
    """Detect Australian street address including unit numbers."""
    pattern = r'\b(?:(?:Unit|Apt|Flat|Level|Suite)\s*\d+[A-Za-z]?\s*[/,]\s*)?(\d{1,5}\s+[A-Za-z][A-Za-z\s]{2,30}(?:Street|St|Road|Rd|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Court|Ct|Crescent|Cres|Place|Pl|Way|Lane|Ln|Parade|Pde|Circuit|Cct|Close|Cl|Grove|Gr|Terrace|Tce|Rise|Highway|Hwy|Strip|Esplanade|Esp|East|West|North|South|E|W|N|S)[\s,]*[A-Za-z\s]*)\b'
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0).strip().rstrip(',') if match else None

# ── Bin Day Logic (multi-turn aware) ───────────────────────
def check_bin_context(messages):
    """Scan conversation history for bin question + address across multiple turns."""
    has_bin_question = False
    address = None
    for msg in messages:
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            cat = classify(content)
            if cat == 'waste_bins':
                has_bin_question = True
            addr = detect_address(content)
            if addr:
                address = addr
    return has_bin_question, address

# ── System Prompt Builder ──────────────────────────────────
def build_system_prompt(category, bin_context=""):
    """Build system prompt with today's date and optional bin data."""
    today = date.today().strftime('%A %d %B %Y')
    prompt = KNOWLEDGE_BASE.replace('__CURRENT_DATE__', today)
    if bin_context:
        prompt += f"\n\n--- LIVE BIN DATA ---\n{bin_context}"
    return prompt

# ── Analytics Logging ──────────────────────────────────────
def log_analytics(category, session_id, message_preview):
    """Log chat analytics to CSV."""
    try:
        with open('analytics.csv', 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), category, session_id,
                           message_preview[:80]])
    except Exception:
        pass

# ── API Fallback (urllib) ──────────────────────────────────
def call_anthropic_urllib(system_prompt, messages):
    """Fallback API call using urllib if anthropic SDK not available."""
    import urllib.request
    api_body = json.dumps({
        'model': MODEL,
        'max_tokens': MAX_TOKENS,
        'system': system_prompt,
        'messages': messages
    }).encode()
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=api_body,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01'
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result['content'][0]['text']

# ── Routes ─────────────────────────────────────────────────

@app.route('/')
def index():
    """Serve the frontend page."""
    return send_file('page.html')

@app.route('/health')
def health():
    """Health check endpoint (V9)."""
    uptime_seconds = time.time() - SERVER_START_TIME
    return jsonify({
        'status': 'ok',
        'version': '9.0',
        'prompt_version': PROMPT_VERSION,
        'council': COUNCIL_NAME,
        'model': MODEL,
        'knowledge_loaded': len(KNOWLEDGE_BASE) > 0,
        'knowledge_chars': len(KNOWLEDGE_BASE),
        'knowledge_lines': KNOWLEDGE_LINES,
        'knowledge_hash': KNOWLEDGE_HASH[:16],
        'bin_mode': BIN_LOOKUP_MODE,
        'bin_zones': len(BIN_ZONES),
        'total_queries': TOTAL_QUERIES,
        'uptime_seconds': round(uptime_seconds, 1),
        'server_start_time': datetime.fromtimestamp(SERVER_START_TIME).isoformat()
    })

@app.route('/api/chat', methods=['POST', 'OPTIONS'])
def chat():
    """Main chat endpoint (V9)."""
    global TOTAL_QUERIES
    if request.method == 'OPTIONS':
        return _cors_preflight()

    data = request.json or {}
    messages = data.get('messages', [])
    session_id = data.get('session_id', 'unknown')

    if not messages:
        return _cors_json({'error': 'No messages provided'}, 400)

    user_message = messages[-1].get('content', '') if messages else ''
    classify_start = time.time()

    # Classify the message
    category = classify(user_message)
    classify_time_ms = (time.time() - classify_start) * 1000

    # Handle potential API abuse (blocking)
    if category == 'potential_api_abuse':
        log_query_basic(user_message, "[BLOCKED_ABUSE]", category, classify_time_ms)
        return _cors_json({
            'response': f"I'm the {COUNCIL_NAME} Community Assistant. I can help with council services like bins, rates, planning, and pets. What can I help you with?",
            'category': category
        })

    # Handle off_topic (logging-only, not blocking)
    if category == 'off_topic':
        log_query_basic(user_message, "[OFF_TOPIC]", category, classify_time_ms)
        # Continue to API call instead of blocking

    # Check bin context across conversation history
    bin_context = ""
    has_bin_q, address = check_bin_context(messages)
    if has_bin_q and address and BIN_LOOKUP_MODE == 'geojson':
        bin_data = lookup_bin_zone(address)
        if bin_data:
            bin_context = (
                f"[LIVE BIN COLLECTION DATA]\n"
                f"Address searched: {address}\n"
                f"Collection zone: {bin_data['zone']}\n"
                f"Collection day: {bin_data['day']}\n"
                f"Group: {bin_data['group']}\n"
                f"Bins collected every {bin_data['day']}.\n"
                f"Shellharbour has 3 bins: General Waste (red lid - weekly), "
                f"Recycling (yellow lid - fortnightly), FOGO (green lid - fortnightly).\n"
                f"General waste is collected every week. Recycling and FOGO alternate fortnightly.\n"
                f"The A/B group determines which fortnight recycling vs FOGO is collected.\n"
                f"For the exact schedule and which bin is out this week, direct to: "
                f"https://www.shellharbourwaste.com.au/find-my-bin-day/\n"
                f"For bin service issues, contact REMONDIS on 1300 121 344."
            )
        elif address:
            bin_context = (
                f"[BIN LOOKUP NOTE]\n"
                f"Address '{address}' could not be matched to a collection zone. "
                f"It may be outside the Shellharbour LGA or the address could not be geocoded.\n"
                f"Direct the resident to: https://www.shellharbourwaste.com.au/find-my-bin-day/"
            )

    # Build system prompt
    system_prompt = build_system_prompt(category, bin_context)

    # Call Anthropic API
    try:
        if client:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=messages
            )
            assistant_text = response.content[0].text
        else:
            assistant_text = call_anthropic_urllib(system_prompt, messages)

        # Increment query counter
        TOTAL_QUERIES += 1

        # Log analytics (CSV)
        log_analytics(category, session_id, user_message)

        # Log to dual JSONL formats
        log_query_basic(user_message, assistant_text, category, classify_time_ms)
        log_query_full(user_message, assistant_text, category, system_prompt, assistant_text, classify_time_ms)

        return _cors_json({
            'response': assistant_text,
            'category': category,
            'bin_info': bin_context if bin_context else None
        })

    except Exception as e:
        print(f"[ERROR] {e}")
        return _cors_json({
            'error': f"Sorry, I'm having trouble right now. Please try again or call {COUNCIL_NAME} on 02 4221 6111.",
            'category': category
        }, 500)

@app.route('/feedback', methods=['POST', 'OPTIONS'])
def feedback():
    """Feedback logging endpoint."""
    if request.method == 'OPTIONS':
        return _cors_preflight()

    data = request.json or {}
    try:
        with open('feedback.csv', 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                data.get('rating', ''),
                data.get('message_preview', '')[:80],
                data.get('session_id', 'unknown')
            ])
    except Exception:
        pass
    return _cors_json({'status': 'ok'})

@app.route('/knowledge.txt')
def serve_knowledge():
    """Serve knowledge base (for debugging)."""
    if os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE, 'r') as f:
            return Response(f.read(), mimetype='text/plain')
    return ('', 404)

# ── CORS Helpers ───────────────────────────────────────────
def _cors_preflight():
    """Handle CORS preflight requests."""
    resp = app.make_default_options_response()
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

def _cors_json(data, status=200):
    """Return JSON response with CORS headers."""
    resp = jsonify(data)
    resp.status_code = status
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# ── Startup ────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"[SERVER] {COUNCIL_NAME} CouncilGenius V9 — {MODEL} (prompt v{PROMPT_VERSION})")
    print(f"[SERVER] Knowledge: {len(KNOWLEDGE_BASE)} chars, {KNOWLEDGE_LINES} lines")
    print(f"[SERVER] Bin mode: {BIN_LOOKUP_MODE}")
    print(f"[SERVER] Dual JSONL logging: query_log_basic.jsonl, query_log_full.jsonl")
    print(f"[SERVER] Listening on :{PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
