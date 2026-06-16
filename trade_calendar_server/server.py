from __future__ import annotations

import calendar
import csv
import html
import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from datetime import date
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request
from urllib.parse import parse_qs, urlparse

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
CSV_PATH = DATA_DIR / "schedule.csv"
JSON_PATH = DATA_DIR / "schedule.json"
RAW_TEXT_PATH = DATA_DIR / "last_pdf_text.txt"
BOARD_URL_PATH = DATA_DIR / "board_url.txt"
PORT = 8080


@dataclass
class TradeEvent:
    date: str
    start: str
    end: str
    item: str
    target: str
    tag: str


TASK_WORDS = ["双边协商", "滚动撮合", "双边挂牌", "单边挂牌", "集中竞价"]
PARTICIPANT_MARKERS = ["发电企业、", "发电企业", "批发交易用户", "电网企业", "售电公司", "零售用户、", "零售用户"]


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def read_board_url() -> str:
    if not BOARD_URL_PATH.exists():
        return ""
    return BOARD_URL_PATH.read_text(encoding="utf-8").strip()


def save_board_url(board_url: str) -> str:
    board_url = board_url.strip().rstrip("/")
    if board_url and not board_url.startswith(("http://", "https://")):
        board_url = f"http://{board_url}"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BOARD_URL_PATH.write_text(board_url, encoding="utf-8")
    return board_url


def notify_board_refresh() -> str:
    board_url = read_board_url()
    if not board_url:
        return "开发板地址未设置，CSV 已更新但未自动刷新开发板"
    url = f"{board_url.rstrip('/')}/refresh"
    try:
        with request.urlopen(url, timeout=8) as response:
            status = response.status
            if 200 <= status < 300:
                return f"已通知开发板刷新：{url}"
            return f"开发板刷新请求返回 {status}：{url}"
    except OSError as exc:
        return f"开发板刷新失败：{exc}"


def read_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def tag_from_task(task: str) -> str:
    if "双边" in task:
        return "双"
    if "挂牌" in task:
        return "挂"
    if "集中" in task or "撮合" in task:
        return "集"
    return "交"


def clean_item_and_task(before_date: str) -> tuple[str, str]:
    item = before_date.strip()
    task = ""
    for word in TASK_WORDS:
        pos = item.find(word)
        if pos >= 0:
            task = word
            item = item[:pos].strip()
            break
    for marker in PARTICIPANT_MARKERS:
        pos = item.find(marker)
        if pos >= 0:
            item = item[:pos].strip()
    return re.sub(r"\s+", "", item), task


def parse_target(text_after_time: str, item: str, previous_target: str) -> str:
    if "标的日" in text_after_time:
        match = re.search(r"标的日\s*(\d{1,2})\s*月\s*(\d{1,2})(?:-(\d{1,2}))?\s*日", text_after_time)
        if match:
            month = int(match.group(1))
            start_day = int(match.group(2))
            end_day = int(match.group(3)) if match.group(3) else None
            if end_day:
                return f"2026-{month:02d}-{start_day:02d}~2026-{month:02d}-{end_day:02d}"
            return f"2026-{month:02d}-{start_day:02d}"

    if "标的月" in text_after_time:
        match = re.search(r"标的月\s*(\d{1,2})-(\d{1,2})\s*月", text_after_time)
        if match:
            return f"2026-{int(match.group(1)):02d}~2026-{int(match.group(2)):02d}"

    if item.startswith("多日"):
        return previous_target
    if item.startswith("月度绿电") and "合同转让" in item:
        return previous_target
    return ""


def parse_events_from_text(text: str) -> list[TradeEvent]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    row_start = re.compile(r"^(\d{1,3})\s+(.*)")
    starts: list[tuple[int, int]] = []

    for index, line in enumerate(lines):
        match = row_start.match(line)
        if not match:
            continue
        number = int(match.group(1))
        rest = match.group(2).strip()
        if not (1 <= number <= 200):
            continue
        if rest.startswith(":") or re.match(r"^(月|年)\s", rest):
            continue
        lookahead = " ".join(lines[index : min(index + 3, len(lines))])
        if not any(word in lookahead for word in ("交易", "零售", "其他电力")):
            continue
        starts.append((index, number))

    date_pattern = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
    time_pattern = re.compile(r"(\d{1,2}:\d{2})\s*[-至]\s*(\d{1,2}:\d{2})")
    events: list[TradeEvent] = []
    previous_target = ""

    for row_index, (start_index, _) in enumerate(starts):
        end_index = starts[row_index + 1][0] if row_index + 1 < len(starts) else len(lines)
        first = row_start.match(lines[start_index]).group(2).strip()
        block = " ".join([first] + lines[start_index + 1 : end_index])

        time_match = time_pattern.search(block)
        date_matches = list(date_pattern.finditer(block))
        if not time_match or not date_matches:
            continue

        dates_before_time = [match for match in date_matches if match.start() < time_match.start()]
        date_match = dates_before_time[-1] if dates_before_time else date_matches[0]
        month, day = int(date_match.group(1)), int(date_match.group(2))
        trade_date = f"2026-{month:02d}-{day:02d}"
        start_time, end_time = time_match.group(1), time_match.group(2)

        item, task = clean_item_and_task(block[: date_match.start()])
        if not item or item == "其他电力市场化交易":
            continue

        target = parse_target(block[time_match.end() :], item, previous_target)
        if target:
            previous_target = target

        events.append(TradeEvent(trade_date, start_time, end_time, item, target, tag_from_task(task)))

    return events


