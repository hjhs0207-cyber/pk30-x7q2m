# 포켓몬카드 30주년 팩 재고 (개인용)

- `docs/index.html`: 조회 사이트 (GitHub Pages). 현재 위치·검색·지도 클릭 위치 기준 반경 1km의 GS25·CU·세븐일레븐·이마트24 재고.
- `checker.py` + `.github/workflows/check.yml`: 10분마다 `config.json`의 감시 위치(옥수역·공릉역·상봉역)를 확인하고, 재고가 새로 생기면 Gmail로 알림.
- 필요한 Secrets: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`

재고 데이터는 오픈소스 프로젝트 [daiso-mcp](https://github.com/hmmhmmhm/daiso-mcp)의 공개 API를 거쳐 가져와요. 비공식이라 예고 없이 막힐 수 있어요.
