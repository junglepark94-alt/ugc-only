# 빙그레 소비자 콘텐츠 레이더 (UGC RADAR)

YouTube에서 빙그레 제품 관련 소비자 콘텐츠(UGC)를 자동 수집·분석하는 대시보드.

## 기능
- 제품 카테고리별 UGC 영상 자동 수집 (매일 16:05 전체, 10:05/22:05 증분)
- 조회수/최신순 정렬, 국내/해외 필터
- OpenAI 기반 영상 AI 요약
- Redis 캐시 (없으면 인메모리 폴백)

## 환경변수
| 변수 | 필수 | 설명 |
|---|---|---|
| `YOUTUBE_API_KEY` | ✅ | YouTube Data API v3 키 (Google Cloud Console) |
| `OPENAI_API_KEY` | 권장 | AI 영상 요약용 |
| `DASHBOARD_PASSWORD` | 권장 | 대시보드 접근 비밀번호 (미설정 시 공개) |
| `REFRESH_PASSWORD` | 권장 | 수동 업데이트 버튼 비밀번호 |
| `FLASK_SECRET_KEY` | 권장 | 세션 서명 키 (랜덤 문자열) |
| `REDIS_URL` | 선택 | Redis 연결 URL (Railway Redis 플러그인) |
| `CACHE_TTL` | 선택 | 캐시 TTL 초 (기본 86400) |

## 로컬 실행
```
pip install -r requirements.txt
python app.py
```

## Railway 배포
1. 이 저장소를 Railway에 연결 (Procfile 자동 인식)
2. Redis 서비스 추가 → `REDIS_URL` 자동 주입 확인
3. 위 환경변수 설정