def write_outputs(events: list[TradeEvent]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    events = sorted(events, key=lambda event: (event.date, event.start, event.end, event.item, event.target))
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["date", "start", "end", "item", "target", "tag"])
        writer.writeheader()
        for event in events:
            writer.writerow(asdict(event))
    JSON_PATH.write_text(json.dumps([asdict(event) for event in events], ensure_ascii=False, indent=2), encoding="utf-8")


def row_to_event(row: dict[str, str]) -> TradeEvent:
    return TradeEvent(
        date=row.get("date", ""),
        start=row.get("start", ""),
        end=row.get("end", ""),
        item=row.get("item", ""),
        target=row.get("target", ""),
        tag=row.get("tag", "") or "交",
    )


def merge_events_by_month(new_events: list[TradeEvent]) -> list[TradeEvent]:
    if not new_events:
        return [row_to_event(row) for row in load_events()]

    months = {event.date[:7] for event in new_events if len(event.date) >= 7}
    existing = [
        row_to_event(row)
        for row in load_events()
        if row.get("date", "")[:7] not in months
    ]

    seen: set[tuple[str, str, str, str, str]] = set()
    merged: list[TradeEvent] = []
    for event in existing + new_events:
        key = (event.date, event.start, event.end, event.item, event.target)
        if key in seen:
            continue
        seen.add(key)
        merged.append(event)
    return merged


def load_events() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def event_short_title(event: dict[str, str]) -> str:
    target = event.get("target", "")
    item = event.get("item", "")
    if target:
        return f"{target} {item}"
    return item


def render_badges(day_events: list[dict[str, str]], selected: bool) -> str:
    if not day_events:
        return ""
    badges = []
    for event in day_events[:7]:
        tag = event.get("tag") or "交"
        cls = "badge selected-badge" if selected else "badge"
        badges.append(f"<span class='{cls}'>{html.escape(tag)}</span>")
    return "<div class='badges'>" + "".join(badges) + "</div>"


def render_calendar(rows: list[dict[str, str]], year: int, month: int, selected_day: int) -> str:
    by_date: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_date.setdefault(row.get("date", ""), []).append(row)

    cal = calendar.Calendar(firstweekday=0)
    cells = []
    for day in cal.itermonthdates(year, month):
        date_key = day.isoformat()
        day_events = by_date.get(date_key, [])
        in_month = day.month == month
        selected = in_month and day.day == selected_day
        classes = ["day-cell"]
        if not in_month:
            classes.append("muted")
        if selected:
            classes.append("selected")
        if day_events:
            classes.append("has-events")
        href = f"/?month={year}-{month:02d}&day={day.day}" if in_month else "#"
        cells.append(
            f"<a class='{' '.join(classes)}' href='{href}'>"
            f"<span class='day-num'>{day.day:02d}</span>"
            f"{render_badges(day_events, selected)}"
            "</a>"
        )
    return "".join(cells)


def render_side_panel(rows: list[dict[str, str]], year: int, month: int, selected_day: int) -> str:
    selected_date = f"{year:04d}-{month:02d}-{selected_day:02d}"
    events = [row for row in rows if row.get("date") == selected_date]
    cards = []
    for event in events:
        cards.append(
            "<div class='trade-card'>"
            f"<div><span class='red-dot'>{html.escape(event.get('tag') or '交')}</span>"
            f"<strong>{html.escape(event_short_title(event))}</strong></div>"
            f"<div class='time'>申报时间:{html.escape(event.get('date', ''))} "
            f"{html.escape(event.get('start', ''))}-{html.escape(event.get('end', ''))}</div>"
            "</div>"
        )
    if not cards:
        cards.append("<div class='trade-card empty'>当天没有交易安排</div>")
    return "".join(cards)


