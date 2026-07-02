import requests
import time
import json
import os
from datetime import datetime, timedelta


class StravaRateTracker:
    """Tracks Strava API rate limits and auto-pauses when needed."""

    def __init__(self, state_file="strava_rate_state.json"):
        self.state_file = state_file
        self.load_state()

    def load_state(self):
        """Load persisted usage from disk so restarts don't lose daily count."""
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                state = json.load(f)
            today = str(datetime.utcnow().date())
            # Reset daily count if it's a new day
            if state.get("date") != today:
                state["usage_day"] = 0
                state["date"] = today
            self.usage_15 = state.get("usage_15", 0)
            self.usage_day = state.get("usage_day", 0)
            self.limit_15 = state.get("limit_15", 200)
            self.limit_day = state.get("limit_day", 2000)
        else:
            self.usage_15 = 0
            self.usage_day = 0
            self.limit_15 = 200
            self.limit_day = 2000

    def save_state(self):
        """Persist current usage to disk."""
        with open(self.state_file, "w") as f:
            json.dump({
                "usage_15": self.usage_15,
                "usage_day": self.usage_day,
                "limit_15": self.limit_15,
                "limit_day": self.limit_day,
                "date": str(datetime.utcnow().date()),
            }, f)

    def update_from_response(self, resp):
        """Parse rate-limit headers from a Strava response."""
        if 'X-RateLimit-Usage' in resp.headers:
            u15, uday = resp.headers['X-RateLimit-Usage'].split(',')
            self.usage_15 = int(u15)
            self.usage_day = int(uday)
        if 'X-RateLimit-Limit' in resp.headers:
            l15, lday = resp.headers['X-RateLimit-Limit'].split(',')
            self.limit_15 = int(l15)
            self.limit_day = int(lday)
        self.save_state()

    def wait_if_needed(self):
        """Block until we're safe to make another request."""
        # --- 15-minute bucket: boundaries at :00, :15, :30, :45 ---
        if self.usage_15 >= self.limit_15:
            now = datetime.utcnow()
            next_boundary = now + timedelta(minutes=15 - now.minute % 15)
            next_boundary = next_boundary.replace(second=0, microsecond=0)
            sleep_sec = (next_boundary - now).total_seconds()
            if sleep_sec > 0:
                print(f"[RateLimit] 15-min limit hit. Sleeping {sleep_sec:.0f}s until {next_boundary}")
                time.sleep(sleep_sec)

        # --- Daily bucket: resets at midnight UTC ---
        if self.usage_day >= self.limit_day:
            tomorrow = (datetime.utcnow() + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            sleep_sec = (tomorrow - datetime.utcnow()).total_seconds()
            print(f"[RateLimit] Daily limit hit. Sleeping until midnight UTC ({sleep_sec:.0f}s)")
            time.sleep(sleep_sec)

    def request(self, method, url, **kwargs):
        """Make an API call with automatic rate-limit handling."""
        self.wait_if_needed()
        resp = requests.request(method, url, **kwargs)
        self.update_from_response(resp)

        # Handle 429 (rate-limited despite our tracking)
        if resp.status_code == 429:
            wait = int(resp.headers.get('Retry-After', 60))
            print(f"[RateLimit] 429! Retrying after {wait}s")
            time.sleep(wait)
            resp = requests.request(method, url, **kwargs)
            self.update_from_response(resp)

        return resp

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)


# =============================================================================
# Example usage
# =============================================================================
if __name__ == "__main__":
    # Replace with your actual Strava config
    ACCESS_TOKEN = "your_access_token_here"
    BASE_URL = "https://www.strava.com/api/v3"

    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    tracker = StravaRateTracker()

    # Make a call — the tracker handles pausing automatically
    resp = tracker.get(f"{BASE_URL}/athlete/activities", headers=headers, params={"per_page": 30})
    print(f"Status: {resp.status_code}")
    print(f"Usage: {tracker.usage_15}/{tracker.limit_15} (15min)  {tracker.usage_day}/{tracker.limit_day} (day)")

    if resp.status_code == 200:
        activities = resp.json()
        for a in activities[:3]:
            print(f"  - {a['name']} ({a['type']}, {a['distance']}m)")