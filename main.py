# main.py  — fast, cached version
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal, List, Tuple, Optional
from pathlib import Path
from math import radians, sin, cos, asin, sqrt
import json
import time
import asyncio

# --- services (your existing modules) ---
from services.openweather import OpenWeatherClient
from services.scoring import running_score
from services.kakao import KakaoLocalClient, keywords_for_terrain
from services.gpt_reason import build_reason_korean
from services.overpass import (
    fetch_paths_near,
    crop_out_and_back,
    nearest_index_on_path,
    path_len_km,
)

# -----------------------------------------------------------------------------
# App / CORS
# -----------------------------------------------------------------------------
app = FastAPI(title="Seoul Running – API (cached)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Globals
# -----------------------------------------------------------------------------
SEOUL_COORDS = json.loads(Path("data/seoul_gu_coords.json").read_text(encoding="utf-8"))
OW = OpenWeatherClient()
KAKAO = KakaoLocalClient()


# -----------------------------------------------------------------------------
# Small TTL cache (in-memory)
# -----------------------------------------------------------------------------
class TTLCache:
    def __init__(self, ttl_sec: int = 600):
        self.ttl = ttl_sec
        self.store: dict[str, tuple[object, float]] = {}

    def get(self, key: str):
        v = self.store.get(key)
        if not v:
            return None
        data, exp = v
        if time.time() > exp:
            self.store.pop(key, None)
            return None
        return data

    def set(self, key: str, data: object):
        self.store[key] = (data, time.time() + self.ttl)


CACHE_WEATHER = TTLCache(ttl_sec=600)   # 10 min
CACHE_CAND = TTLCache(ttl_sec=900)      # 15 min
CACHE_ROUTE = TTLCache(ttl_sec=300)     # 5  min


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class UserInput(BaseModel):
    gu: str = Field(..., description="예: 마포구")
    hour: int = Field(..., ge=0, le=23, description="뛰고 싶은 시작 시각(0~23)")
    terrain: Literal["평지", "경사"] = Field(..., description="선호 지형")


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _extract_latlon(raw) -> Tuple[float, float]:
    if isinstance(raw, dict):
        return float(raw["lat"]), float(raw["lon"])
    lat, lon = raw
    return float(lat), float(lon)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _normalize(v: float, vmin: float, vmax: float) -> float:
    if vmax <= vmin:
        return 0.0
    return max(0.0, min(1.0, (v - vmin) / (vmax - vmin)))


def _signal_score_by_name(name: str) -> float:
    n = (name or "").lower()
    if any(k in n for k in ["한강", "천", "수변", "강변", "탄천"]):
        return 1.0
    if "공원" in n:
        return 0.75
    if any(k in n for k in ["둘레길", "산책로", "숲", "산"]):
        return 0.7
    return 0.4


def hilliness_by_name(name: str) -> float:
    n = (name or "").lower()
    if any(k in n for k in ["둘레길", "산책로", "능선", "봉", "정상", "숲", "산"]):
        return 1.0
    if "계단" in n:
        return 0.8
    if any(k in n for k in ["한강", "천", "탄천", "강변", "수변"]):
        return 0.2
    return 0.5


def estimate_loop_km(spot_name: str, terrain: str) -> float:
    n = (spot_name or "").lower()
    if any(k in n for k in ["한강", "천", "탄천", "강변", "수변"]):
        return 5.0
    if any(k in n for k in ["둘레길", "산책로", "숲", "산"]):
        return 4.0 if terrain == "경사" else 3.5
    return 3.0


# -----------------------------------------------------------------------------
# Cached weather bundle (current + forecast + air)
# -----------------------------------------------------------------------------
async def _weather_bundle(lat: float, lon: float):
    key = f"wx:{round(lat, 4)}:{round(lon, 4)}"
    hit = CACHE_WEATHER.get(key)
    if hit:
        return hit

    # parallel calls
    bundle, air = await asyncio.gather(
        OW.current_and_forecast(lat, lon),
        OW.air_quality(lat, lon),
    )
    data = (bundle, air)
    CACHE_WEATHER.set(key, data)
    return data


# -----------------------------------------------------------------------------
# Kakao parallel search
# -----------------------------------------------------------------------------
async def _search_kakao_parallel(kwds: List[str], lon: float, lat: float, radius: int, size: int):
    tasks = [KAKAO.search_keyword(query=q, x=lon, y=lat, radius=radius, size=size) for q in kwds]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    outs = []
    for r in results:
        if isinstance(r, Exception):
            continue
        outs.append(r)
    return outs


# -----------------------------------------------------------------------------
# Candidates core builder (no cache)
# -----------------------------------------------------------------------------
async def _build_candidates_core(gu: str, terrain: Literal["평지", "경사"]) -> dict:
    if gu not in SEOUL_COORDS:
        raise HTTPException(400, f"좌표를 모르는 구입니다: {gu}")
    lat, lon = _extract_latlon(SEOUL_COORDS[gu])

    radius = 4500 if terrain == "평지" else 3800
    kwds = keywords_for_terrain(terrain)
    found: dict[tuple, dict] = {}

    # 1st pass (strict radius)
    res_list = await _search_kakao_parallel(kwds, lon, lat, radius=radius, size=15)

    # 2nd pass if too few (loose radius)
    if len(found) < 3:
        res_list += await _search_kakao_parallel(kwds, lon, lat, radius=radius + 1200, size=15)

    # collect
    for res in res_list:
        for doc in res.get("documents", []):
            name = doc.get("place_name") or ""
            px, py = float(doc["x"]), float(doc["y"])  # x=lon, y=lat
            addr = doc.get("road_address_name") or doc.get("address_name") or ""
            if gu not in addr:  # keep inside selected district
                continue
            key = (px, py, name)
            if key in found:
                continue
            found[key] = dict(
                name=name,
                addr=addr,
                lon=px,
                lat=py,
                signal_free_score=_signal_score_by_name(name),
            )

    # sort by (signal desc, dist asc)
    def dist2(p):
        return (p["lat"] - lat) ** 2 + (p["lon"] - lon) ** 2

    items = sorted(found.values(), key=lambda p: (-p["signal_free_score"], dist2(p)))[:20]

    # attach simple features needed by frontend/recommend
    dists = [haversine_km(lat, lon, it["lat"], it["lon"]) for it in items]
    for it, d in zip(items, dists):
        it["dist_km"] = round(d, 2)
        it["est_loop_km"] = estimate_loop_km(it["name"], terrain)

    return {
        "gu": gu,
        "terrain": terrain,
        "center": {"lat": lat, "lon": lon},
        "count": len(items),
        "items": items,
    }


# cached facade
async def _cached_candidates(gu: str, terrain: Literal["평지", "경사"]) -> dict:
    key = f"cand:{gu}:{terrain}"
    hit = CACHE_CAND.get(key)
    if hit:
        return hit
    data = await _build_candidates_core(gu, terrain)
    CACHE_CAND.set(key, data)
    return data


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/weather")
async def weather(gu: str = Query(...), hour: int = Query(19, ge=0, le=23)):
    if gu not in SEOUL_COORDS:
        raise HTTPException(400, f"좌표를 모르는 구입니다: {gu}")
    lat, lon = _extract_latlon(SEOUL_COORDS[gu])

    try:
        bundle, air = await _weather_bundle(lat, lon)
    except Exception as e:
        raise HTTPException(502, f"OpenWeather 호출 실패: {e}")

    cur = bundle["current"]
    target_item = min(
        bundle["forecast"]["list"],
        key=lambda x: abs(int(x["dt_txt"][11:13]) - hour),
    )

    cur_temp = cur["main"]["temp"]
    cur_desc = cur["weather"][0]["description"]
    fc_temp = target_item["main"]["temp"]
    fc_pop = target_item.get("pop", 0.0)
    fc_desc = target_item["weather"][0]["description"]

    aqi_item = air["list"][0]
    pm2_5 = aqi_item["components"].get("pm2_5")
    pm10 = aqi_item["components"].get("pm10")

    score = running_score(fc_temp, fc_pop, pm2_5, pm10)

    return {
        "gu": gu,
        "coords": {"lat": lat, "lon": lon},
        "now": {"temp_c": cur_temp, "desc": cur_desc},
        "forecast_near_hour": {"hour": hour, "temp_c": fc_temp, "pop": fc_pop, "desc": fc_desc},
        "air_quality": {"pm2_5": pm2_5, "pm10": pm10},
        "running_fitness": score,
    }


@app.post("/score")
async def score(input: UserInput):
    if input.gu not in SEOUL_COORDS:
        raise HTTPException(400, f"좌표를 모르는 구입니다: {input.gu}")
    lat, lon = _extract_latlon(SEOUL_COORDS[input.gu])

    try:
        bundle, air = await _weather_bundle(lat, lon)
    except Exception as e:
        raise HTTPException(502, f"OpenWeather 호출 실패: {e}")

    target_item = min(
        bundle["forecast"]["list"],
        key=lambda x: abs(int(x["dt_txt"][11:13]) - input.hour),
    )
    fc_temp = target_item["main"]["temp"]
    fc_pop = target_item.get("pop", 0.0)
    aqi_item = air["list"][0]
    pm2_5 = aqi_item["components"].get("pm2_5")
    pm10 = aqi_item["components"].get("pm10")

    return {
        "input": input.model_dump(),
        "fitness": running_score(fc_temp, fc_pop, pm2_5, pm10),
    }


@app.get("/candidates")
async def candidates(gu: str = Query(...), terrain: Literal["평지", "경사"] = Query(...)):
    try:
        return await _cached_candidates(gu, terrain)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Kakao Local 호출 실패: {e}")


@app.post("/recommend")
async def recommend(input: UserInput):
    if input.gu not in SEOUL_COORDS:
        raise HTTPException(400, f"좌표를 모르는 구입니다: {input.gu}")
    lat, lon = _extract_latlon(SEOUL_COORDS[input.gu])

    # weather
    try:
        bundle, air = await _weather_bundle(lat, lon)
    except Exception as e:
        raise HTTPException(502, f"OpenWeather 호출 실패: {e}")

    target_item = min(
        bundle["forecast"]["list"],
        key=lambda x: abs(int(x["dt_txt"][11:13]) - input.hour),
    )
    fc_temp = target_item["main"]["temp"]
    fc_pop = target_item.get("pop", 0.0)
    aqi_item = air["list"][0]
    pm2_5 = float(aqi_item["components"].get("pm2_5", 0.0))
    pm10 = float(aqi_item["components"].get("pm10", 0.0))
    wscore = running_score(fc_temp, fc_pop, pm2_5, pm10)["score"]  # 0~100

    # candidates (cached)
    res = await _cached_candidates(input.gu, input.terrain)
    items: List[dict] = res["items"]
    if not items:
        return {"message": "후보 장소를 찾지 못했습니다.", "input": input.model_dump()}

    # scoring
    dists = [haversine_km(lat, lon, it["lat"], it["lon"]) for it in items]
    dmin, dmax = (min(dists), max(dists)) if dists else (0.0, 1.0)

    scored = []
    for it, d in zip(items, dists):
        proximity = 1.0 - _normalize(d, 0.0, max(0.5, dmax))
        hill = hilliness_by_name(it["name"])
        signal = float(it["signal_free_score"])

        if input.terrain == "경사":
            cand = 0.4 * hill + 0.4 * signal + 0.2 * proximity
        else:
            cand = 0.6 * signal + 0.3 * proximity + 0.1 * (1.0 - hill)

        final = 0.6 * cand + 0.4 * (wscore / 100.0)
        it2 = dict(it)
        it2.update(
            cand_score=round(cand, 3),
            final_score=round(final, 3),
        )
        scored.append(it2)

    scored.sort(key=lambda x: -x["final_score"])
    top = scored[:3]
    picked = top[0]

    payload = {
        "gu": input.gu,
        "hour": input.hour,  # ★ 추가: 선택한 시각
        "terrain": input.terrain,
        "weather": {
            "hour": input.hour,  # ★ 프론트에서 라벨로 쓰기 좋게
            "temp_c": fc_temp,
            "pop": fc_pop,
            "pm25": pm2_5,
            "pm10": pm10,
            "score": wscore
        },
        "candidates": [
            {
                "name": s["name"],
                "signal": s["signal_free_score"],
                "dist_km": s["dist_km"],
                "est_loop_km": s["est_loop_km"]
            }
            for s in top
        ],
        "picked": {
            "name": picked["name"],
            "signal": picked["signal_free_score"],
            "dist_km": picked["dist_km"],
            "est_loop_km": picked["est_loop_km"]
        }
    }
    reason = build_reason_korean(payload)

    return {
        "input": input.model_dump(),
        "weather": payload["weather"],  # ★ hour가 포함됨
        "top_candidates": top,
        "recommended": picked,
        "reason_korean": reason
    }


# -----------------------------------------------------------------------------
# Route (cached) — choose nearest good way then crop out-and-back
# -----------------------------------------------------------------------------
@app.get("/route")
async def route(
    lat: float = Query(...),
    lon: float = Query(...),
    km: float = Query(5.0, ge=1.0, le=20.0),
    radius_m: int = Query(1500, ge=400, le=4000),
    near_thresh_m: int = Query(600, ge=100, le=2000),
):
    cache_key = f"route:{round(lat,5)}:{round(lon,5)}:{km}:{radius_m}:{near_thresh_m}"
    hit = CACHE_ROUTE.get(cache_key)
    if hit:
        return hit

    # name bias
    def name_score(w) -> float:
        name = (w.get("tags", {}).get("name") or "").lower()
        s = 0.0
        if any(k in name for k in ["한강", "천", "탄천", "강변", "수변"]):
            s += 1.0
        if any(k in name for k in ["둘레길", "산책로", "숲", "공원"]):
            s += 0.6
        if "자전거" in name or "cycle" in name:
            s += 0.4
        return s

    # try with relaxed thresholds progressively
    tried = None
    for expand in (0, 600, 1200):
        try:
            ways = await fetch_paths_near(lat, lon, radius_m=radius_m + expand)
        except Exception:
            continue
        if not ways:
            continue

        cand = []
        for w in ways:
            coords = w["coords"]
            idx = nearest_index_on_path(coords, lat, lon)
            d_km = haversine_km(lat, lon, coords[idx][0], coords[idx][1])
            cand.append(
                {
                    "w": w,
                    "nearest_idx": idx,
                    "nearest_d_km": d_km,
                    "len_km": w["len_km"],
                    "name_s": name_score(w),
                }
            )

        within = [c for c in cand if c["nearest_d_km"] <= (near_thresh_m + expand) / 1000.0]
        pool = within if within else cand
        if not pool:
            continue

        # closer → better name → longer
        pool.sort(key=lambda c: (c["nearest_d_km"], -c["name_s"], -c["len_km"]))
        chosen = pool[0]
        coords = chosen["w"]["coords"]
        si = chosen["nearest_idx"]
        route_coords = crop_out_and_back(coords, want_km=km, start_idx=si)

        tried = {
            "length_km": round(path_len_km(route_coords), 3),
            "source": {
                "way_id": chosen["w"].get("id"),
                "name": chosen["w"].get("tags", {}).get("name"),
                "len_km": round(chosen["len_km"], 3),
                "nearest_d_km": round(chosen["nearest_d_km"], 3),
            },
            "polyline": route_coords,
        }
        break

    if not tried:
        tried = {"polyline": [], "length_km": 0.0, "message": "근처에서 사용할 길을 찾지 못했습니다."}

    CACHE_ROUTE.set(cache_key, tried)
    return tried
