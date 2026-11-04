# ksafetrip-content

K-Safe Trip iOS 앱이 받아 가는 여행 콘텐츠(JSON)와 개인정보 처리방침·지원 페이지예요. GitHub Pages 로 게시돼요.

- 손으로 쓰는 콘텐츠는 앱 저장소의 `scripts/export_content.sh` 로 복사해요.
- `events.json` 은 `.github/workflows/tourapi.yml` 이 1시간마다 한국관광공사 TourAPI 에서 받아 만들어요.
  - Settings › Secrets and variables › Actions 에 `TOURAPI_KEY` 를 넣어야 동작해요.
- 실행 코드는 앱이 받지 않아요. 앱은 JSON 데이터만 받아요.
