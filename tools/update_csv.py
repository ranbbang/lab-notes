"""종가를 CSV 에 이어붙인다. GitHub Actions 가 매일 돌린다.

    python tools/update_csv.py

소스·필드·행 검증·미확정 봉 판정 규칙은 이 데이터를 쓰는 다른 파이프라인과
반드시 맞춰야 한다 — 다르게 가면 배당조정 차이로 계산 결과가 조용히
달라진다. (그 파이프라인은 이 저장소 밖에 있어 import 할 수 없다.
복사본이다 — 한쪽을 고치면 다른 쪽도 같이 고친다.)

기준선(2010-03-11 시작)은 교체하지 않는다. 야후 range 는 최근 구간만 주므로
앞 구간은 CSV 에만 있다. 마지막 날짜 이후 행만 붙인다.

세 소스가 다 막히면 아무것도 쓰지 않고 종료코드 0 으로 끝낸다. 야후가
러너 IP 를 자주 막으므로 그걸로 워크플로를 빨간불로 만들지 않는다 — 그날치가
안 쌓일 뿐이고, 페이지가 "데이터 기준" 을 띄우므로 낡은 값을 최신으로
오인하지 않는다.

다만 '하루 못 받았다' 와 '몇 주째 못 받고 있다' 는 다르다. 후자는 소리를
내야 한다 — 공개 저장소의 스케줄 워크플로는 60일 무활동이면 GitHub 이
끄는데, 커밋이 0건인 채로 워크플로가 내내 초록불이면 아무도 모르는 사이
영구 정지된다. 그래서 --check-stale 을 따로 둔다(daily.yml 의 마지막 단계).

    python tools/update_csv.py --check-stale
"""
import argparse
import csv
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

#: 이 파일은 공개 저장소의 tools/ 에 놓인 것을 전제로 한다(publish.py 가
#: 거기로 복사한다). 비공개 저장소에서 직접 돌리면 없는 경로를 친다.
CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "series-a.csv"
UA = {"User-Agent": "Mozilla/5.0 (compatible; market-data-fetch/1.0)"}

#: 받아올 종목 코드. 가격 API 호출에 실제로 필요한 설정값이라 여기 남아
#: 있다 — 바꾸면 CSV_PATH 가 가리키는 시계열도 같이 바뀐다는 뜻이다.
TICKER = "SOXL"

ET_CLOSE_HOUR = 16          # 미국 정규장 마감 16:00 ET
SETTLE_BUFFER_MIN = 30      # 마감 직후 정정이 들어오므로 여유를 둔다

#: CSV 마지막 날짜가 이보다 더 낡으면 종료코드 1 을 낸다(--check-stale).
#: 평일 수로 센다 — 휴장일은 안 따지므로 넉넉히 잡는다. 추수감사절 주간처럼
#: 휴장일이 낀 주가 있어도 5거래일이면 거짓경보가 나지 않는다.
STALE_TRADING_DAYS = 5


def log(msg: str) -> None:
    print(msg, flush=True)


def valid_row(d: str, c: str) -> bool:
    """날짜 꼴과 양수 가격인지. 소스가 엉뚱한 걸 줘도 CSV 로 새지 않게 막는다."""
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return float(c) > 0
    except (ValueError, TypeError):
        return False


def _now_et() -> datetime:
    """미국 동부시간. 이 데이터를 쓰는 다른 파이프라인과 같은 계산이다.

    tz 데이터베이스에 기대지 않으려고 미국 서머타임 규칙(3월 둘째 일요일
    02:00 ~ 11월 첫째 일요일 02:00)을 직접 계산한다.
    """
    utc = datetime.now(timezone.utc)

    def nth_sunday(month: int, nth: int) -> int:
        first_wd = datetime(utc.year, month, 1, tzinfo=timezone.utc).weekday()   # 월=0 … 일=6
        return 1 + (6 - first_wd) % 7 + 7 * (nth - 1)

    dst_start = datetime(utc.year, 3, nth_sunday(3, 2), 7, tzinfo=timezone.utc)    # 02:00 EST
    dst_end = datetime(utc.year, 11, nth_sunday(11, 1), 6, tzinfo=timezone.utc)    # 02:00 EDT
    return utc + timedelta(hours=-4 if dst_start <= utc < dst_end else -5)


