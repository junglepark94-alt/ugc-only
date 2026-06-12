"""
빙그레 소비자 콘텐츠 (UGC RADAR) 대시보드
Flask 백엔드 - YouTube Data API v3 (API 키) + OpenAI 요약
"""
from __future__ import annotations

import os
import re
import json
import math
import time
import threading
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request, session, redirect, url_for

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "your-secret-key-change-this")

# 응답 gzip 압축 (대용량 JSON 전송량 절감)
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)

# ── 접근 비밀번호 ───────────────────────────
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# ── UGC 설정 ───────────────────────────────
UGC_YOUTUBE_API_KEY  = os.getenv("YOUTUBE_API_KEY", "")
UGC_OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
UGC_CACHE_TTL        = int(os.getenv("CACHE_TTL", "172800"))      # 48h: 일일 갱신 1회 실패해도 전날 데이터 유지
UGC_AI_CACHE_TTL     = int(os.getenv("AI_CACHE_TTL", "604800"))   # 7d: 영상 요약은 내용이 변하지 않음
UGC_REFRESH_PASSWORD = os.getenv("REFRESH_PASSWORD", "")

# ── Redis 클라이언트 ───────────────────────
try:
    import redis as _ugc_redis_lib
    _ugc_redis_url = os.getenv("REDIS_URL", "")
    _ugc_redis = _ugc_redis_lib.from_url(_ugc_redis_url) if _ugc_redis_url else None
except Exception:
    _ugc_redis = None

_ugc_cache = {}
_ugc_last_updated = None


def _ugc_load_last_updated():
    global _ugc_last_updated
    if _ugc_redis:
        try:
            val = _ugc_redis.get("ugc_last_updated")
            if val:
                _ugc_last_updated = datetime.fromisoformat(val.decode())
        except Exception:
            pass


def ugc_cache_get(key):
    if _ugc_redis:
        try:
            val = _ugc_redis.get(f"ugc:{key}")
            if val:
                return json.loads(val)
        except Exception:
            pass
    entry = _ugc_cache.get(key)
    if entry and (time.time() - entry["ts"]) < entry.get("ttl", UGC_CACHE_TTL):
        return entry["data"]
    return None


def ugc_cache_set(key, data, ttl=None):
    ttl = ttl or UGC_CACHE_TTL
    if _ugc_redis:
        try:
            _ugc_redis.setex(f"ugc:{key}", ttl, json.dumps(data, ensure_ascii=False))
            return
        except Exception:
            pass
    _ugc_cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}


# ── 제품 카테고리 ──────────────────────────────────────────────
UGC_PRODUCT_CATEGORIES = {
    "빙그레": {
        "keywords": ["빙그레 먹방", "bingrae korea"],
        "color": "#E8383B"
    },
    "바나나맛우유": {
        "keywords": ["바나나맛우유", "banana milk bingrae", "香蕉牛奶"],
        "color": "#F5A623"
    },
    "부라보콘": {
        "keywords": ["부라보콘 먹방", "bravocone bingrae"],
        "color": "#D4865A"
    },
    "메로나": {
        "keywords": ["메로나", "melona ice cream"],
        "color": "#4CAF82"
    },
    "붕어싸만코": {
        "keywords": ["붕어싸만코", "samanco ice cream"],
        "color": "#C0853A"
    },
    "요플레": {
        "keywords": ["요플레 먹방", "yoplait korea"],
        "color": "#E8657A"
    },
    "투게더": {
        "keywords": ["빙그레 투게더", "together bingrae"],
        "color": "#9B7FD4"
    },
    "엑설런트": {
        "keywords": ["엑설런트 아이스크림", "빙그레엑셀런트"],
        "color": "#E05A9A"
    },
    "더:단백": {
        "keywords": ["더단백 먹방", "빙그레 더단백"],
        "color": "#4A6FA5"
    }
}

UGC_NEGATIVE_KEYWORDS = [
    "맛없", "맛 없", "맛이없", "더럽", "불량", "이물질", "벌레",
    "불만", "소비자 불만", "환불", "항의", "민원", "고발",
    "실망", "최악", "짜증", "역겨", "구역질",
    "불쾌", "위생 문제", "곰팡이",
    "바가지", "비싸다", "비싸네", "비쌈", "가격 논란", "가격 문제",
    "bad", "worst", "disgusting", "terrible", "gross"
]