def current_month_day(rows: list[dict[str, str]], query: dict[str, list[str]]) -> tuple[int, int, int]:
    today = date.today()
    year, month, day = today.year, today.month, today.day
    if rows:
        first = rows[0].get("date", "")
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", first)
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            day = 1
    if "month" in query:
        match = re.match(r"(\d{4})-(\d{2})", query["month"][0])
        if match:
            year, month = int(match.group(1)), int(match.group(2))
    if "day" in query:
        day = max(1, min(31, int(query["day"][0])))
    day = min(day, calendar.monthrange(year, month)[1])
    return year, month, day


def render_page(message: str = "", query: dict[str, list[str]] | None = None) -> bytes:
    query = query or {}
    ip = local_ip()
    board_url = read_board_url()
    board_url_value = html.escape(board_url, quote=True)
    rows = load_events()
    year, month, selected_day = current_month_day(rows, query)
    selected_date = f"{year:04d}年{month:02d}月"
    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>交易日历处理系统</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f1f4f8; color: #26313d; }}
    .topbar {{ height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background: #fff; border-top: 6px solid #0d57b7; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
    .home {{ border: 1px solid #3b8cff; color: #2f80ed; background: #edf5ff; padding: 8px 22px; border-radius: 5px; font-weight: 700; }}
    .unit {{ font-weight: 700; }}
    .unit span {{ color: #3088ff; }}
    .wrap {{ display: grid; grid-template-columns: minmax(760px, 1fr) 410px; gap: 16px; padding: 18px; }}
    .main {{ background: #fff; padding: 14px; border-radius: 4px; }}
    .controls {{ display: flex; align-items: center; gap: 10px; height: 46px; }}
    .controls strong {{ margin-right: 8px; color: #6b7480; }}
    .month-box {{ border: 1px solid #d8e0ea; border-radius: 5px; padding: 8px 14px; background: #fafcff; min-width: 126px; text-align: center; }}
    .month-nav {{ color: #2f80ed; text-decoration: none; padding: 4px 8px; }}
    .week-row, .calendar {{ display: grid; grid-template-columns: repeat(7, 1fr); }}
    .week-row div {{ background: #eee; color: #5f6b78; font-weight: 700; text-align: center; padding: 12px 0; border-right: 1px solid #e7edf5; }}
    .day-cell {{ position: relative; display: block; height: 158px; background: #fff; border-right: 1px solid #e7edf5; border-bottom: 1px solid #e7edf5; text-decoration: none; color: #2f3742; padding: 12px; }}
    .day-cell:hover {{ background: #f8fbff; }}
    .day-cell.muted {{ color: #bdc5d0; background: #fff; }}
    .day-cell.selected {{ background: #eef6ff; }}
    .day-num {{ position: absolute; right: 12px; top: 12px; font-weight: 700; }}
    .badges {{ position: absolute; left: 50%; transform: translateX(-50%); bottom: 18px; white-space: nowrap; display: flex; gap: 9px; }}
    .badge {{ display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 50%; background: #cfcfcf; color: #777; font-size: 13px; font-weight: 700; }}
    .selected-badge {{ background: #f14b37; color: #fff; }}
    .side {{ background: #3493f6; border-radius: 6px; padding: 12px 14px; color: #fff; }}
    .date-card {{ text-align: center; background: rgba(255,255,255,.10); border-radius: 5px; padding: 16px; margin-bottom: 12px; }}
    .date-card .big {{ font-size: 38px; font-weight: 800; }}
    .date-card .month {{ font-size: 22px; font-weight: 700; border-top: 1px solid rgba(255,255,255,.22); padding-top: 8px; margin-top: 8px; }}
    .trade-card {{ background: rgba(255,255,255,.15); border-radius: 4px; padding: 14px 10px; margin: 12px 0; border-bottom: 2px solid rgba(0,83,180,.25); font-size: 16px; line-height: 1.55; }}
    .trade-card.empty {{ text-align: center; padding: 28px 10px; }}
    .red-dot {{ display: inline-flex; align-items: center; justify-content: center; width: 25px; height: 25px; border-radius: 50%; background: #fb4e3a; margin-right: 8px; font-size: 13px; font-weight: 800; }}
    .time {{ margin-top: 8px; font-weight: 700; }}
    .upload {{ background: #fff; border-radius: 4px; padding: 14px; margin-top: 16px; }}
    .upload form {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    button {{ padding: 8px 16px; border: 0; background: #2f80ed; color: #fff; border-radius: 4px; cursor: pointer; }}
    code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; }}
    .ok {{ color: #0f766e; font-weight: 700; margin: 8px 0; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="home">首页</div>
    <div class="unit">单位：<span>兆瓦时，元/兆瓦时</span></div>
  </div>
  <div class="wrap">
    <div>
      <div class="main">
        <div class="controls">
          <strong>月份</strong>
          <a class="month-nav" href="/?month={prev_year}-{prev_month:02d}&day=1">上一月</a>
          <div class="month-box">{year}-{month:02d}</div>
          <a class="month-nav" href="/?month={next_year}-{next_month:02d}&day=1">下一月</a>
        </div>
        <div class="week-row"><div>一</div><div>二</div><div>三</div><div>四</div><div>五</div><div>六</div><div>日</div></div>
        <div class="calendar">{render_calendar(rows, year, month, selected_day)}</div>
      </div>
      <div class="upload">
        {"<p class='ok'>" + html.escape(message) + "</p>" if message else ""}
        <form method="POST" action="/upload" enctype="multipart/form-data">
          <strong>上传交易安排 PDF</strong>
          <input type="file" name="pdf" accept="application/pdf,.pdf" required>
          <button type="submit">上传并转换</button>
          <span>开发板读取：<code>http://{ip}:{PORT}/schedule.csv</code></span>
          <a href="/schedule.csv">下载 CSV</a>
        </form>
      </div>
    </div>
    <aside class="side">
      <div class="date-card">
        <div class="big">{selected_day} 日</div>
        <div class="month">{selected_date}</div>
      </div>
      {render_side_panel(rows, year, month, selected_day)}
    </aside>
  </div>
</body>
</html>"""
    return page.encode("utf-8")


def render_side_panel(rows: list[dict[str, str]], year: int, month: int, selected_day: int) -> str:
    selected_date = f"{year:04d}-{month:02d}-{selected_day:02d}"
    events = [row for row in rows if row.get("date") == selected_date]
    if not events:
        return """
        <div class="empty-state">
          <div class="empty-title">当天无交易安排</div>
          <div class="empty-sub">选择有标记的日期查看交易窗口</div>
        </div>
        """

    cards = []
    for event in events:
        tag = event.get("tag") or "交"
        item = event.get("item", "")
        target = event.get("target", "") or "未识别"
        start = event.get("start", "")
        end = event.get("end", "")
        cards.append(
            "<article class='trade-card'>"
            f"<div class='card-head'><span class='trade-tag'>{html.escape(tag)}</span>"
            f"<span class='trade-name'>{html.escape(item)}</span></div>"
            "<div class='trade-meta'>"
            f"<div class='meta-block'><span>申报时间</span><strong>{html.escape(start)}-{html.escape(end)}</strong></div>"
            f"<div class='meta-block'><span>交易标的</span><strong>{html.escape(target)}</strong></div>"
            "</div>"
            "</article>"
        )
    return "".join(cards)


def render_page(message: str = "", query: dict[str, list[str]] | None = None) -> bytes:
    query = query or {}
    ip = local_ip()
    board_url = read_board_url()
    board_url_value = html.escape(board_url, quote=True)
    rows = load_events()
    year, month, selected_day = current_month_day(rows, query)
    selected_date = f"{year:04d}年{month:02d}月"
    selected_iso = f"{year:04d}-{month:02d}-{selected_day:02d}"
    selected_count = sum(1 for row in rows if row.get("date") == selected_iso)
    month_count = sum(1 for row in rows if row.get("date", "").startswith(f"{year:04d}-{month:02d}-"))
    month_days = len({row.get("date", "") for row in rows if row.get("date", "").startswith(f"{year:04d}-{month:02d}-")})
    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>广西电力交易日历</title>
  <style>
    * {{ box-sizing: border-box; }}
    :root {{
      --bg: #eef3f8;
      --panel: #ffffff;
      --line: #dce5ee;
      --ink: #1e2a36;
      --muted: #728094;
      --blue: #1f7ae0;
      --blue-dark: #1459ae;
      --blue-soft: #eaf3ff;
      --red: #ef4b3a;
      --green: #12856f;
      --shadow: 0 16px 44px rgba(25, 48, 80, .10);
    }}
    body {{
      margin: 0;
      min-width: 1180px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: radial-gradient(circle at 20% -10%, #ffffff 0, #eef3f8 34%, #e8eef6 100%);
      color: var(--ink);
      letter-spacing: 0;
    }}
    a {{ color: inherit; }}
    .topbar {{
      height: 64px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 28px;
      background: rgba(255,255,255,.94);
      border-top: 5px solid var(--blue-dark);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    .brand {{ display: flex; align-items: center; gap: 12px; font-weight: 800; }}
    .brand-mark {{
      width: 36px; height: 36px; border-radius: 6px;
      background: var(--blue-dark); color: #fff;
      display: grid; place-items: center; font-weight: 900;
    }}
    .brand small {{ display: block; color: var(--muted); font-weight: 600; margin-top: 2px; }}
    .unit {{ display: flex; gap: 18px; align-items: center; color: var(--muted); font-weight: 700; }}
    .unit strong {{ color: var(--blue-dark); }}
    .page {{ padding: 20px 24px 26px; }}
    .shell {{
      display: grid;
      grid-template-columns: minmax(760px, 1fr) 390px;
      gap: 18px;
      align-items: start;
    }}
    .panel {{
      background: rgba(255,255,255,.96);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      align-items: center;
      padding: 16px;
      margin-bottom: 14px;
    }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .stat {{
      min-width: 116px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfdff;
    }}
    .stat span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 700; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 20px; }}
    .upload {{ display: grid; gap: 8px; justify-items: end; }}
    .upload form {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }}
    .upload input[type=file] {{
      max-width: 210px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px;
      background: #fff;
    }}
    .upload input[type=url], .upload input[type=text] {{
      width: 240px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
      font-weight: 700;
      color: #24364a;
    }}
    button {{
      border: 0;
      background: var(--blue);
      color: #fff;
      padding: 9px 14px;
      border-radius: 6px;
      font-weight: 800;
      cursor: pointer;
    }}
    .device-url {{ width: 100%; color: var(--muted); font-size: 12px; text-align: right; }}
    code {{ background: #edf3f9; color: #305174; padding: 2px 5px; border-radius: 4px; }}
    .ok {{ margin: 0 0 10px; color: var(--green); font-weight: 800; }}
    .calendar-panel {{ padding: 10px 12px 12px; }}
    .calendar-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 40px;
      border-bottom: 1px solid var(--line);
      margin-bottom: 8px;
    }}
    .month-title {{ font-size: 20px; font-weight: 900; }}
    .month-navs {{ display: flex; align-items: center; gap: 8px; }}
    .month-nav {{
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 10px;
      color: #35536f;
      background: #fbfdff;
      font-weight: 800;
    }}
    .week-row, .calendar {{ display: grid; grid-template-columns: repeat(7, 1fr); }}
    .week-row div {{
      background: #f3f6fa;
      color: #516176;
      text-align: center;
      padding: 8px 0;
      border-right: 1px solid var(--line);
      font-weight: 900;
    }}
    .day-cell {{
      position: relative;
      display: block;
      height: 108px;
      background: #fff;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      text-decoration: none;
      color: var(--ink);
      padding: 8px;
      transition: background .12s ease, box-shadow .12s ease;
    }}
    .day-cell:hover {{ background: #f8fbff; box-shadow: inset 0 0 0 2px #cfe3ff; }}
    .day-cell.muted {{ color: #b7c1cd; background: #fbfcfe; }}
    .day-cell.selected {{ background: var(--blue-soft); box-shadow: inset 0 0 0 2px var(--blue); }}
    .day-num {{ position: absolute; right: 9px; top: 8px; font-weight: 900; font-size: 14px; }}
    .badges {{
      position: absolute;
      left: 8px;
      right: 8px;
      bottom: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 21px;
      height: 21px;
      border-radius: 50%;
      background: #d8dde4;
      color: #657184;
      font-size: 12px;
      font-weight: 900;
    }}
    .selected-badge {{ background: var(--red); color: #fff; }}
    .side {{
      overflow: hidden;
      background: linear-gradient(180deg, #2f8af0, #1c6ed0);
      color: #fff;
      border: 0;
      position: sticky;
      top: 84px;
    }}
    .date-card {{
      padding: 20px 20px 18px;
      border-bottom: 1px solid rgba(255,255,255,.18);
      background: rgba(255,255,255,.10);
    }}
    .date-card .big {{ font-size: 46px; line-height: 1; font-weight: 950; text-align: center; }}
    .date-card .month {{ margin-top: 10px; text-align: center; font-size: 20px; font-weight: 800; }}
    .side-summary {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      padding: 12px 14px;
      border-bottom: 1px solid rgba(255,255,255,.18);
    }}
    .side-chip {{
      background: rgba(255,255,255,.14);
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 6px;
      padding: 9px 10px;
      font-weight: 800;
    }}
    .side-chip span {{ display: block; font-size: 12px; opacity: .82; margin-bottom: 4px; }}
    .trade-list {{ padding: 8px 14px 14px; }}
    .trade-card {{
      background: rgba(255,255,255,.14);
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 6px;
      padding: 12px;
      margin: 10px 0;
      line-height: 1.45;
    }}
    .card-head {{ display: flex; align-items: flex-start; gap: 8px; }}
    .trade-name {{ font-size: 16px; font-weight: 900; }}
    .red-dot {{
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      background: var(--red);
      font-size: 13px;
      font-weight: 900;
    }}
    .time-row, .target-row {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-top: 9px;
      font-size: 14px;
    }}
    .time-row span, .target-row span {{ opacity: .78; }}
    .time-row strong, .target-row strong {{ text-align: right; }}
    .empty-state {{ padding: 34px 16px; text-align: center; opacity: .95; }}
    .empty-title {{ font-size: 18px; font-weight: 900; }}
    .empty-sub {{ margin-top: 8px; opacity: .78; }}
    @media (max-width: 1220px) {{
      body {{ min-width: 0; }}
      .shell {{ grid-template-columns: 1fr; }}
      .side {{ position: static; }}
    }}
    body {{
      background:
        linear-gradient(180deg, #f7f9fc 0, #edf2f7 100%);
    }}
    .topbar {{
      height: 58px;
      border-top: 0;
      padding: 0 30px;
      background: rgba(255,255,255,.92);
      backdrop-filter: blur(12px);
      box-shadow: 0 1px 0 rgba(18, 35, 56, .08);
    }}
    .brand {{ gap: 10px; color: #172337; }}
    .brand-mark {{
      width: 30px;
      height: 30px;
      border-radius: 9px;
      background: linear-gradient(135deg, #125bb8, #2290f2);
      box-shadow: 0 8px 18px rgba(31, 122, 224, .22);
    }}
    .brand small {{ color: #7a8797; font-size: 12px; }}
    .unit {{ font-size: 14px; }}
    .page {{ padding: 18px 24px 24px; }}
    .panel {{
      border: 1px solid #e1e8f0;
      border-radius: 12px;
      box-shadow: 0 18px 50px rgba(29, 47, 73, .08);
    }}
    .toolbar {{
      min-height: 70px;
      padding: 12px 16px;
      margin-bottom: 14px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.96), rgba(248,251,254,.96));
    }}
    .stats {{
      gap: 8px;
    }}
    .stat {{
      min-width: 96px;
      padding: 8px 10px;
      border-color: #e8eef5;
      border-radius: 999px;
      background: rgba(255,255,255,.88);
    }}
    .stat span {{ font-size: 11px; letter-spacing: .1px; }}
    .stat strong {{ margin-top: 3px; font-size: 18px; color: #142338; }}
    .upload input[type=file] {{ border-radius: 9px; }}
    button {{
      border-radius: 9px;
      background: linear-gradient(135deg, #1768c9, #238cf0);
      box-shadow: 0 8px 18px rgba(31, 122, 224, .18);
    }}
    .month-nav {{
      border-radius: 9px;
      background: #fff;
      color: #29445f;
    }}
    .month-nav:hover {{ border-color: #b9d5f5; color: #1768c9; }}
    .shell {{
      grid-template-columns: minmax(760px, 1fr) 340px;
      gap: 16px;
    }}
    .calendar-panel {{
      padding: 12px;
      overflow: hidden;
      height: calc(100vh - 214px);
      display: flex;
      flex-direction: column;
    }}
    .calendar-head {{
      height: 38px;
      margin-bottom: 8px;
      border-bottom-color: #e7edf4;
    }}
    .month-kicker {{
      color: #7a8797;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .8px;
      margin-bottom: 2px;
    }}
    .month-title {{
      font-size: 20px;
      color: #172337;
      letter-spacing: .2px;
    }}
    .week-row {{
      overflow: hidden;
      border-radius: 9px 9px 0 0;
      border: 1px solid #e6edf5;
      border-bottom: 0;
    }}
    .week-row div {{
      background: #f6f8fb;
      color: #5b6d82;
      padding: 7px 0;
      border-right: 1px solid #e6edf5;
      font-size: 14px;
    }}
    .calendar {{
      border-left: 1px solid #e6edf5;
      border-top: 1px solid #e6edf5;
      flex: 1;
      grid-template-rows: repeat(6, minmax(0, 1fr));
      grid-auto-rows: 1fr;
      min-height: 0;
    }}
    .day-cell {{
      height: auto;
      min-height: 0;
      border-color: #e6edf5;
      background: #fff;
      padding: 8px;
    }}
    .day-cell:hover {{
      background: #fbfdff;
      box-shadow: inset 0 0 0 2px #d7eaff;
    }}
    .day-cell.muted {{ background: #fbfcfe; }}
    .day-cell.selected {{
      background: #f3f8ff;
      box-shadow: inset 0 0 0 2px #2a86ee;
    }}
    .day-cell.has-events::before {{
      content: "";
      position: absolute;
      left: 8px;
      top: 8px;
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: #9eb3cb;
    }}
    .day-cell.selected::before {{ background: #ef4b3a; }}
    .day-num {{
      right: 9px;
      top: 8px;
      color: #152236;
      font-size: 14px;
    }}
    .muted .day-num {{ color: #b4bfcb; }}
    .badges {{
      left: 9px;
      right: 9px;
      bottom: 9px;
      gap: 4px;
    }}
    .badge {{
      width: auto;
      min-width: 20px;
      height: 18px;
      padding: 0 5px;
      border-radius: 999px;
      background: #edf2f7;
      color: #65768a;
      font-size: 11px;
      box-shadow: inset 0 0 0 1px #dde6ef;
    }}
    .selected-badge {{
      background: #ef4b3a;
      color: #fff;
      box-shadow: none;
    }}
    .side {{
      overflow: hidden;
      color: #172337;
      background: #ffffff;
      border: 1px solid #dbe7f5;
      position: sticky;
      top: 76px;
    }}
    .date-card {{
      padding: 18px 18px 16px;
      color: #fff;
      border-bottom: 0;
      background:
        linear-gradient(135deg, rgba(17, 69, 142, .96), rgba(44, 132, 219, .92)),
        radial-gradient(circle at 20% 0, rgba(255,255,255,.36), transparent 36%);
    }}
    .date-card .big {{
      font-size: 40px;
      text-align: left;
    }}
    .date-card .month {{
      margin-top: 7px;
      text-align: left;
      font-size: 16px;
      opacity: .9;
    }}
    .side-summary {{
      padding: 12px;
      border-bottom: 1px solid #e4ecf5;
      background: #f7faff;
    }}
    .side-chip {{
      color: #172337;
      background: #fff;
      border: 1px solid #e1e9f2;
      border-radius: 10px;
      padding: 9px 10px;
    }}
    .side-chip span {{ color: #738297; opacity: 1; }}
    .trade-list {{
      padding: 10px 12px 12px;
      max-height: calc(100vh - 266px);
      overflow: auto;
    }}
    .trade-card {{
      color: #172337;
      background: #fff;
      border: 1px solid #e1e9f2;
      border-radius: 11px;
      padding: 11px 12px 12px;
      margin: 9px 0;
      box-shadow: 0 8px 18px rgba(31, 67, 105, .06);
      position: relative;
    }}
    .trade-card::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 14px;
      bottom: 14px;
      width: 3px;
      border-radius: 0 999px 999px 0;
      background: linear-gradient(180deg, #f15b48, #ef4b3a);
    }}
    .trade-card:hover {{ border-color: #c7ddf5; }}
    .card-head {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding-left: 6px;
    }}
    .trade-name {{
      font-size: 15px;
      line-height: 1.35;
      font-weight: 800;
      color: #162235;
    }}
    .trade-tag {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 22px;
      height: 22px;
      padding: 0 7px;
      border-radius: 999px;
      background: #fff1ef;
      color: #e54c39;
      font-size: 12px;
      font-weight: 900;
      box-shadow: inset 0 0 0 1px #ffd2cb;
    }}
    .trade-meta {{
      display: grid;
      gap: 7px;
      margin-top: 10px;
      padding-left: 6px;
    }}
    .meta-block {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
      color: #263b55;
    }}
    .meta-block span {{
      color: #75869a;
      white-space: nowrap;
    }}
    .meta-block strong {{
      text-align: right;
      color: #233852;
      font-weight: 800;
    }}
    .empty-state {{
      color: #42556e;
      background: #f7faff;
      border: 1px dashed #ccd9e7;
      border-radius: 11px;
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark">桂</div>
      <div>广西电力交易日历<small>PDF 转换 · CSV 同步 · 开发板读取</small></div>
    </div>
    <div class="unit">单位 <strong>兆瓦时</strong><span>价格单位 元/兆瓦时</span></div>
  </header>

  <main class="page">
    <section class="toolbar panel">
      <div class="stats">
        <div class="stat"><span>当前月份</span><strong>{year}-{month:02d}</strong></div>
        <div class="stat"><span>本月交易</span><strong>{month_count}</strong></div>
        <div class="stat"><span>有安排日期</span><strong>{month_days}</strong></div>
        <div class="stat"><span>选中日期</span><strong>{selected_count}</strong></div>
      </div>
      <div class="upload">
        {"<p class='ok'>" + html.escape(message) + "</p>" if message else ""}
        <form method="POST" action="/device">
          <input type="url" name="board_url" placeholder="http://开发板IP" value="{board_url_value}">
          <button type="submit">保存开发板</button>
          <a class="month-nav" href="/device/refresh">测试刷新</a>
        </form>
        <form method="POST" action="/upload" enctype="multipart/form-data">
          <input type="file" name="pdf" accept="application/pdf,.pdf" required>
          <button type="submit">上传并转换 PDF</button>
          <a class="month-nav" href="/schedule.csv">下载 CSV</a>
          <div class="device-url">开发板读取 <code>http://{ip}:{PORT}/schedule.csv</code></div>
          <div class="device-url">开发板控制 <code>{html.escape(board_url or "未设置")}</code></div>
        </form>
      </div>
    </section>

    <section class="shell">
      <div class="calendar-panel panel">
        <div class="calendar-head">
          <div>
            <div class="month-kicker">Trading Calendar</div>
            <div class="month-title">{selected_date}</div>
          </div>
          <div class="month-navs">
            <a class="month-nav" href="/?month={prev_year}-{prev_month:02d}&day=1">上一月</a>
            <a class="month-nav" href="/?month={next_year}-{next_month:02d}&day=1">下一月</a>
          </div>
        </div>
        <div class="week-row"><div>一</div><div>二</div><div>三</div><div>四</div><div>五</div><div>六</div><div>日</div></div>
        <div class="calendar">{render_calendar(rows, year, month, selected_day)}</div>
      </div>

      <aside class="side panel">
        <div class="date-card">
          <div class="big">{selected_day}日</div>
          <div class="month">{selected_iso}</div>
        </div>
        <div class="side-summary">
          <div class="side-chip"><span>当天交易</span>{selected_count} 项</div>
          <div class="side-chip"><span>数据来源</span>CSV</div>
        </div>
        <div class="trade-list">{render_side_panel(rows, year, month, selected_day)}</div>
      </aside>
    </section>
  </main>
</body>
</html>"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            self.send_bytes(200, "text/html; charset=utf-8", render_page(query=query))
            return
        if path == "/schedule.csv":
            if CSV_PATH.exists():
                self.send_bytes(200, "text/csv; charset=utf-8", CSV_PATH.read_bytes())
            else:
                self.send_bytes(404, "text/plain; charset=utf-8", "schedule.csv not found".encode("utf-8"))
            return
        if path == "/schedule.json":
            if JSON_PATH.exists():
                self.send_bytes(200, "application/json; charset=utf-8", JSON_PATH.read_bytes())
            else:
                self.send_bytes(404, "text/plain; charset=utf-8", "schedule.json not found".encode("utf-8"))
            return
        if path == "/device/refresh":
            self.send_bytes(200, "text/html; charset=utf-8", render_page(notify_board_refresh(), query=query))
            return
        self.send_bytes(404, "text/plain; charset=utf-8", "not found".encode("utf-8"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/device":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length).decode("utf-8", errors="ignore")
            fields = parse_qs(body)
            board_url = save_board_url(fields.get("board_url", [""])[0])
            if board_url:
                message = f"开发板地址已保存：{board_url}"
            else:
                message = "已清空开发板地址"
            self.send_bytes(200, "text/html; charset=utf-8", render_page(message))
            return

        if path == "/device/refresh":
            self.send_bytes(200, "text/html; charset=utf-8", render_page(notify_board_refresh()))
            return

        if path != "/upload":
            self.send_bytes(404, "text/plain; charset=utf-8", "not found".encode("utf-8"))
            return

        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        message = BytesParser(policy=default).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8") + body)
        part = next((p for p in message.iter_parts() if p.get_filename()), None)
        if part is None:
            self.send_bytes(400, "text/html; charset=utf-8", render_page("没有收到 PDF 文件"))
            return

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = UPLOAD_DIR / f"{int(time.time())}_{Path(part.get_filename()).name}"
        pdf_path.write_bytes(part.get_payload(decode=True))
        text = read_pdf_text(pdf_path)
        RAW_TEXT_PATH.write_text(text, encoding="utf-8")
        events = parse_events_from_text(text)
        merged_events = merge_events_by_month(events)
        write_outputs(merged_events)
        board_message = notify_board_refresh()
        self.send_bytes(
            200,
            "text/html; charset=utf-8",
            render_page(f"转换完成：新增/更新 {len(events)} 条记录，当前共 {len(merged_events)} 条记录。{board_message}"),
        )


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"交易日历处理系统已启动: http://{local_ip()}:{PORT}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
