import os
import json
import hmac
import hashlib
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import requests

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SPREADSHEET_ID      = os.environ["SPREADSHEET_ID"]
SHEET_NAME          = os.environ.get("SHEET_NAME", "By Category (Clean)")
ADMIN_USER_IDS      = [u.strip() for u in os.environ.get("ADMIN_USER_IDS", "").split(",") if u.strip()]

# ── Slack request verification ─────────────────────────────────────────────────
def _verify_slack(headers, raw_body: bytes) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if abs(time.time() - int(ts)) > 60 * 5:
        return False
    base = f"v0:{ts}:{raw_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)

# ── Google Sheets ──────────────────────────────────────────────────────────────
def _fetch_sheet():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!A:E")
        .execute()
    )
    raw = result.get("values", [])
    if not raw:
        return []
    headers = [h.strip().lower().replace(" ", "_") for h in raw[0]]
    rows = []
    for r in raw[1:]:
        while len(r) < 5:
            r.append("")
        row = dict(zip(headers, [c.strip() for c in r[:5]]))
        row.setdefault("category", "")
        row.setdefault("subcategory", "")
        row.setdefault("product", "")
        row.setdefault("lead_time", "")
        row.setdefault("min_quantity", "TBD")
        if not row["min_quantity"]:
            row["min_quantity"] = "TBD"
        if row["category"] or row["product"]:
            rows.append(row)
    return rows

# ── Slack response helpers ─────────────────────────────────────────────────────
def _post_response(response_url: str, blocks: list, text: str):
    requests.post(response_url, json={
        "response_type": "in_channel",
        "text": text,
        "blocks": blocks,
    }, timeout=10)

def _header_block(text):
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}

def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

def _divider():
    return {"type": "divider"}

# ── Query helpers ──────────────────────────────────────────────────────────────
def _all_categories(rows):
    seen, cats = set(), []
    for r in rows:
        c = r["category"]
        if c and c not in seen:
            seen.add(c)
            cats.append(c)
    return cats

def _rows_for_product(rows, query):
    q = query.lower().strip()
    return [r for r in rows if r["product"].lower() == q]

def _rows_for_category(rows, query):
    q = query.lower().strip()
    return [r for r in rows if r["category"].lower() == q]

def _fuzzy_search(rows, query):
    words = query.lower().strip().split()
    matches, seen = [], set()
    for r in rows:
        if all(w in r["product"].lower() for w in words):
            key = (r["category"], r["product"])
            if key not in seen:
                seen.add(key)
                matches.append(r)
    return matches

# ── Block builders ─────────────────────────────────────────────────────────────
def _product_blocks(product_name, rows):
    if not rows:
        return [_section(f"❌ No product found matching *{product_name}*.\nTry `/bakery search {product_name}` to find similar items.")]
    sub_label = f"  ›  {rows[0]['subcategory']}" if rows[0]["subcategory"] else ""
    blocks = [
        _header_block(f"🎂 {rows[0]['product']}"),
        _section(f"*Category:* {rows[0]['category']}{sub_label}"),
        _divider(),
    ]
    lines = [f"• `{r['lead_time']}`  →  {r['min_quantity']}" for r in rows]
    blocks.append(_section("*Lead Time*  |  *Min Quantity*\n" + "\n".join(lines)))
    return blocks

def _category_blocks(category_name, rows):
    if not rows:
        return [_section(f"❌ No category found matching *{category_name}*.\nRun `/bakery list` to see all categories.")]
    blocks = [_header_block(f"📦 {category_name}"), _divider()]
    grouped = {}
    for r in rows:
        sub = r["subcategory"] or "_none_"
        grouped.setdefault(sub, {}).setdefault(r["product"], []).append(r)
    for sub, products in grouped.items():
        if sub != "_none_":
            blocks.append(_section(f"*── {sub} ──*"))
        for prod, prod_rows in products.items():
            lines = [f"*{prod}*"] + [f"  • `{r['lead_time']}`  →  Min Qty: {r['min_quantity']}" for r in prod_rows]
            blocks.append(_section("\n".join(lines)))
    return blocks

def _list_blocks(categories):
    if not categories:
        return [_section("No data found. An admin needs to run `/bakery sync` first.")]
    return [
        _header_block("📋 All Categories"),
        _section("\n".join([f"• {c}" for c in categories])),
        _section("_Use `/bakery category [name]` to see products in a category._"),
    ]