UGC_EXCLUDE_KEYWORDS = [
    "이글스", "야구", "baseball", "한화", "선수", "경기", "투수", "타자",
    "홈런", "승리", "패배", "리그", "시즌", "응원", "구단",
    "투모로우바이투게더", "tomorrow by together", "txt", "TXT"
]

# 자사 공식 채널 영상은 소비자 콘텐츠(UGC)가 아니므로 제외
UGC_EXCLUDE_CHANNEL_IDS    = {"UC3fvuZiuwjbwyOI3FpztlXw"}  # 빙그레(Binggrae) @official.binggrae
UGC_EXCLUDE_CHANNEL_TITLES = {"빙그레(binggrae)"}  # 소문자 비교 — channel_id 없는 기존 캐시 항목용


def _ugc_filter_own_channel(entries):
    """캐시된 UGC 목록에서 자사 공식 채널 영상 제거 (서빙 시점 필터)."""
    return [e for e in entries
            if e.get("channel_id") not in UGC_EXCLUDE_CHANNEL_IDS
            and e.get("channel", "").lower().strip() not in UGC_EXCLUDE_CHANNEL_TITLES]


def ugc_classify_sentiment(title, description):
    text = (title + " " + description).lower()
    neg = sum(1 for kw in UGC_NEGATIVE_KEYWORDS if kw in text)
    return "negative" if neg > 0 else "positive"


def ugc_is_overseas(title, description):
    text = title + " " + description[:300]
    return not bool(re.search(r'[가-힣ᄀ-ᇿ㄰-㆏]', text))


def ugc_parse_duration(duration_str):
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str or "")
    if not match:
        return 0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s


def ugc_calc_virality_score(stats, published_at):
    views    = int(stats.get("viewCount", 0))
    likes    = int(stats.get("likeCount", 0))
    comments = int(stats.get("commentCount", 0))
    try:
        pub_dt   = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        days_old = (datetime.now(timezone.utc) - pub_dt).days
    except Exception:
        return None
    if days_old > 365:
        return None
    timeliness    = 25 * (0.99 ** days_old)
    view_score    = min(math.log10(views + 1) / 7 * 35, 35)
    like_score    = min(math.log10(likes + 1) / 6 * 25, 25)
    comment_score = min(math.log10(comments + 1) / 5 * 15, 15)
    total = round(min(timeliness + view_score + like_score + comment_score, 100), 1)
    return {
        "score":         total,
        "timeliness":    round(timeliness, 1),
        "view_score":    round(view_score, 1),
        "like_score":    round(like_score, 1),
        "comment_score": round(comment_score, 1),
    }


def ugc_build_entry(v, category):
    snippet = v.get("snippet", {})
    stats   = v.get("statistics", {})
    content = v.get("contentDetails", {})
    pub_str = snippet.get("publishedAt", "")

    if snippet.get("channelId") in UGC_EXCLUDE_CHANNEL_IDS:
        return None
    if snippet.get("channelTitle", "").lower().strip() in UGC_EXCLUDE_CHANNEL_TITLES:
        return None

    title_lower = snippet.get("title", "").lower()
    desc_lower  = snippet.get("description", "")[:200].lower()
    if any(kw in title_lower or kw in desc_lower for kw in UGC_EXCLUDE_KEYWORDS):
        return None
    if category == "투게더":
        together_check = title_lower + " " + desc_lower
        if not any(kw in together_check for kw in ["빙그레", "아이스크림", "ice cream", "bingrae", "투게더아이스크림", "투게더 아이스크림"]):
            return None
    views = int(stats.get("viewCount", 0))
    if views < 1000:
        return None
    score_data = ugc_calc_virality_score(stats, pub_str)
    if score_data is None:
        return None
    try:
        pub_dt   = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        days_old = (datetime.now(timezone.utc) - pub_dt).days
    except Exception:
        days_old = 0
    return {
        "video_id":      v["id"],
        "title":         snippet.get("title", ""),
        "channel":       snippet.get("channelTitle", ""),
        "channel_id":    snippet.get("channelId", ""),
        "thumbnail":     snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
        "published_at":  pub_str[:10],
        "days_old":      days_old,
        "views":         stats.get("viewCount", 0),
        "likes":         stats.get("likeCount", 0),
        "comments":      stats.get("commentCount", 0),
        "duration":      ugc_parse_duration(content.get("duration", "PT0S")),
        "score":         score_data["score"],
        "timeliness":    score_data["timeliness"],
        "view_score":    score_data["view_score"],
        "like_score":    score_data["like_score"],
        "comment_score": score_data["comment_score"],
        "category":      category,
        "description":   snippet.get("description", "")[:300],
        "sentiment":     ugc_classify_sentiment(snippet.get("title", ""), snippet.get("description", "")[:300]),
        "is_overseas":   ugc_is_overseas(snippet.get("title", ""), snippet.get("description", ""))
    }


