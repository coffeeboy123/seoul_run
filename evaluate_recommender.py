# -*- coding: utf-8 -*-
"""
서울 러닝코스 추천 API 자동 평가 스크립트
- 테스트 케이스 생성(구 × 시간 × 지형)
- POST /recommend 호출, 응답 JSON 저장
- 품질 지표 계산: 일관성, 문장-데이터 일치, 지형 타당, 길이 적절성, 응답속도, 다양성
- 결과 csv 저장 + 요약 출력

실행: python evaluate_recommender.py
"""

import os
import json
import time
import math
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple

import requests
import pandas as pd

# -------------------- 구성 --------------------
BASE = "http://127.0.0.1:8002"   # FastAPI 서버
OUT_DIR = "eval_runs"            # 결과 저장 폴더
TIMEOUT = 20                     # 초

# 평가에 사용할 구/시간/지형 조합
GUS = ["마포구", "강남구", "서초구", "송파구", "서대문구"]
HOURS = [7, 12, 19]
TERRAINS = ["평지", "경사"]

# 문장-데이터 일치 판단 임계
RAIN_HIGH = 0.60   # pop >= 0.60 이면 '비/강수' 언급 기대
RAIN_LOW  = 0.20   # pop <= 0.20 이면 '비/강수' 언급 지양
# 길이 적절성(루프 추정치)
LOOP_MIN = 3.0
LOOP_MAX = 8.0
# ------------------------------------------------


# 한글 키워드 규칙들
RIVER_WORDS = ["한강", "천", "탄천", "수변", "강변", "자전거", "공원"]
HILL_WORDS  = ["둘레", "산책로", "산", "봉", "정상", "능선", "계단", "숲", "트레일"]

def contains_any(name: str, keywords: List[str]) -> bool:
    n = (name or "").lower()
    return any(k in n for k in keywords)

def terrain_ok(terrain: str, spot_name: str) -> bool:
    if terrain == "평지":
        # 평지면 강/천/공원/자전거길 선호
        return contains_any(spot_name, RIVER_WORDS)
    else:
        # 경사면 둘레길/산/숲 등 선호
        return contains_any(spot_name, HILL_WORDS)

def consistency_ok(pop: float, wx_score: float) -> bool:
    """
    날씨 일관성 점검:
    - 비가 많이 올수록(>= 0.6) 점수는 낮아야 한다( < 50 )
    - 비가 거의 없을수록(<= 0.2) 점수는 높아야 한다( > 50 )
    나머지 구간은 '판정 불가'로 True 처리(느슨)
    """
    try:
        if pop >= RAIN_HIGH:
            return wx_score < 50
        if pop <= RAIN_LOW:
            return wx_score > 50
        return True
    except Exception:
        return False

def reason_ok(pop: float, reason: str) -> bool:
    """
    추천 이유 문장과 비 데이터의 일치:
    - pop >= 0.6 이면 '비/강수' 언급 기대
    - pop <= 0.2 이면 '비/강수' 언급 지양
    - 그 외 구간은 느슨하게 True
    """
    if reason is None:
        return False
    has_rain_word = ("비" in reason) or ("강수" in reason)
    if pop >= RAIN_HIGH:
        return has_rain_word
    if pop <= RAIN_LOW:
        return not has_rain_word
    return True

def length_ok(est_loop_km: float) -> bool:
    try:
        return LOOP_MIN <= float(est_loop_km) <= LOOP_MAX
    except Exception:
        return False

