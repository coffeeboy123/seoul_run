# services/overpass.py
import httpx
from typing import Dict, Any, List, Tuple
from math import radians, sin, cos, asin, sqrt

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

def haversine_km(a: Tuple[float,float], b: Tuple[float,float]) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    R = 6371.0
    dlat = radians(lat2-lat1)
    dlon = radians(lon2-lon1)
    x = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2*R*asin(sqrt(x))

def path_len_km(coords: List[Tuple[float,float]]) -> float:
    return sum(haversine_km(coords[i], coords[i+1]) for i in range(len(coords)-1))

async def fetch_paths_near(lat: float, lon: float, radius_m: int = 2000) -> List[Dict[str, Any]]:
    """
    주변 반경 내에서 보행/자전거 길을 수집 (way + geometry)
    - highway=path|footway|cycleway
    """
    q = f"""
    [out:json][timeout:25];
    (
      way(around:{radius_m},{lat},{lon})["highway"~"^(path|footway|cycleway)$"];
    );
    out geom tags;
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(OVERPASS_URL, data={"data": q})
        r.raise_for_status()
        data = r.json()
    ways = data.get("elements", [])
    # geometry -> [(lat,lon), ...]
    for w in ways:
        geom = w.get("geometry", [])
        w["coords"] = [(g["lat"], g["lon"]) for g in geom]
        w["len_km"] = path_len_km(w["coords"]) if len(w["coords"]) > 1 else 0.0
    # 길이가 있고, 좌표가 충분한 것만
    ways = [w for w in ways if w["len_km"] > 0.05 and len(w["coords"]) > 2]
    # 긴 순으로 우선
    ways.sort(key=lambda x: -x["len_km"])
    return ways

def crop_out_and_back(coords: List[Tuple[float,float]], want_km: float, start_idx: int) -> List[Tuple[float,float]]:
    """
    주어진 polyline에서 start_idx를 기준으로 한 방향으로 절반 거리만큼 갔다가
    되돌아오는 '왕복' 루트를 만들어 '사실상 루프'처럼 보이게 함.
    """
    if want_km <= 0: want_km = 3.0
    half = want_km / 2.0
    # forward
    path = [coords[start_idx]]
    acc = 0.0
    i = start_idx
    while i < len(coords)-1 and acc < half:
        seg = haversine_km(coords[i], coords[i+1])
        path.append(coords[i+1])
        acc += seg
        i += 1
    # backward (되돌아오기)
    back = list(reversed(path[:-1]))
    return path + back

def nearest_index_on_path(coords: List[Tuple[float,float]], lat: float, lon: float) -> int:
    best_i, best_d = 0, 1e9
    p = (lat, lon)
    for i, c in enumerate(coords):
        d = haversine_km(c, p)
        if d < best_d:
            best_d, best_i = d, i
    return best_i