def ugc_search_youtube(keywords, max_results=25, published_after=None):
    yt_key = os.getenv("YOUTUBE_API_KEY", "") or UGC_YOUTUBE_API_KEY
    if not yt_key:
        return []

    def search_one(kw):
        params = {
            "key": yt_key, "q": kw,
            "type": "video", "part": "id",
            "maxResults": max_results,
            "order": "date",
            "relevanceLanguage": "ko"
        }
        if published_after:
            params["publishedAfter"] = published_after
        try:
            res = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=params,
                timeout=15
            )
            return [item["id"]["videoId"] for item in res.json().get("items", [])]
        except Exception as e:
            print(f"[UGC] Search error '{kw}': {e}")
            return []

    ids = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(search_one, keywords):
            ids.extend(result)
    return list(set(ids))


def ugc_get_video_details(video_ids):
    yt_key = os.getenv("YOUTUBE_API_KEY", "") or UGC_YOUTUBE_API_KEY
    if not video_ids or not yt_key:
        return []
    results = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i+50]
        try:
            res = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "key": yt_key,
                    "id": ",".join(chunk),
                    "part": "snippet,statistics,contentDetails"
                },
                timeout=10
            )
            results += res.json().get("items", [])
        except Exception as e:
            print(f"[UGC] Video detail error: {e}")
    return results


def ugc_get_ai_analysis(title, description, channel, sentiment="positive", score=0,
                        timeliness=0, view_score=0, like_score=0, comment_score=0):
    api_key = os.getenv("OPENAI_API_KEY", "") or UGC_OPENAI_API_KEY
    if not api_key:
        return {
            "summary": "OPENAI_API_KEY를 환경변수에 설정하면 AI 분석이 활성화됩니다.",
            "sentiment": sentiment,
        }

    prompt = f"""유튜브 UGC 영상 정보야.

채널명: {channel}
영상 제목: {title}
영상 설명: {(description or '')[:500]}

5줄 내외로, ① 이 영상이 어떤 내용인지, ② 빙그레 제품이 어떻게 노출·활용됐는지 작성해줘.

반드시 아래 JSON 형식으로만 응답하세요:
{{"summary": "요약 내용"}}"""

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
            verify=False
        )
        raw = res.json()["choices"][0]["message"]["content"]
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            result["sentiment"] = sentiment
            return result
    except Exception as e:
        print(f"[UGC] AI analysis error: {e}")

    return {"summary": "분석 중 오류가 발생했습니다.", "sentiment": sentiment}