def safe_get(d: Dict, path: List[str], default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def call_recommend(gu: str, hour: int, terrain: str) -> Tuple[Dict[str, Any], float]:
    url = f"{BASE}/recommend"
    payload = {"gu": gu, "hour": hour, "terrain": terrain}
    t0 = time.time()
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    dt = time.time() - t0
    r.raise_for_status()
    return r.json(), dt

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []

    # 1) 테스트 케이스 루프
    case_id = 0
    for gu in GUS:
        for hour in HOURS:
            for terrain in TERRAINS:
                case_id += 1
                case_name = f"{case_id:03d}_{gu}_{hour}_{terrain}"
                try:
                    data, rt = call_recommend(gu, hour, terrain)
                except Exception as e:
                    print(f"[FAIL] {case_name} -> {e}")
                    rows.append({
                        "case": case_name,
                        "gu": gu, "hour": hour, "terrain": terrain,
                        "ok": False, "error": str(e),
                    })
                    continue

                # 2) JSON 저장
                out_json = os.path.join(OUT_DIR, f"{case_name}.json")
                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                # 3) 필요한 값 추출
                rec = safe_get(data, ["recommended"], {}) or {}
                wx  = safe_get(data, ["weather"], {}) or {}
                pop = float(wx.get("pop", 0.0) or 0.0)
                wx_score = float(wx.get("score", 0.0) or 0.0)
                reason = data.get("reason_korean") or ""
                name = rec.get("name") or ""
                est_loop = rec.get("est_loop_km")

                # 4) 지표 판정
                ok_consistency = consistency_ok(pop, wx_score)
                ok_reason = reason_ok(pop, reason)
                ok_terrain = terrain_ok(terrain, name)
                ok_length = length_ok(est_loop)

                rows.append({
                    "case": case_name,
                    "gu": gu, "hour": hour, "terrain": terrain,
                    "resp_time_s": round(rt, 3),
                    "recommended": name,
                    "pop": round(pop, 3),
                    "wx_score": wx_score,
                    "est_loop_km": est_loop,
                    "ok_consistency": ok_consistency,
                    "ok_reason": ok_reason,
                    "ok_terrain": ok_terrain,
                    "ok_length": ok_length,
                    "reason_text": reason.strip(),
                    "ok": True,
                    "error": "",
                })
                print(f"[OK] {case_name}  {name}  rt={rt:.2f}s")

    # 5) DataFrame / 집계
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT_DIR, "raw_results.csv"), index=False, encoding="utf-8-sig")

    ok_df = df[df["ok"] == True].copy()
    n_total = len(ok_df)

    def rate(col):
        if n_total == 0:
            return 0.0
        return round(100.0 * ok_df[col].mean(), 1)

    # 다양성(유니크 추천 비율)
    diversity = 0.0
    if n_total > 0:
        uniq = ok_df["recommended"].nunique()
        diversity = round(100.0 * uniq / n_total, 1)

    summary = {
        "n_cases_total": len(df),
        "n_ok": n_total,
        "consistency_%": rate("ok_consistency"),
        "reason_match_%": rate("ok_reason"),
        "terrain_fit_%": rate("ok_terrain"),
        "length_fit_%": rate("ok_length"),
        "diversity_%(unique/total)": diversity,
        "mean_resp_time_s": round(ok_df["resp_time_s"].mean(), 3) if n_total else None,
        "p95_resp_time_s": round(ok_df["resp_time_s"].quantile(0.95), 3) if n_total else None,
    }
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 콘솔 요약 출력
    print("\n===== SUMMARY =====")
    for k, v in summary.items():
        print(f"{k:>24}: {v}")

    # 구/지형/시간별 피벗표(선택)
    if n_total:
        pivot = ok_df.pivot_table(
            index=["gu", "terrain"],
            values=["ok_consistency","ok_reason","ok_terrain","ok_length","resp_time_s"],
            aggfunc={"ok_consistency":"mean","ok_reason":"mean","ok_terrain":"mean","ok_length":"mean","resp_time_s":"mean"}
        )
        # 비율→%
        for c in ["ok_consistency","ok_reason","ok_terrain","ok_length"]:
            pivot[c] = (pivot[c] * 100).round(1)
        pivot["resp_time_s"] = pivot["resp_time_s"].round(3)
        pivot.to_csv(os.path.join(OUT_DIR, "pivot_by_gu_terrain.csv"), encoding="utf-8-sig")
        print("\nSaved:", os.path.join(OUT_DIR, "pivot_by_gu_terrain.csv"))

if __name__ == "__main__":
    main()
