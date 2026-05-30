"""
send_weekly_report.py

Generates and sends a weekly HTML email report to regional managers.
Reads current ratings from google_reviews_dashboard.html and
recent review activity from reviews_data.json.

Run locally:   python send_weekly_report.py
Run in CI:     GitHub Actions workflow calls this every Monday at 09:15 UTC.

Required GitHub Secrets:
  SMTP_HOST     — e.g. smtp.office365.com
  SMTP_PORT     — e.g. 587
  SMTP_USER     — sender email address
  SMTP_PASS     — sender password or app password
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent
HTML_PATH = ROOT / "google_reviews_dashboard.html"
REVIEWS_JSON_PATH = ROOT / "reviews_data.json"

RECIPIENTS = [
    "north@student-cribs.com",
    "northeast@student-cribs.com",
    "midlands@student-cribs.com",
    "south@student-cribs.com",
    "marketing@student-cribs.com",
]

TARGET_RATING = 4.5

REGION_NAMES = {
    "Region 1": "North East",
    "Region 2": "North",
    "Region 3": "Midlands",
    "Region 4": "South",
}


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def extract_properties_from_html() -> list:
    """Parse the const properties = [...] block from the dashboard HTML."""
    if not HTML_PATH.exists():
        return []
    html = HTML_PATH.read_text(encoding="utf-8")
    block = re.search(r"const properties\s*=\s*\[([\s\S]*?)\];", html)
    if not block:
        return []

    properties = []
    for m in re.finditer(
        r'name:\s*"([^"]+)".*?region:\s*"([^"]+)".*?score:\s*([0-9.]+|null).*?last:\s*([0-9.]+|null).*?reviews:\s*([0-9]+|null)',
        block.group(1),
        re.DOTALL,
    ):
        name, region, score, last, reviews = m.groups()
        properties.append({
            "name": name,
            "region": region,
            "score": float(score) if score != "null" else None,
            "last": float(last) if last != "null" else None,
            "reviews": int(reviews) if reviews != "null" else None,
        })
    return properties


def load_reviews_json() -> dict:
    if not REVIEWS_JSON_PATH.exists():
        return {}
    with open(REVIEWS_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_weekly_review_counts(reviews_data: dict) -> dict:
    """Returns {property_name: {"five_star": int, "one_star": int, "total": int}}"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    counts = {}
    for prop in reviews_data.get("properties", []):
        name = prop["name"]
        five = one = total = 0
        for r in prop.get("reviews", []):
            try:
                d = datetime.fromisoformat(r["raw_date"].replace("Z", "+00:00"))
            except Exception:
                continue
            if d >= cutoff:
                total += 1
                if r["stars"] == 5:
                    five += 1
                elif r["stars"] == 1:
                    one += 1
        counts[name] = {"five_star": five, "one_star": one, "total": total}
    return counts


# ---------------------------------------------------------------------------
# Email HTML builder
# ---------------------------------------------------------------------------