def last_settled_date(now_et: datetime | None = None) -> date:
    """정규장이 확실히 끝난 마지막 날짜. 이 데이터를 쓰는 다른 파이프라인과 같다.

    야후는 장이 열려 있는 동안에도(프리마켓 포함) 그날 캔들을 내려 준다.
    그 값은 '현재가' 지 종가가 아니다. 그대로 CSV 에 붙이면 append_new 의
    'd > last' 규칙상 다음날 크론도 그 행을 고치지 않아 장중 현재가가
    종가 자리에 영구히 박힌다 — daily.yml 의 workflow_dispatch 로 미 장중에
    손으로 한 번 돌리면 바로 그 상황이 된다.

    휴장일은 따지지 않는다 — 여기서는 상한선으로만 쓰이고, 휴장일에는
    애초에 받아 올 봉이 없기 때문이다.
    """
    et = now_et or _now_et()
    d = et.date()
    if (et.hour, et.minute) < (ET_CLOSE_HOUR, SETTLE_BUFFER_MIN):
        d -= timedelta(days=1)
    while d.weekday() >= 5:          # 주말이면 금요일까지 되돌린다
        d -= timedelta(days=1)
    return d


def drop_unsettled(rows: list[tuple[str, str]], cutoff: str | None = None
                   ) -> list[tuple[str, str]]:
    """확정 종가가 아닌 봉을 버린다. cutoff 는 마지막 확정일(YYYY-MM-DD)."""
    cutoff = cutoff or last_settled_date().isoformat()
    unsettled = [d for d, _ in rows if d > cutoff]
    if unsettled:
        log(f"미확정 봉 {len(unsettled)}개 버림 (마지막 확정일 {cutoff}): "
            + ", ".join(unsettled[:5]))
    return [(d, c) for d, c in rows if d <= cutoff]


def _yahoo(host: str, ticker: str) -> list[tuple[str, str]]:
    # range=3mo 다. 1mo 는 크론이 며칠 멈춘 뒤나 첫 배포에서 공백을 못 메운다.
    # append_new 가 마지막 날짜 이후만 걸러내므로 더 받아도 결과는 같다.
    url = (f"https://{host}/v8/finance/chart/{ticker}"
           "?range=3mo&interval=1d&events=div%2Csplit")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read().decode("utf-8"))
    res = j["chart"]["result"][0]
    # 분할만 반영된 원종가를 쓴다. adjclose(배당 조정)는 기존 CSV 와 어긋난다.
    closes = res["indicators"]["quote"][0]["close"]
    rows = []
    for ts, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append((d, f"{c:.4f}".rstrip("0").rstrip(".")))
    return rows


def fetch_yahoo(ticker: str) -> list[tuple[str, str]]:
    return _yahoo("query1.finance.yahoo.com", ticker)


def fetch_yahoo2(ticker: str) -> list[tuple[str, str]]:
    return _yahoo("query2.finance.yahoo.com", ticker)


