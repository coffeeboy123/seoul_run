# services/scoring.py
from typing import Optional, Dict, Any

def running_score(temp_c: float,
                  pop: Optional[float],  # 강수확률 0~1 (없으면 None)
                  pm25: Optional[float],
                  pm10: Optional[float]) -> Dict[str, Any]:
    """
    아주 단순한 규칙 기반 점수 (0~100)
    - 온도 6~22℃ 가산점
    - 강수확률, 미세먼지 페널티
    """
    score = 70.0

    # 온도 (쾌적 구간 15~20, 허용 6~22)
    if 15 <= temp_c <= 20:
        score += 20
    elif 6 <= temp_c <= 22:
        score += 10
    else:
        score -= 10

    # 강수확률
    if pop is not None:
        score -= float(pop) * 30  # 비 올수록 감점 (0~30)

    # 미세먼지 (대략적 임계)
    if pm25 is not None:
        if pm25 > 55:  # 매우 나쁨
            score -= 25
        elif pm25 > 35:
            score -= 15
        elif pm25 > 15:
            score -= 5

    if pm10 is not None:
        if pm10 > 150:
            score -= 15
        elif pm10 > 80:
            score -= 8
        elif pm10 > 30:
            score -= 3

    score = max(0, min(100, round(score, 1)))
    return {"score": score}