def build_email_html(properties: list, weekly_counts: dict, report_date: str) -> str:
    # Summary stats
    active = [p for p in properties if p["score"] is not None]
    if active:
        avg_rating = round(sum(p["score"] for p in active) / len(active), 2)
    else:
        avg_rating = None

    at_target = sum(1 for p in active if p["score"] >= TARGET_RATING)
    below_target = sum(1 for p in active if p["score"] < TARGET_RATING)
    total_five_week = sum(v["five_star"] for v in weekly_counts.values())
    total_one_week = sum(v["one_star"] for v in weekly_counts.values())

    # Group by region
    regions = {}
    for p in properties:
        regions.setdefault(p["region"], []).append(p)

    def score_color(s):
        if s is None:
            return "#6b7280"
        if s >= 4.5:
            return "#0e6640"
        if s >= 4.0:
            return "#c45c10"
        return "#e24b4a"

    def score_bg(s):
        if s is None:
            return "#f3f4f6"
        if s >= 4.5:
            return "#edfaf3"
        if s >= 4.0:
            return "#fff3ec"
        return "#fdf2f2"

    def change_html(score, last):
        if score is None or last is None:
            return '<span style="color:#9ca3af">—</span>'
        diff = round(score - last, 1)
        if diff > 0:
            return f'<span style="color:#0e6640">▲ +{diff:.1f}</span>'
        if diff < 0:
            return f'<span style="color:#e24b4a">▼ {diff:.1f}</span>'
        return '<span style="color:#9ca3af">—</span>'

    # Build region tables
    region_tables = ""
    for region_key in ["Region 1", "Region 2", "Region 3", "Region 4"]:
        props = regions.get(region_key, [])
        if not props:
            continue
        region_label = REGION_NAMES.get(region_key, region_key)
        rows = ""
        for p in props:
            wc = weekly_counts.get(p["name"], {"five_star": 0, "one_star": 0, "total": 0})
            score_disp = f"{p['score']:.1f}" if p["score"] is not None else "N/A"
            rows += f"""
            <tr>
              <td style="padding:10px 12px;font-size:13px;border-bottom:1px solid #f3f4f6;">{p['name']}</td>
              <td style="padding:10px 12px;text-align:center;border-bottom:1px solid #f3f4f6;">
                <span style="background:{score_bg(p['score'])};color:{score_color(p['score'])};
                  padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600;">
                  ★ {score_disp}
                </span>
              </td>
              <td style="padding:10px 12px;text-align:center;border-bottom:1px solid #f3f4f6;font-size:12px;">
                {change_html(p['score'], p['last'])}
              </td>
              <td style="padding:10px 12px;text-align:center;border-bottom:1px solid #f3f4f6;font-size:12px;">
                {p['reviews']:,}" if p['reviews'] is not None else "—"}
              </td>
              <td style="padding:10px 12px;text-align:center;border-bottom:1px solid #f3f4f6;font-size:12px;color:#0e6640;font-weight:600;">
                {"+" + str(wc['five_star']) if wc['five_star'] else "—"}
              </td>
              <td style="padding:10px 12px;text-align:center;border-bottom:1px solid #f3f4f6;font-size:12px;color:#e24b4a;font-weight:600;">
                {str(wc['one_star']) if wc['one_star'] else "—"}
              </td>
            </tr>"""

        region_tables += f"""
        <div style="margin-bottom:28px;">
          <div style="background:#101111;color:white;padding:10px 16px;border-radius:8px 8px 0 0;
            font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;">
            {region_label}
          </div>
          <table width="100%" cellpadding="0" cellspacing="0"
            style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;
            overflow:hidden;background:white;font-family:'Helvetica Neue',Arial,sans-serif;">
            <thead>
              <tr style="background:#f9fafb;border-bottom:1px solid #e5e7eb;">
                <th style="padding:8px 12px;text-align:left;font-size:10px;color:#6b7280;
                  letter-spacing:0.04em;text-transform:uppercase;font-weight:600;">Property</th>
                <th style="padding:8px 12px;text-align:center;font-size:10px;color:#6b7280;
                  letter-spacing:0.04em;text-transform:uppercase;font-weight:600;">Rating</th>
                <th style="padding:8px 12px;text-align:center;font-size:10px;color:#6b7280;
                  letter-spacing:0.04em;text-transform:uppercase;font-weight:600;">Change</th>
                <th style="padding:8px 12px;text-align:center;font-size:10px;color:#6b7280;
                  letter-spacing:0.04em;text-transform:uppercase;font-weight:600;">Total</th>
                <th style="padding:8px 12px;text-align:center;font-size:10px;color:#6b7280;
                  letter-spacing:0.04em;text-transform:uppercase;font-weight:600;">5★ this week</th>
                <th style="padding:8px 12px;text-align:center;font-size:10px;color:#6b7280;
                  letter-spacing:0.04em;text-transform:uppercase;font-weight:600;">1★ this week</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    avg_disp = f"{avg_rating:.2f}" if avg_rating else "N/A"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f5f5f3;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f3;padding:32px 16px;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

  <!-- Header -->
  <tr><td style="background:#101111;padding:24px 32px;border-radius:12px 12px 0 0;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td>
          <div style="color:white;font-size:18px;font-weight:700;letter-spacing:-0.01em;">
            reviews dashboard
          </div>
          <div style="color:rgba(255,255,255,0.5);font-size:12px;margin-top:3px;">
            Weekly Report · {report_date}
          </div>
        </td>
        <td align="right">
          <div style="background:#FFCC00;color:#101111;padding:6px 14px;
            border-radius:20px;font-size:12px;font-weight:700;display:inline-block;">
            Student Cribs
          </div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Summary pills -->
  <tr><td style="background:white;padding:24px 32px;border-bottom:1px solid #e5e7eb;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="text-align:center;padding:0 8px;">
          <div style="font-size:28px;font-weight:700;color:#18706E;letter-spacing:-0.03em;">{avg_disp}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:3px;">Portfolio avg</div>
        </td>
        <td style="text-align:center;padding:0 8px;border-left:1px solid #e5e7eb;">
          <div style="font-size:28px;font-weight:700;color:#18706E;letter-spacing:-0.03em;">{at_target}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:3px;">At target (≥{TARGET_RATING}★)</div>
        </td>
        <td style="text-align:center;padding:0 8px;border-left:1px solid #e5e7eb;">
          <div style="font-size:28px;font-weight:700;color:#e24b4a;letter-spacing:-0.03em;">{below_target}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:3px;">Need improvement</div>
        </td>
        <td style="text-align:center;padding:0 8px;border-left:1px solid #e5e7eb;">
          <div style="font-size:28px;font-weight:700;color:#0e6640;letter-spacing:-0.03em;">+{total_five_week}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:3px;">5★ this week</div>
        </td>
        <td style="text-align:center;padding:0 8px;border-left:1px solid #e5e7eb;">
          <div style="font-size:28px;font-weight:700;color:#e24b4a;letter-spacing:-0.03em;">{total_one_week}</div>
          <div style="font-size:11px;color:#6b7280;margin-top:3px;">1★ this week</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Region tables -->
  <tr><td style="background:#f5f5f3;padding:24px 32px;">
    {region_tables}

    <!-- Footer -->
    <div style="text-align:center;font-size:11px;color:#9ca3af;margin-top:8px;">
      Data updated every Monday at 08:00 UTC ·
      <a href="https://kane-r94.github.io/google-reviews-dashboard/google_reviews_dashboard.html"
        style="color:#18706E;text-decoration:none;">View full dashboard</a>
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Send email
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    if not all([host, user, password]):
        print("SMTP credentials not configured — skipping email send.")
        print("Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS environment variables.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Student Cribs Reviews <{user}>"
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(html_body, "html"))

    print(f"Connecting to {host}:{port}...")
    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.sendmail(user, RECIPIENTS, msg.as_string())
    print(f"Email sent to {len(RECIPIENTS)} recipients.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Weekly Report Sender ===\n")
    report_date = datetime.now(timezone.utc).strftime("%d %b %Y")

    print("Loading property ratings from HTML...")
    properties = extract_properties_from_html()
    print(f"  {len(properties)} properties loaded")

    print("Loading review activity from reviews_data.json...")
    reviews_data = load_reviews_json()
    weekly_counts = get_weekly_review_counts(reviews_data)
    total_new = sum(v["total"] for v in weekly_counts.values())
    print(f"  {total_new} reviews in the last 7 days")

    print("Building email...")
    html_body = build_email_html(properties, weekly_counts, report_date)
    subject = f"Student Cribs Reviews — Weekly Update {report_date}"

    send_email(subject, html_body)
    print("\nDone!")


if __name__ == "__main__":
    main()