def ugc_demo_data():
    cats = list(UGC_PRODUCT_CATEGORIES.keys())
    samples = [
        ("dQw4w9WgXcQ", "{} 진짜 맛있는 거 맞죠? 먹방+솔직후기", "먹방왕국TV",  "2025-04-18", 284000, 12400, 893, 847, 87.3, 3),
        ("ScMzIvxBSi4", "{} 편의점 신상 vs 기존 맛 비교",         "편의점탐험대", "2025-04-14", 156000,  7800, 421, 612, 74.2, 7),
        ("oHg5SJYRHA0", "외국인 친구한테 {} 처음 먹여봤더니",     "글로벌라이프", "2025-04-10",  92000,  5100, 367, 423, 68.9, 11),
        ("9bZkp7q19f0", "{} 레시피 활용법 모음.zip",              "요리하는언니", "2025-04-07",  45000,  3200, 198, 384, 55.4, 14),
        ("kJQP7kiw5Fk", "일상 브이로그 | {} 사러 갔다가 충동구매", "소소한일상",  "2025-04-04",  18000,   940,  77, 921, 38.1, 17),
        ("L_jWHffIx5E", "{} 먹으면서 공부하는 브이로그",          "스터디with민지","2025-04-20",  31000,  1800, 143, 1205, 44.6, 1),
        ("fJ9rUzIMcZQ", "편의점 {} 신상 즉석 리뷰",              "편스타그램",   "2025-04-19",  67000,  3400, 289, 234, 62.1, 2),
    ]
    result = []
    for i, (vid, title_tpl, ch, dt, views, likes, cmts, dur, score, days) in enumerate(samples):
        cat = cats[i % len(cats)]
        title = title_tpl.format(cat)
        result.append({
            "video_id": vid, "title": title, "channel": ch,
            "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
            "published_at": dt, "days_old": days,
            "views": views, "likes": likes, "comments": cmts, "duration": dur,
            "score": score, "category": cat,
            "description": f"{cat} 관련 영상입니다.",
            "sentiment": "positive", "is_overseas": False,
            "timeliness": round(50*(0.99**days),1), "view_score": 0,
            "like_score": 0, "comment_score": 0,
        })
    return result


def ugc_prefetch_all(incremental=False):
    global _ugc_last_updated
    mode = "증분" if incremental else "전체"
    print(f"[UGC SCHEDULER] {mode} 업데이트 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 증분 모드: 최근 2일치 신규 영상만 검색해 기존 캐시에 병합
    published_after = None
    if incremental:
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        _ugc_cache.clear()

    def fetch_category(category, cat_info):
        new_ids = list(set(ugc_search_youtube(cat_info["keywords"], published_after=published_after)))
        new_entries = []
        for v in ugc_get_video_details(new_ids):
            e = ugc_build_entry(v, category)
            if e:
                new_entries.append(e)

        if incremental:
            existing = ugc_cache_get(f"cat:{category}") or []
            existing_ids = {e["video_id"] for e in existing}
            merged = existing + [e for e in new_entries if e["video_id"] not in existing_ids]
            merged = [e for e in merged if e.get("days_old", 0) <= 365]
            sorted_entries = sorted(merged, key=lambda x: x["score"], reverse=True)
        else:
            sorted_entries = sorted(new_entries, key=lambda x: x["score"], reverse=True)

        return category, sorted_entries

    all_videos = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_category, cat, info): cat
                   for cat, info in UGC_PRODUCT_CATEGORIES.items()}
        for fut in as_completed(futures):
            try:
                category, entries = fut.result()
                all_videos.extend(entries)
                if entries:
                    ugc_cache_set(f"cat:{category}", entries)
                    print(f"[UGC SCHEDULER] {category}: {len(entries)}개")
                else:
                    print(f"[UGC SCHEDULER] {category}: 결과 없음")
            except Exception as e:
                print(f"[UGC SCHEDULER] Fetch error: {e}")

    seen, unique = set(), []
    for v in sorted(all_videos, key=lambda x: x["score"], reverse=True):
        if v["video_id"] not in seen:
            seen.add(v["video_id"])
            unique.append(v)

    if unique:
        ugc_cache_set("all", unique)
        _ugc_last_updated = datetime.now(timezone(timedelta(hours=9)))
        if _ugc_redis:
            try:
                _ugc_redis.setex("ugc_last_updated", UGC_CACHE_TTL, _ugc_last_updated.isoformat())
            except Exception:
                pass

    print(f"[UGC SCHEDULER] {mode} 업데이트 완료: 총 {len(unique)}개 영상")


# ── 페이지 라우트 ──────────────────────────────────────────────

@app.route("/")
def index():
    if DASHBOARD_PASSWORD and not session.get("viewer_authed"):
        return redirect(url_for("viewer_login"))
    ugc_categories = [{"name": k, "color": v["color"]} for k, v in UGC_PRODUCT_CATEGORIES.items()]
    return render_template("ugc.html", ugc_categories=ugc_categories)


