import requests
import time
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime


class StravaRateLimitError(requests.exceptions.HTTPError):
    """Raised when Strava responds with HTTP 429; requests are not retried."""


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
                state["read_usage_day"] = 0
                state["date"] = today
            self.usage_15 = state.get("usage_15", 0)
            self.usage_day = state.get("usage_day", 0)
            self.limit_15 = state.get("limit_15", 200)
            self.limit_day = state.get("limit_day", 2000)
            self.read_usage_15 = state.get("read_usage_15", 0)
            self.read_usage_day = state.get("read_usage_day", 0)
            self.read_limit_15 = state.get("read_limit_15", 100)
            self.read_limit_day = state.get("read_limit_day", 1000)
            self.retry_after_until = state.get("retry_after_until")
            if state.get("window_15") != window_15:
                self.usage_15 = 0
                self.read_usage_15 = 0
        else:
            self.usage_15 = 0
            self.usage_day = 0
            self.limit_15 = 200
            self.limit_day = 2000
            self.read_usage_15 = 0
            self.read_usage_day = 0
            self.read_limit_15 = 100
            self.read_limit_day = 1000
            self.retry_after_until = None

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
                "read_usage_15": self.read_usage_15,
                "read_usage_day": self.read_usage_day,
                "read_limit_15": self.read_limit_15,
                "read_limit_day": self.read_limit_day,
                "retry_after_until": self.retry_after_until,
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
        if 'X-ReadRateLimit-Usage' in resp.headers:
            read_u15, read_uday = resp.headers['X-ReadRateLimit-Usage'].split(',', maxsplit=1)
            self.read_usage_15 = int(read_u15)
            self.read_usage_day = int(read_uday)
        else:
            self.read_usage_15 = self.usage_15
            self.read_usage_day = self.usage_day
        if 'X-ReadRateLimit-Limit' in resp.headers:
            read_l15, read_lday = resp.headers['X-ReadRateLimit-Limit'].split(',', maxsplit=1)
            self.read_limit_15 = int(read_l15)
            self.read_limit_day = int(read_lday)
        else:
            self.read_limit_15 = min(self.read_limit_15, self.limit_15)
            self.read_limit_day = min(self.read_limit_day, self.limit_day)
        now = datetime.now(timezone.utc)
        self.date = now.date().isoformat()
        self.window_15 = self._window_15(now)
        self.save_state()

    def _reset_expired_windows(self, now):
        today = now.date().isoformat()
        window_15 = self._window_15(now)
        changed = False

        if self.retry_after_until is not None:
            retry_at = datetime.fromtimestamp(
                self.retry_after_until, tz=timezone.utc
            )
            if retry_at <= now:
                self.retry_after_until = None
                changed = True

        if self.date != today:
            self.usage_day = 0
            self.read_usage_day = 0
            self.date = today
            changed = True
        if self.window_15 != window_15:
            self.usage_15 = 0
            self.read_usage_15 = 0
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
        if self.read_usage_15 >= self.read_limit_15:
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
        if self.read_usage_day >= self.read_limit_day:
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            reset_times.append(tomorrow)
        if self.retry_after_until is not None:
            retry_at = datetime.fromtimestamp(
                self.retry_after_until, tz=timezone.utc
            )
            if retry_at > now:
                reset_times.append(retry_at)

        if reset_times:
            reset_at = max(reset_times)
            sleep_sec = max(0, (reset_at - now).total_seconds())
            print(f"[RateLimit] Limit reached. Sleeping {sleep_sec:.0f}s until {reset_at}")
            time.sleep(sleep_sec)
            self._reset_expired_windows(datetime.now(timezone.utc))

    def request(self, method, url, **kwargs):
        """Make one API request, pausing for known quotas and never retrying 429s."""
        self.wait_if_needed()
        resp = requests.request(method, url, **kwargs)
        self.update_from_response(resp)

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    retry_after_seconds = float(retry_after)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                    except (TypeError, ValueError):
                        retry_at = None
                    if retry_at is not None:
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        retry_after_until = retry_at.timestamp()
                    else:
                        retry_after_until = None
                else:
                    retry_after_until = time.time() + max(0.0, retry_after_seconds)
                if retry_after_until is not None:
                    self.retry_after_until = max(
                        self.retry_after_until or 0, retry_after_until
                    )
                    self.save_state()
            raise StravaRateLimitError(
                "Strava returned HTTP 429; stopping API requests for this execution.",
                response=resp,
            )

        resp.raise_for_status()
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