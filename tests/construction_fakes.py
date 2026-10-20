"""Synthetic job-site weather for the construction tests (no network)."""
from datetime import datetime, timedelta, timezone

from rainout_agent import rain, weather

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)  # Wed 7:00 AM CDT
BASE = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
GEO = {"austin": [{"name": "Austin", "admin1": "Texas", "country": "United States", "country_code": "US",
                   "latitude": 30.26715, "longitude": -97.74306, "timezone": "America/Chicago", "population": 961855}]}


def local(s):
    """'2026-10-22 07:00' CDT -> UTC datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=timezone(timedelta(hours=-5))).astimezone(timezone.utc)


class SiteFake:
    """hour(t) -> dict overrides for the hour [t, t+1) in UTC. Defaults: dry, 65F, RH 60, wind 5, gust 10."""

    def __init__(self, hour=None, nws=False):
        self.hour = hour or (lambda t: {})
        self.nws = nws
        self.calls = []

    def row(self, t):
        r = {"temp": 65.0, "rh": 60, "dew": 50.0, "wind": 5.0, "gust": 10.0, "mm": 0.0, "pop": 0, "code": 1}
        r.update(self.hour(t))
        return r

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if url.startswith(rain.GEOCODE_URL):
            return {"results": GEO.get(url.split("name=")[1].split("&")[0].lower(), [])}
        if url.startswith(weather.OPEN_METEO_URL):
            ts = [BASE + timedelta(hours=i) for i in range(24 * 7)]
            rows = [self.row(t) for t in ts]
            # precipitation at T is the sum of the hour before T
            mm = [0.0] + [self.row(t)["mm"] for t in ts[:-1]]
            return {"timezone": "America/Chicago", "utc_offset_seconds": -18000, "hourly": {
                "time": [int(t.timestamp()) for t in ts],
                "temperature_2m": [r["temp"] for r in rows], "relative_humidity_2m": [r["rh"] for r in rows],
                "dew_point_2m": [r["dew"] for r in rows], "wind_speed_10m": [r["wind"] for r in rows],
                "wind_gusts_10m": [r["gust"] for r in rows], "precipitation": mm,
                "precipitation_probability": [r["pop"] for r in rows], "weather_code": [r["code"] for r in rows]}}
        if url.startswith("https://api.weather.gov/"):
            if not self.nws:
                raise TimeoutError("nws off in this test")
            if "/points/" in url:
                return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/EWX/1,1/forecast/hourly"}}
            return {"properties": {"periods": [
                {"startTime": (BASE + timedelta(hours=i)).isoformat(), "endTime": (BASE + timedelta(hours=i + 1)).isoformat(),
                 "probabilityOfPrecipitation": {"value": self.nws(BASE + timedelta(hours=i))},
                 "shortForecast": "Chance Showers And Thunderstorms" if self.nws(BASE + timedelta(hours=i)) >= 90 else "Sunny"}
                for i in range(156)]}}
        raise AssertionError(url)
