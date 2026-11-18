import requests
import time
import json
import os
from datetime import datetime, timedelta, timezone


class StravaRateTracker:
    """Tracks Strava API rate limits and auto-pauses when needed."""

    def __init__(self, state_file="strava_rate_state.json"):
        self.state_file = state_file
        self.load_state()

    def load_state(self):
        """Load persisted usage from disk so restarts don't lose daily count."""
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        window_15 = self._window_15(now)
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                state = json.load(f)
            if state.get("date") != today:
                state["usage_day"] = 0
                state["date"] = today
            self.usage_15 = state.get("usage_15", 0)
            self.usage_day = state.get("usage_day", 0)
            self.limit_15 = state.get("limit_15", 200)
            self.limit_day = state.get("limit_day", 2000)
            if state.get("window_15") != window_15:
                self.usage_15 = 0
        else:
            self.usage_15 = 0
            self.usage_day = 0
            self.limit_15 = 200
            self.limit_day = 2000

        self.date = today
        self.window_15 = window_15

    @staticmethod
    def _window_15(now):
        return (
            f"{now.date().isoformat()}-{now.hour:02d}-"
            f"{now.minute // 15 * 15:02d}"
        )

    def save_state(self):
        """Persist current usage to disk."""
        with open(self.state_file, "w") as f:
            json.dump({
                "usage_15": self.usage_15,
                "usage_day": self.usage_day,
                "limit_15": self.limit_15,
                "limit_day": self.limit_day,
                "date": self.date,
                "window_15": self.window_15,
            }, f)

    def update_from_response(self, resp):
        """Parse rate-limit headers from a Strava response."""
        if 'X-RateLimit-Usage' in resp.headers:
            u15, uday = resp.headers['X-RateLimit-Usage'].split(',', maxsplit=1)
            self.usage_15 = int(u15)
            self.usage_day = int(uday)
        if 'X-RateLimit-Limit' in resp.headers:
            l15, lday = resp.headers['X-RateLimit-Limit'].split(',', maxsplit=1)
            self.limit_15 = int(l15)
            self.limit_day = int(lday)
        now = datetime.now(timezone.utc)
        self.date = now.date().isoformat()
        self.window_15 = self._window_15(now)
        self.save_state()

    def _reset_expired_windows(self, now):
        today = now.date().isoformat()
        window_15 = self._window_15(now)
        changed = False

        if self.date != today:
            self.usage_day = 0
            self.date = today
            changed = True
        if self.window_15 != window_15:
            self.usage_15 = 0
            self.window_15 = window_15
            changed = True
        if changed:
            self.save_state()

    def wait_if_needed(self):
        """Block until we're safe to make another request."""
        now = datetime.now(timezone.utc)
        self._reset_expired_windows(now)
        reset_times = []

        if self.usage_15 >= self.limit_15:
            minutes_to_next = 15 - now.minute % 15
            next_boundary = (now + timedelta(minutes=minutes_to_next)).replace(
                second=0, microsecond=0
            )
            reset_times.append(next_boundary)
        if self.usage_day >= self.limit_day:
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            reset_times.append(tomorrow)

        if reset_times:
            reset_at = max(reset_times)
            sleep_sec = max(0, (reset_at - now).total_seconds())
            print(f"[RateLimit] Limit reached. Sleeping {sleep_sec:.0f}s until {reset_at}")
            time.sleep(sleep_sec)
            self._reset_expired_windows(datetime.now(timezone.utc))

    def request(self, method, url, **kwargs):
        """Make an API call with automatic rate-limit handling."""
        for attempt in range(4):
            self.wait_if_needed()
            resp = requests.request(method, url, **kwargs)
            self.update_from_response(resp)

            if resp.status_code != 429:
                resp.raise_for_status()
                return resp

            if attempt == 3:
                resp.raise_for_status()

            try:
                wait = int(resp.headers.get('Retry-After', 60))
            except ValueError:
                wait = 60
            print(f"[RateLimit] 429! Retrying after {wait}s")
            time.sleep(wait)

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