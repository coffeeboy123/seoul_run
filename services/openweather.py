# services/openweather.py
import os
from typing import Dict, Any
import httpx
from dotenv import load_dotenv

load_dotenv()
OW_API_KEY = os.getenv("OPENWEATHER_API_KEY")
BASE = "https://api.openweathermap.org/data/2.5"

class OpenWeatherClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or OW_API_KEY
        if not self.api_key:
            raise RuntimeError("OPENWEATHER_API_KEY 가 설정되지 않았습니다 (.env 확인).")

    async def current_and_forecast(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        현재 날씨 + 5일/3시간 간격 예보
        """
        async with httpx.AsyncClient(timeout=10) as client:
            cur = await client.get(
                f"{BASE}/weather",
                params={"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric", "lang": "kr"},
            )
            cur.raise_for_status()

            fc = await client.get(
                f"{BASE}/forecast",
                params={"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric", "lang": "kr"},
            )
            fc.raise_for_status()

        return {"current": cur.json(), "forecast": fc.json()}

    async def air_quality(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        대기질 (PM2.5/PM10, AQI)
        """
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{BASE}/air_pollution",
                params={"lat": lat, "lon": lon, "appid": self.api_key},
            )
            res.raise_for_status()
            return res.json()
