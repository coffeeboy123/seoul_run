# services/gpt_reason.py
import os, json, hashlib, time
from typing import Dict, Optional

# ── (A) 템플릿 폴백 ─────────────────────────────────────────────────────────────
def _fallback_template(payload: Dict) -> str:
    gu = payload.get("gu", "")
    h = int(payload.get("hour", 0))
    terrain = payload.get("terrain", "평지")
    w = payload.get("weather", {}) or {}
    t = float(w.get("temp_c", 0.0))
    pop = int(round(float(w.get("pop", 0.0)) * 100))
    pm25 = w.get("pm25")
    pm10 = w.get("pm10")
    score = int(round(float(w.get("score", 0.0))))
    picked = payload.get("picked", {}) or {}
    name = picked.get("name", "추천 코스")
    dist_km = picked.get("dist_km")
    loop_km = picked.get("est_loop_km")

    bits = [f"{h}시 예상 기온 {t:.1f}℃, 강수확률 {pop}%."]
    if pm25 is not None or pm10 is not None:
        air = []
        if pm25 is not None: air.append(f"PM2.5 {pm25}")
        if pm10  is not None: air.append(f"PM10 {pm10}")
        bits.append(f"대기질 {(' · '.join(air))}.")
    terr = "평지 코스" if terrain == "평지" else "경사 코스"
    tail = f"{gu}에서 접근성과 신호대기 측면에서 '{name}' {terr}을(를) 추천합니다."
    info = []
    if dist_km is not None: info.append(f"중심 기준 약 {dist_km}km")
    if loop_km is not None: info.append(f"루프 약 {loop_km}km")
    if info: tail += " " + " · ".join(info) + "."
    tail += f" (러닝 적합도 {score}/100)"
    return " ".join(bits + [tail]).strip()

# ── (B) 간단 캐시 (프로세스 메모리) ─────────────────────────────────────────────
_CACHE: dict[str, str] = {}
def _cache_key(payload: Dict, tone: Optional[str]) -> str:
    raw = json.dumps({"p": payload, "tone": tone}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

# ── (C) LLM 호출 ───────────────────────────────────────────────────────────────
def _call_openai_reason(payload: Dict, tone: Optional[str] = None, timeout: float = 8.0) -> str:
    """
    OpenAI Chat Completions로 한국어 설명 생성.
    - 반드시 '예상' 시간(h시)을 기준으로 작성
    - '현재'라는 단어 금지
    - 2~3문장, 자연스러운 한국어, 과장 금지
    """
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)

    # 프롬프트(시스템+유저)
    sys = (
        "너는 러닝 코스 추천 앱의 한국어 카피라이터야. "
        "입력으로 주어진 예보(hour) 값을 기준으로 '예상' 컨디션을 간결히 설명하고, "
        "추천 코스의 장점을 자연스럽게 말해. 숫자는 그대로 노출하고, 과장은 피하고, "
        "전체 2~3문장으로 마무리해. '현재'라는 단어는 쓰지 마."
    )
    if tone:
        sys += f" 톤 앤 매너는 '{tone}'에 맞춰."

    # 모델이 상황을 오해하지 않도록 최소 구조 제공
    info = {
        "hour": payload.get("hour"),
        "gu": payload.get("gu"),
        "terrain": payload.get("terrain"),
        "weather": payload.get("weather"),
        "picked": payload.get("picked"),
        "candidates": payload.get("candidates", [])[:3],
    }

    user = (
        "다음 JSON을 읽고 한국어로 2~3문장을 생성해.\n"
        "- 반드시 '{hour}시 예상' 기준으로 서술\n"
        "- 비/대기질/온도는 사실만 간단히\n"
        "- 코스명 1회 언급, 불필요한 해시태그 금지\n"
        "- 있으면 거리/루프 길이도 한 번만 언급\n"
        "- 마지막에 괄호로 '러닝 적합도 X/100'\n"
        "JSON:\n" + json.dumps(info, ensure_ascii=False)
    )

    # 네트워크 변동 고려: 짧은 재시도
    last_err = None
    for _ in range(2):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.8,
                top_p=0.95,
                max_tokens=180,
                timeout=timeout,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": user},
                ],
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    raise RuntimeError(f"OpenAI call failed: {last_err}")

# ── (D) 공개 함수: 앱에서 이 함수만 호출하면 됨 ────────────────────────────────
# services/gpt_reason.py
USE_TEMPLATE_ONLY_FOR_EVAL = False  # ★ 평가 시 True

def build_reason_korean(payload: Dict, *, tone: Optional[str] = "friendly") -> str:
    key = _cache_key(payload, tone)
    if key in _CACHE:
        return _CACHE[key]
    try:
        if USE_TEMPLATE_ONLY_FOR_EVAL:
            text = _fallback_template(payload)   # ★ LLM 비활성화
        else:
            text = _call_openai_reason(payload, tone=tone)
    except Exception:
        text = _fallback_template(payload)
    text = " ".join(text.split())
    _CACHE[key] = text
    return text