def _search_blocks(query, matches):
    if not matches:
        return [_section(f"🔍 No products found for *\"{query}\"*.\nTry a shorter keyword or run `/bakery list` for categories.")]
    blocks = [_header_block(f"🔍 Search: \"{query}\""), _divider()]
    seen = {}
    for r in matches:
        seen.setdefault((r["category"], r["product"]), []).append(r)
    for (cat, prod), rows in seen.items():
        lines = [f"*{prod}*  _({cat})_"] + [f"  • `{r['lead_time']}`  →  Min Qty: {r['min_quantity']}" for r in rows]
        blocks.append(_section("\n".join(lines)))
    blocks.append(_section(f"_{len(seen)} product(s) found. Use `/bakery product [exact name]` for full details._"))
    return blocks

def _help_blocks():
    return [
        _header_block("🍰 Bakery Bot — Commands"),
        _section(
            "*`/bakery product [name]`*\nLead times & min quantities for a specific product.\n"
            "_Example: `/bakery product Bulk Logo Cupcakes`_\n\n"
            "*`/bakery category [name]`*\nAll products in a category.\n"
            "_Example: `/bakery category Cookies`_\n\n"
            "*`/bakery list`*\nAll available categories.\n\n"
            "*`/bakery search [keyword]`*\nSearch across all products.\n"
            "_Example: `/bakery search logo oreo`_\n\n"
            "*`/bakery sync`*\nRefresh data from Google Sheets. _(Admins only)_"
        ),
    ]

# ── Main handler ───────────────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        # Verify request is genuinely from Slack
        if not _verify_slack(self.headers, raw_body):
            self._respond(403, "Forbidden")
            return

        # Acknowledge Slack immediately (must reply within 3 seconds)
        self._respond(200, json.dumps({"response_type": "in_channel", "text": "⏳ Working on it..."}),
                      content_type="application/json")

        params = parse_qs(raw_body.decode("utf-8"))
        get = lambda k: params.get(k, [""])[0].strip()

        text         = get("text")
        user_id      = get("user_id")
        response_url = get("response_url")
        parts        = text.split(None, 1)
        sub          = parts[0].lower() if parts else ""
        arg          = parts[1].strip() if len(parts) > 1 else ""

        try:
            self._dispatch(sub, arg, user_id, response_url)
        except Exception as e:
            _post_response(response_url, [_section(f"❌ Something went wrong: {e}")], "Error")

    def _dispatch(self, sub, arg, user_id, response_url):
        # ── sync ──────────────────────────────────────────────────────────────
        if sub == "sync":
            if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
                _post_response(response_url, [_section("🔒 Only admins can run `/bakery sync`.")], "Forbidden")
                return
            rows = _fetch_sheet()
            from datetime import datetime
            ts = datetime.now().strftime("%b %d, %Y at %I:%M %p")
            # Store in Vercel KV if available, otherwise confirm inline
            # For simplicity we confirm the count — data is fetched fresh on every command
            _post_response(response_url,
                [_section(f"✅ Connected! Google Sheet has *{len(rows)} rows* available.\nLast checked: {ts}")],
                "Sync complete")
            return

        # All data-dependent commands fetch fresh from sheet each time
        rows = _fetch_sheet()

        # ── list ──────────────────────────────────────────────────────────────
        if sub == "list":
            cats = _all_categories(rows)
            _post_response(response_url, _list_blocks(cats), "All categories")
            return

        # ── product ───────────────────────────────────────────────────────────
        if sub == "product":
            if not arg:
                _post_response(response_url, [_section("Usage: `/bakery product [product name]`")], "Usage")
                return
            matched = _rows_for_product(rows, arg)
            if not matched:
                fuzzy = _fuzzy_search(rows, arg)
                if fuzzy:
                    blocks = [_section(f"_No exact match for \"{arg}\" — showing search results:_")] + _search_blocks(arg, fuzzy)
                    _post_response(response_url, blocks, f"Search results for {arg}")
                else:
                    _post_response(response_url, _product_blocks(arg, []), "Not found")
            else:
                _post_response(response_url, _product_blocks(arg, matched), matched[0]["product"])
            return

        # ── category ──────────────────────────────────────────────────────────
        if sub == "category":
            if not arg:
                _post_response(response_url, [_section("Usage: `/bakery category [category name]`")], "Usage")
                return
            matched = _rows_for_category(rows, arg)
            _post_response(response_url, _category_blocks(arg, matched), f"Category: {arg}")
            return

        # ── search ────────────────────────────────────────────────────────────
        if sub == "search":
            if not arg:
                _post_response(response_url, [_section("Usage: `/bakery search [keyword]`")], "Usage")
                return
            matches = _fuzzy_search(rows, arg)
            _post_response(response_url, _search_blocks(arg, matches), f"Search: {arg}")
            return

        # ── help / fallback ───────────────────────────────────────────────────
        _post_response(response_url, _help_blocks(), "Bakery Bot Help")

    def _respond(self, status, body, content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def log_message(self, *args):
        pass  # suppress default request logging