@app.route("/login", methods=["GET", "POST"])
def viewer_login():
    # 비밀번호 미설정 시 로그인 화면 없이 바로 대시보드로
    if not DASHBOARD_PASSWORD:
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw and pw == DASHBOARD_PASSWORD:
            session["viewer_authed"] = True
            return redirect(url_for("index"))
        error = "비밀번호가 올바르지 않습니다."
    return (
        """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>빙그레 소비자 콘텐츠 대시보드</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', sans-serif;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 12px;
          padding: 48px 40px; width: 100%; max-width: 380px; text-align: center; }
  .logo { font-size: 20px; font-weight: 700; color: #fff; margin-bottom: 8px; line-height: 1.4; }
  .logo-img { width: 48px; height: 48px; object-fit: contain; margin-bottom: 12px; }
  .sub  { font-size: 13px; color: #888; margin-bottom: 36px; }
  input[type=password] { width: 100%; padding: 12px 16px; border-radius: 8px;
    border: 1px solid #2a2d3a; background: #12141d; color: #e0e0e0;
    font-size: 15px; outline: none; transition: border .2s; }
  input[type=password]:focus { border-color: #FF0000; }
  button { width: 100%; margin-top: 16px; padding: 12px; border-radius: 8px;
    background: #FF0000; color: #fff; font-size: 15px; font-weight: 600;
    border: none; cursor: pointer; transition: background .2s; }
  button:hover { background: #cc0000; }
  .error { color: #f87171; font-size: 13px; margin-top: 12px; }
</style>
</head>
<body>
<div class="card">
  <img src="/static/logo.png" class="logo-img" alt="빙그레 로고">
  <div class="logo">빙그레 소비자 콘텐츠<br>실시간 분석 대시보드</div>
  <div class="sub">접근 비밀번호를 입력해 주세요</div>
  <form method="POST">
    <input type="password" name="password" placeholder="비밀번호" autofocus>
    <button type="submit">로그인</button>
  </form>"""
        + (f'<div class="error">{error}</div>' if error else "")
        + """
  <div style="margin-top:28px;font-size:13px;color:#ccc;line-height:1.6">문의: 홍보담당 콘텐츠전략팀 박종걸 (6198)</div>
</div>
</body>
</html>"""
    )


@app.route("/logout")
def viewer_logout():
    session.pop("viewer_authed", None)
    return redirect(url_for("viewer_login"))


# ── UGC API 라우트 ─────────────────────────────────────────────

@app.route("/api/ugc/categories")
def ugc_api_categories():
    return jsonify([{"name": k, "color": v["color"]} for k, v in UGC_PRODUCT_CATEGORIES.items()])


@app.route("/api/ugc/search/all")
def ugc_api_search_all():
    cached = ugc_cache_get("all")
    if cached:
        return jsonify({"videos": _ugc_filter_own_channel(cached), "demo": False, "cached": True})
    if not (os.getenv("YOUTUBE_API_KEY", "") or UGC_YOUTUBE_API_KEY):
        return jsonify({"videos": ugc_demo_data(), "demo": True})
    return jsonify({"videos": [], "demo": False, "cached": False, "pending": True})


@app.route("/api/ugc/search")
def ugc_api_search():
    category = request.args.get("category", "바나나맛우유")
    if category not in UGC_PRODUCT_CATEGORIES:
        return jsonify({"error": "유효하지 않은 카테고리입니다."}), 400
    cached = ugc_cache_get(f"cat:{category}")
    if cached:
        return jsonify({"videos": _ugc_filter_own_channel(cached), "demo": False, "cached": True})
    if not (os.getenv("YOUTUBE_API_KEY", "") or UGC_YOUTUBE_API_KEY):
        return jsonify({"videos": ugc_demo_data(), "demo": True})
    return jsonify({"videos": [], "demo": False, "cached": False, "pending": True})


@app.route("/api/ugc/last-updated")
def ugc_api_last_updated():
    if _ugc_last_updated:
        return jsonify({
            "updated": True,
            "datetime": _ugc_last_updated.strftime("%Y년 %m월 %d일 %p %I:%M").replace("AM", "오전").replace("PM", "오후"),
            "iso": _ugc_last_updated.isoformat()
        })
    return jsonify({"updated": False, "datetime": None})


