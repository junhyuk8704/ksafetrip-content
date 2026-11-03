#!/usr/bin/env python3
"""한국관광공사 TourAPI 에서 8개 도시의 음식점·카페·축제를 받아 places.json / events.json 을 만들어요.

파이썬 표준 라이브러리만 써요 (GitHub Actions 에서 따로 설치할 것이 없게).

사용법:
    TOURAPI_KEY=<공공데이터포털 일반 인증키(Decoding)> python3 fetch_tourapi.py --out <폴더>
    (GitHub Actions 가 1시간마다 실행해요 — scripts/tourapi/tourapi.yml)
    python3 fetch_tourapi.py --out <폴더> --dry-run      # 받기만 하고 파일은 안 써요 (개수·매칭률 확인용)

흐름:
  1. 국문 서비스(KorService2)로 한국어 이름·한국어 주소·좌표를 받아요 (기사님께 보여주기·주소 복사에 필요).
  2. 다국어 서비스(영·일·중간·중번·독·불·스·러)에서 같은 곳을 좌표(50m 안)로 찾아 그 언어 이름·주소·소개를 붙여요.
     관광공사 다국어 서비스는 국문과 콘텐츠 ID 가 달라서 ID 로는 짝을 못 지어요.
  3. 안전장치: 받은 개수가 지난번의 절반도 안 되거나 오류가 나면 그 파일은 쓰지 않아요 (이전 파일 유지).

1시간마다 돌리기 때문에 호출 수를 아껴요 (공공데이터포털 개발계정은 서비스마다 하루 1,000회):
  - 시군구 번호(법정동코드)는 한 번 실행 안에서 한 번만 물어봐요.
  - 행사 소개글은 지난번 파일에 이미 있으면 다시 부르지 않아요 (새 행사만).
  - 한 번 실행에 서비스마다 최대 MAX_CALLS_PER_SERVICE 번까지만 불러요. 넘으면 멈추고 이전 파일을 그대로 둬요.
  → 보통 한 번에 서비스마다 30회 안팎 × 24번 = 하루 700~800회.

⚠️ [키 받은 뒤 확인] 아래 CAFE_CLASS_CODES, 법정동 시군구 이름, 다국어 서비스의 콘텐츠 타입 번호는
   공식 문서·실제 응답으로 한 번 확인해야 해요. --dry-run 으로 개수와 매칭률을 보고 고쳐요.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

API_ROOT = "https://apis.data.go.kr/B551011"
SOURCE_NAME = "한국관광공사 TourAPI"
KST = dt.timezone(dt.timedelta(hours=9))

# 앱 콘텐츠 언어 키(AppLanguage.contentKey) → 관광공사 서비스 이름
SERVICES: dict[str, str] = {
    "ko": "KorService2",
    "en": "EngService2",
    "ja": "JpnService2",
    "zh": "ChsService2",       # 간체 (앱 키가 예전대로 "zh")
    "zh-Hant": "ChtService2",  # 번체
    "de": "GerService2",
    "fr": "FreService2",
    "es": "SpnService2",
    "ru": "RusService2",
}

# 콘텐츠 타입 번호: 국문과 다국어 서비스가 달라요. [키 받은 뒤 확인]
FOOD_TYPE = {"ko": "39", "foreign": "82"}
FESTIVAL_TYPE = {"ko": "15", "foreign": "85"}

# 카페로 볼 분류 코드. 새 분류체계(lclsSystm3)와 옛 분류(cat3) 둘 다 봐요. [키 받은 뒤 확인]
CAFE_CLASS_CODES = {"FD050100", "FD0501", "A05020900"}

# 도시 → (법정동 시도 코드, 포함할 시군구 이름들). 시군구가 비어 있으면 시도 전체.
CITIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "seoul": ("11", ()),
    "busan": ("26", ()),
    "daegu": ("27", ()),
    "incheon": ("28", ()),
    "jeju": ("50", ()),
    "gyeongju": ("47", ("경주시",)),
    "gangneung": ("51", ("강릉시",)),
    "jeonju": ("52", ("전주시", "전주시 완산구", "전주시 덕진구")),
}

# 1시간마다 실행할 때 서비스(언어)마다 한 번에 부를 수 있는 최대 횟수.
# 24번 × 40 = 960 < 개발계정 하루 1,000회. 활용사례를 등록해 운영계정으로 올리면 넉넉해져요.
MAX_CALLS_PER_SERVICE = 40
PLACE_SCAN_LIMIT = 200           # 도시·지역마다 훑어볼 음식점 수 (100개씩 2쪽)
FESTIVAL_SCAN_LIMIT = 100        # 도시·지역마다 훑어볼 행사 수 (1쪽)
MAX_FOOD_PER_CITY = 60
MAX_CAFE_PER_CITY = 30
MAX_EVENTS_PER_CITY = 30
EVENT_WINDOW_DAYS = 120          # 오늘부터 이 기간 안에 시작하는 행사만
MATCH_DISTANCE_M = 50            # 다국어 항목을 같은 곳으로 볼 거리
SAFETY_RATIO = 0.5               # 지난번 개수의 이 비율보다 적으면 쓰지 않음
OPEN_IMAGE_TYPES = {"Type1", "Type3"}  # 공공누리 1·3유형만 사진 사용

Fetcher = Callable[[str, dict], dict]


class TourAPIError(RuntimeError):
    pass


# MARK: - 네트워크

def http_fetcher(key: str, pause: float = 0.1) -> Fetcher:
    """실제 API 를 부르는 함수. 테스트에서는 가짜 함수로 바꿔 끼워요."""

    def fetch(path: str, params: dict) -> dict:
        query = {"serviceKey": key, "MobileOS": "ETC", "MobileApp": "KSafeTrip", "_type": "json", **params}
        url = f"{API_ROOT}/{path}?{urllib.parse.urlencode(query)}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    body = response.read().decode("utf-8")
                time.sleep(pause)  # 초당 호출 제한을 넘지 않게 조금 쉬어요
                try:
                    return json.loads(body)
                except json.JSONDecodeError as error:
                    # 키 오류 등은 XML 로 돌아와요. 앞부분만 보여 줘요 (키가 찍히지 않게 URL 은 안 보여 줌).
                    raise TourAPIError(f"{path}: JSON 이 아닌 응답 {body[:200]!r}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                time.sleep(2 * (attempt + 1))
        raise TourAPIError(f"{path}: 네트워크 오류 {last_error}")

    return fetch


class BudgetedFetcher:
    """호출 수를 서비스(언어)별로 세다가, 한도를 넘으면 멈춰요 (하루 트래픽 한도를 지키려고)."""

    def __init__(self, fetch: Fetcher, limit: int = MAX_CALLS_PER_SERVICE):
        self.fetch = fetch
        self.limit = limit
        self.counts: dict[str, int] = {}

    def __call__(self, path: str, params: dict) -> dict:
        service = path.split("/", 1)[0]
        self.counts[service] = self.counts.get(service, 0) + 1
        if self.counts[service] > self.limit:
            raise TourAPIError(f"{service}: 이번 실행 호출 한도({self.limit}회)를 넘어서 멈췄어요")
        return self.fetch(path, params)


def items_of(response: dict) -> tuple[list[dict], int]:
    """응답에서 항목 목록과 전체 개수를 꺼내요. 결과가 없으면 items 가 빈 문자열로 와요."""
    body = response.get("response", {})
    header = body.get("header", {})
    if header.get("resultCode") not in (None, "0000", "00"):
        raise TourAPIError(f"API 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
    content = body.get("body", {})
    raw = content.get("items") or {}
    items = raw.get("item", []) if isinstance(raw, dict) else []
    if isinstance(items, dict):  # 하나뿐이면 배열이 아니라 객체로 와요
        items = [items]
    return items, int(content.get("totalCount") or 0)


def fetch_all(fetch: Fetcher, path: str, params: dict, limit: int, page_size: int = 100) -> list[dict]:
    collected: list[dict] = []
    page = 1
    while len(collected) < limit:
        items, total = items_of(fetch(path, {**params, "numOfRows": page_size, "pageNo": page}))
        collected.extend(items)
        if not items or len(collected) >= total:
            break
        page += 1
    return collected[:limit]


# MARK: - 변환

def to_day(value: str | None) -> str | None:
    """"20261103" 또는 "20261103120000" → "2026-11-03"."""
    if not value or len(value) < 8 or not value[:8].isdigit():
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def coordinate(item: dict) -> tuple[float, float] | None:
    try:
        lat, lon = float(item.get("mapy") or 0), float(item.get("mapx") or 0)
    except ValueError:
        return None
    if 32 < lat < 39.5 and 124 < lon < 132:
        return lat, lon
    return None


def address(item: dict) -> str:
    return " ".join(part.strip() for part in (item.get("addr1"), item.get("addr2")) if part and part.strip())


def image_url(item: dict) -> str | None:
    url = (item.get("firstimage") or "").strip()
    if not url or item.get("cpyrhtDivCd") not in OPEN_IMAGE_TYPES:
        return None
    return url.replace("http://", "https://", 1)


def is_cafe(item: dict) -> bool:
    codes = {item.get("lclsSystm3"), item.get("lclsSystm2"), item.get("cat3")}
    return bool(codes & CAFE_CLASS_CODES)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """두 좌표 사이 거리(미터). 50m 정도를 비교할 때는 간단한 평면 근사로 충분해요."""
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(lat)
    return math.hypot(dx, dy)


def base_record(item: dict, city: str, today: str) -> dict | None:
    coords = coordinate(item)
    name = (item.get("title") or "").strip()
    addr = address(item)
    if not name or not addr:
        return None
    record = {
        "id": f"tour-{item.get('contentid')}",
        "city": city,
        "nameKorean": name,
        "addressKorean": addr,
        "tel": (item.get("tel") or "").strip() or None,
        "latitude": coords[0] if coords else None,
        "longitude": coords[1] if coords else None,
        "imageURL": image_url(item),
        "localized": {},
        "updatedAt": to_day(item.get("modifiedtime")) or today,
    }
    return record


def attach_translation(records: list[dict], foreign: list[dict], language: str,
                       overviews: dict[str, str] | None = None,
                       matched_ids: dict[str, str] | None = None) -> int:
    """다국어 항목을 좌표로 가장 가까운 국문 항목에 붙여요. 붙인 개수를 돌려줘요.

    matched_ids 를 넘기면 "국문 id → 다국어 콘텐츠 id" 를 채워 줘요 (소개글을 나중에 부를 때 써요).
    """
    matched = 0
    for item in foreign:
        coords = coordinate(item)
        name = (item.get("title") or "").strip()
        if not coords or not name:
            continue
        best, best_distance = None, MATCH_DISTANCE_M
        for record in records:
            if record["latitude"] is None or language in record["localized"]:
                continue
            gap = distance_m(coords, (record["latitude"], record["longitude"]))
            if gap <= best_distance:
                best, best_distance = record, gap
        if best is None:
            continue
        overview = (overviews or {}).get(str(item.get("contentid")))
        best["localized"][language] = {"name": name, "address": address(item) or None, "overview": overview}
        if matched_ids is not None:
            matched_ids[best["id"]] = str(item.get("contentid"))
        matched += 1
    return matched


def clean_overview(text: str | None) -> str | None:
    """소개글의 HTML 태그(<br> 등)를 지우고 너무 길면 잘라요."""
    if not text:
        return None
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:600] or None


# MARK: - 도시별 수집

_region_cache: dict[tuple[str, str], list[dict]] = {}


def region_params(fetch: Fetcher, service: str, city: str) -> list[dict]:
    """도시 → API 지역 조건 목록. 시군구 이름은 법정동코드조회로 번호를 찾아요 (실행마다 한 번만)."""
    cached = _region_cache.get((service, city))
    if cached is not None:
        return cached
    region, district_names = CITIES[city]
    if not district_names:
        return [{"lDongRegnCd": region}]
    items, _ = items_of(fetch(f"{service}/ldongCode2", {"lDongRegnCd": region, "numOfRows": 300, "pageNo": 1}))
    codes = []
    for item in items:
        name = (item.get("lDongSignguNm") or item.get("name") or "").strip()
        code = item.get("lDongSignguCd") or item.get("code")
        if name in district_names and code:
            codes.append({"lDongRegnCd": region, "lDongSignguCd": str(code)})
    if not codes:
        raise TourAPIError(f"{city}: 시군구 코드를 찾지 못했어요 ({district_names})")
    _region_cache[(service, city)] = codes
    return codes


def collect_places(fetch: Fetcher, today: str, log: Callable[[str], None]) -> list[dict]:
    places: list[dict] = []
    for city in CITIES:
        records: list[dict] = []
        regions = region_params(fetch, SERVICES["ko"], city)
        for region in regions:
            items = fetch_all(fetch, f"{SERVICES['ko']}/areaBasedList2",
                              {**region, "contentTypeId": FOOD_TYPE["ko"], "arrange": "Q"}, limit=PLACE_SCAN_LIMIT)
            for item in items:
                record = base_record(item, city, today)
                if record is None or record["latitude"] is None:
                    continue
                record["kind"] = "cafe" if is_cafe(item) else "food"
                records.append(record)
        food = [r for r in records if r["kind"] == "food"][:MAX_FOOD_PER_CITY]
        cafes = [r for r in records if r["kind"] == "cafe"][:MAX_CAFE_PER_CITY]
        chosen = food + cafes
        for language, service in SERVICES.items():
            if language == "ko":
                continue
            foreign: list[dict] = []
            for region in region_params(fetch, service, city):
                foreign += fetch_all(fetch, f"{service}/areaBasedList2",
                                     {**region, "contentTypeId": FOOD_TYPE["foreign"]}, limit=PLACE_SCAN_LIMIT)
            matched = attach_translation(chosen, foreign, language)
            log(f"  {city} {language}: 음식점 {len(foreign)}개 중 {matched}개 매칭")
        log(f"{city}: 맛집 {len(food)} · 카페 {len(cafes)}")
        places += chosen
    return places


def collect_events(fetch: Fetcher, today: str, log: Callable[[str], None],
                   previous: dict[str, dict] | None = None) -> list[dict]:
    """previous: 지난번 events.json 의 id → 항목. 이미 받은 소개글은 다시 부르지 않아요."""
    previous = previous or {}

    def cached_overview(record_id: str, language: str) -> tuple[bool, str | None]:
        texts = previous.get(record_id, {}).get("localized", {})
        if language in texts:
            return True, texts[language].get("overview")
        return False, None

    start = dt.date.fromisoformat(today)
    last_start = (start + dt.timedelta(days=EVENT_WINDOW_DAYS)).isoformat()
    # 이미 시작했지만 아직 안 끝난 행사도 받으려고 30일 전부터 찾아요.
    search_from = (start - dt.timedelta(days=30)).strftime("%Y%m%d")
    events: list[dict] = []
    for city in CITIES:
        records: list[dict] = []
        for region in region_params(fetch, SERVICES["ko"], city):
            items = fetch_all(fetch, f"{SERVICES['ko']}/searchFestival2",
                              {**region, "eventStartDate": search_from, "arrange": "A"}, limit=FESTIVAL_SCAN_LIMIT)
            for item in items:
                record = base_record(item, city, today)
                begin, end = to_day(item.get("eventstartdate")), to_day(item.get("eventenddate"))
                if record is None or not begin or not end or end < today or begin > last_start:
                    continue
                record["startDate"], record["endDate"] = begin, end
                records.append(record)
        records.sort(key=lambda r: (r["startDate"], r["nameKorean"]))
        records = records[:MAX_EVENTS_PER_CITY]
        # 한국어 소개글 (행사만: 장소는 호출 수가 너무 많아져요)
        for record in records:
            known, overview = cached_overview(record["id"], "ko")
            if not known:
                overview = fetch_overview(fetch, SERVICES["ko"], record["id"].removeprefix("tour-"))
            record["localized"]["ko"] = {"name": record["nameKorean"], "address": record["addressKorean"], "overview": overview}
        for language, service in SERVICES.items():
            if language == "ko":
                continue
            foreign: list[dict] = []
            for region in region_params(fetch, service, city):
                foreign += fetch_all(fetch, f"{service}/searchFestival2",
                                     {**region, "eventStartDate": search_from}, limit=FESTIVAL_SCAN_LIMIT)
            # 먼저 이름·주소만 붙이고, 소개글은 지난번에 없던 것만 불러요 (호출 수 절약)
            matched_ids: dict[str, str] = {}
            matched = attach_translation(records, foreign, language, matched_ids=matched_ids)
            for record in records:
                if record["id"] not in matched_ids:
                    continue
                known, overview = cached_overview(record["id"], language)
                if not known:
                    overview = fetch_overview(fetch, service, matched_ids[record["id"]])
                record["localized"][language]["overview"] = overview
            log(f"  {city} {language}: 행사 {len(foreign)}개 중 {matched}개 매칭")
        log(f"{city}: 행사 {len(records)}")
        events += records
    return events


def fetch_overview(fetch: Fetcher, service: str, content_id: str) -> str | None:
    try:
        items, _ = items_of(fetch(f"{service}/detailCommon2", {"contentId": content_id}))
    except TourAPIError:
        return None  # 소개글 하나 실패로 전체를 멈추지 않아요
    return clean_overview(items[0].get("overview")) if items else None


# MARK: - 파일 쓰기 (안전장치)

def previous_items(path: Path, array: str) -> dict[str, dict]:
    """지난번 파일의 id → 항목 (없거나 깨졌으면 빈 사전)."""
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get(array, [])
        return {item["id"]: item for item in items if isinstance(item, dict) and "id" in item}
    except (OSError, json.JSONDecodeError):
        return {}


def previous_count(path: Path, array: str) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get(array, []))
    except (OSError, json.JSONDecodeError):
        return 0


def is_safe_to_write(new_count: int, old_count: int) -> bool:
    """새로 받은 개수가 지난번의 절반도 안 되면(예: API 장애로 일부만 옴) 쓰지 않아요."""
    if new_count == 0:
        return old_count == 0
    return old_count < 20 or new_count >= old_count * SAFETY_RATIO


def write_content(path: Path, array: str, items: list[dict], today: str, log: Callable[[str], None]) -> bool:
    old = previous_count(path, array)
    if not is_safe_to_write(len(items), old):
        log(f"⚠️ {path.name}: {len(items)}개 (지난번 {old}개) → 너무 적어서 쓰지 않아요")
        return False
    # 앱이 Codable 로 읽기 쉽게 null 값 필드는 그대로 두고, 순서를 고정해서 쓸데없는 변경을 줄여요.
    items = sorted(items, key=lambda r: (r["city"], r.get("kind", ""), r.get("startDate", ""), r["id"]))
    document = {"schemaVersion": 1, "updatedAt": today, "source": SOURCE_NAME, array: items}
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        old_doc = json.loads(path.read_text(encoding="utf-8"))
        # 날짜만 바뀌고 내용이 같으면 쓰지 않아요 (매일 쓸데없는 커밋 방지)
        if old_doc.get(array) == json.loads(text)[array]:
            log(f"{path.name}: 바뀐 내용 없음")
            return False
    path.write_text(text, encoding="utf-8")
    log(f"{path.name}: {len(items)}개 저장")
    return True


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="places.json / events.json 을 쓸 폴더")
    parser.add_argument("--dry-run", action="store_true", help="받기만 하고 파일은 쓰지 않아요")
    parser.add_argument("--only", choices=["places", "events"], help="한 종류만 받기")
    args = parser.parse_args(list(argv) if argv is not None else None)

    key = os.environ.get("TOURAPI_KEY", "").strip()
    if not key:
        print("TOURAPI_KEY 환경 변수가 없어요 (공공데이터포털 일반 인증키 Decoding 값)", file=sys.stderr)
        return 2
    out = Path(args.out)
    today = dt.datetime.now(KST).date().isoformat()
    fetch = BudgetedFetcher(http_fetcher(key))
    log = print
    failed = False
    previous_events = previous_items(out / "events.json", "events")
    collectors = {
        "places": lambda: collect_places(fetch, today, log),
        "events": lambda: collect_events(fetch, today, log, previous_events),
    }

    for kind in ("places", "events"):
        array = kind
        if args.only and args.only != kind:
            continue
        try:
            items = collectors[kind]()
        except TourAPIError as error:
            log(f"⚠️ {kind}: {error} → 이전 파일을 그대로 둬요")
            failed = True
            continue
        localized = sum(1 for item in items if any(k != "ko" for k in item["localized"]))
        log(f"{kind}: 총 {len(items)}개, 다국어 매칭 {localized}개")
        if not args.dry_run:
            write_content(out / f"{kind}.json", array, items, today, log)
    log("호출 수: " + ", ".join(f"{name} {count}" for name, count in sorted(fetch.counts.items())))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