def fetch_stooq(ticker: str) -> list[tuple[str, str]]:
    """예비 소스. 2026-08 현재 봇 검증 HTML 을 돌려준다 — CSV 가 아니면 실패시킨다."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        text = r.read().decode("utf-8", "replace")
    head = text.lstrip()[:200].lower()
    if "html" in ctype or head.startswith("<!doctype") or "<html" in head:
        raise RuntimeError("CSV 가 아니라 HTML 이 왔습니다 (봇 검증 페이지로 보입니다)")
    rows = [(rec["Date"], rec["Close"]) for rec in csv.DictReader(text.splitlines())
            if rec.get("Date") and rec.get("Close") not in (None, "", "N/A")]
    if not rows:
        raise RuntimeError("CSV 를 읽었지만 쓸 수 있는 행이 없습니다")
    return rows


DEFAULT_FETCHERS = [("yahoo", fetch_yahoo), ("yahoo2", fetch_yahoo2),
                    ("stooq", fetch_stooq)]


def append_new(csv_path: Path, rows: list[tuple[str, str]]) -> int:
    """마지막 날짜 이후의 정상 행만 붙이고 붙인 개수를 돌려준다."""
    text = csv_path.read_text(encoding="utf-8")
    existing = [ln for ln in text.strip().split("\n")[1:] if ln.strip()]
    last = existing[-1].split(",")[0] if existing else ""

    fresh = sorted({(d, c) for d, c in rows if valid_row(d, c) and d > last})
    if not fresh:
        return 0

    with csv_path.open("a", encoding="utf-8", newline="") as f:
        if not text.endswith("\n"):
            f.write("\n")
        for d, c in fresh:
            f.write(f"{d},{c}\n")
    return len(fresh)


def last_csv_date(csv_path: Path) -> str:
    """CSV 마지막 행의 날짜. 없으면 빈 문자열."""
    text = csv_path.read_text(encoding="utf-8")
    existing = [ln for ln in text.strip().split("\n")[1:] if ln.strip()]
    return existing[-1].split(",")[0] if existing else ""


def _weekdays_between(a: date, b: date) -> int:
    """a 다음날부터 b 까지의 평일 수. 휴장일은 안 따진다(상한선 용도)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def check_stale(csv_path=None, cutoff: str | None = None) -> int:
    """CSV 가 너무 낡았으면 종료코드 1. 조용한 정지를 시끄럽게 만든다.

    세 소스가 다 막히면 커밋이 안 생긴다(의도된 동작이다). 그런데 공개
    저장소의 스케줄 워크플로는 60일 무활동이면 GitHub 이 끄므로, 오래
    막히면 커밋 0건 → 크론 비활성화 → 되살릴 사람이 없으면 영구 정지다.
    그동안 워크플로는 내내 초록불이다. 여기서 빨간불을 켜 알림이 가게 한다.
    """
    csv_path = csv_path or CSV_PATH
    cutoff = cutoff or last_settled_date().isoformat()
    last = last_csv_date(csv_path)
    if not last:
        log(f"CSV 에 행이 없습니다: {csv_path}")
        return 1
    try:
        behind = _weekdays_between(datetime.strptime(last, "%Y-%m-%d").date(),
                                   datetime.strptime(cutoff, "%Y-%m-%d").date())
    except ValueError:
        log(f"CSV 마지막 날짜의 꼴이 이상합니다: {last!r}")
        return 1
    if behind > STALE_TRADING_DAYS:
        log(f"CSV 가 낡았습니다 — 마지막 {last}, 마지막 확정일 {cutoff} "
            f"(평일 {behind}일 뒤짐, 허용 {STALE_TRADING_DAYS}일). "
            "가격 소스가 오래 막혀 있습니다. 이대로 두면 커밋이 없어 크론이 "
            "60일 뒤 비활성화됩니다.")
        return 1
    log(f"CSV 최신 상태 — 마지막 {last} (평일 {behind}일 뒤짐)")
    return 0


def main(fetchers=None, csv_path=None, cutoff=None) -> int:
    fetchers = fetchers or DEFAULT_FETCHERS
    csv_path = csv_path or CSV_PATH

    rows: list[tuple[str, str]] = []
    for name, fn in fetchers:
        try:
            rows = fn(TICKER)
            if rows:
                log(f"가격 소스 {name} 에서 {len(rows)}행 수신")
                break
        except Exception as e:                                   # noqa: BLE001
            log(f"가격 소스 {name} 실패: {type(e).__name__}: {e}")

    if not rows:
        log("모든 소스가 막혔습니다 — 아무것도 쓰지 않고 끝냅니다")
        return 0

    # 미확정 봉을 먼저 버린다 — append_new 는 마지막 날짜 이후만 붙이므로
    # 여기서 안 거르면 장중 현재가가 종가 자리에 영구히 박힌다.
    rows = drop_unsettled(rows, cutoff)
    if not rows:
        log("확정된 봉이 없습니다 — 아무것도 쓰지 않고 끝냅니다")
        return 0

    n = append_new(csv_path, rows)
    log(f"추가 {n}행" if n else "새 종가 없음")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-stale", action="store_true",
                    help="받아오지 않고, CSV 가 너무 낡았으면 종료코드 1 을 낸다.")
    args = ap.parse_args()
    sys.exit(check_stale() if args.check_stale else main())