@app.route("/api/ugc/refresh", methods=["POST"])
def ugc_api_refresh():
    data = request.json or {}
    if not UGC_REFRESH_PASSWORD or data.get("password") != UGC_REFRESH_PASSWORD:
        return jsonify({"ok": False, "message": "비밀번호가 틀렸습니다."}), 401
    threading.Thread(target=ugc_prefetch_all, daemon=True).start()
    return jsonify({"ok": True, "message": "데이터 수집을 시작했습니다. 잠시 후 새로고침해주세요."})


@app.route("/api/ugc/status")
def ugc_api_status():
    yt_key  = os.getenv("YOUTUBE_API_KEY", "")
    oai_key = os.getenv("OPENAI_API_KEY", "")
    return jsonify({
        "YOUTUBE_API_KEY":  bool(yt_key),
        "OPENAI_API_KEY":   bool(oai_key),
        "yt_key_prefix":    yt_key[:8]  + "..." if yt_key  else "(empty)",
        "oai_key_prefix":   oai_key[:8] + "..." if oai_key else "(empty)",
    })


@app.route("/api/ugc/analyze", methods=["POST"])
def ugc_api_analyze():
    data = request.json or {}
    video_id = data.get("video_id", "")
    if video_id:
        cached = ugc_cache_get(f"ai:{video_id}")
        if cached:
            return jsonify(cached)
    result = ugc_get_ai_analysis(
        title=data.get("title", ""),
        description=data.get("description", ""),
        channel=data.get("channel", ""),
        sentiment=data.get("sentiment", "positive"),
        score=data.get("score", 0),
        timeliness=data.get("timeliness", 0),
        view_score=data.get("view_score", 0),
        like_score=data.get("like_score", 0),
        comment_score=data.get("comment_score", 0),
    )
    if video_id:
        ugc_cache_set(f"ai:{video_id}", result, ttl=UGC_AI_CACHE_TTL)
    return jsonify(result)


# ── 시작 워밍 + 스케줄러 ───────────────────────────────────────

def _startup_warmup():
    """배포 직후 UGC 캐시가 비어 있으면 한 번 워밍."""
    try:
        if _ugc_redis:
            ugc_updated = _ugc_redis.get("ugc_last_updated")
            if ugc_updated:
                print(f"[STARTUP] UGC 캐시 있음 ({ugc_updated.decode()}) — 스킵")
                return
        # Redis 캐시가 없거나, Redis 미설정(재시작 시 인메모리 캐시는 항상 빈 상태)
        print("[STARTUP] UGC 캐시 없음 — 워밍 시작")
        threading.Thread(target=ugc_prefetch_all, daemon=True).start()
    except Exception as _e:
        print(f"[STARTUP] 워밍 오류: {_e}")


def _ugc_retry_if_stale():
    """16:05 갱신이 실패했으면 한 번 더 시도 (17:05 점검)."""
    kst = timezone(timedelta(hours=9))
    today_1600 = datetime.now(kst).replace(hour=16, minute=0, second=0, microsecond=0)
    if _ugc_last_updated and _ugc_last_updated >= today_1600:
        return
    print("[UGC SCHEDULER] 16:05 갱신 미완료 감지 — 재시도")
    ugc_prefetch_all()


try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _ugc_scheduler = BackgroundScheduler(
        timezone="Asia/Seoul",
        job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    )
    # 매일 16:05 전체 갱신 (하루 1회) + 17:05 실패 시 재시도
    _ugc_scheduler.add_job(ugc_prefetch_all,    "cron", hour=16, minute=5, id="ugc_daily")
    _ugc_scheduler.add_job(_ugc_retry_if_stale, "cron", hour=17, minute=5, id="ugc_retry")
    _ugc_scheduler.start()
    _ugc_load_last_updated()
    print("[SCHEDULER] 매일 16:05 전체 UGC 워밍 (17:05 실패 재시도)")
    _startup_warmup()
except Exception as _e:
    print(f"[스케줄러] 시작 실패: {_e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
