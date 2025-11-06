# services/kakao.py
import os
from typing import Dict, Any, List, Optional
import httpx
from dotenv import load_dotenv

load_dotenv()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY")
BASE = "https://dapi.kakao.com/v2/local"

HEADERS = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"} if KAKAO_REST_API_KEY else None

class KakaoLocalClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or KAKAO_REST_API_KEY
        if not key:
            raise RuntimeError("KAKAO_REST_API_KEY 가 설정되지 않았습니다 (.env 확인).")
        self.headers = {"Authorization": f"KakaoAK {key}"}

    async def search_keyword(self, query: str, x: float, y: float,
                             radius: int = 6000, size: int = 15) -> Dict[str, Any]:
        """
        - query: 검색어 (예: '한강공원', '둘레길')
        - x,y: 중심 좌표 (경도=lon, 위도=lat)
        - radius: m 단위 (최대 20000)
        """
        params = {
            "query": query,
            "x": x,     # 경도
            "y": y,     # 위도
            "radius": radius,
            "size": size,
        }
        async with httpx.AsyncClient(timeout=10, headers=self.headers) as client:
            r = await client.get(f"{BASE}/search/keyword.json", params=params)
            r.raise_for_status()
            return r.json()

# 간단한 후보 생성 유틸
def keywords_for_terrain(terrain: str) -> List[str]:
    # 평지 위주: 강/천/수변/자전거도로/대형공원
    # 경사 위주: 둘레길/산책로/산/공원+언덕/둘레길 이름
    if terrain == "평지":
        return ["한강공원", "수변공원", "자전거도로", "천 산책로", "대형 공원 산책로", "하천 산책로"]
    else:
        return ["둘레길", "산책로", "둘레길 입구", "공원 계단", "산 정상", "숲길"]
