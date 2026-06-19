from __future__ import annotations

import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

LOCAL_PACKAGES = Path(__file__).with_name(".python_packages")
if LOCAL_PACKAGES.exists():
    sys.path.insert(0, str(LOCAL_PACKAGES))

import akshare as ak
import pandas as pd
import requests

from storage import load_app_data, save_app_data


MA_WINDOWS = (5, 10, 20, 60)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if symbol.startswith(prefix):
            symbol = symbol[len(prefix) :]
    return symbol


def with_market_prefix(symbol: str) -> str:
    if symbol.startswith(("6", "5", "9")):
        return f"sh{symbol}"
    if symbol.startswith(("0", "2", "3")):
        return f"sz{symbol}"
    if symbol.startswith(("4", "8")):
        return f"bj{symbol}"
    return symbol


def format_ak_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def normalize_daily_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "涨跌幅": "pct_change",
        "涨跌额": "change",
        "换手率": "turnover",
    }
    result = df.rename(columns=rename_map).copy()
    result["date"] = pd.to_datetime(result["date"])
    if "volume" not in result.columns and "amount" in result.columns:
        result["volume"] = result["amount"]
        result["amount"] = pd.NA
    if "pct_change" not in result.columns:
        result["pct_change"] = result["close"].pct_change() * 100
    required = ["date", "open", "high", "low", "close", "volume", "amount", "pct_change", "turnover"]
    result = result[[column for column in required if column in result.columns]]
    numeric_columns = [col for col in result.columns if col != "date"]
    result[numeric_columns] = result[numeric_columns].apply(pd.to_numeric, errors="coerce")
    return result.sort_values("date").reset_index(drop=True)


def fetch_daily_data(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    errors: list[str] = []
    providers = [
        (
            "东方财富",
            lambda: ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust),
        ),
        (
            "新浪",
            lambda: ak.stock_zh_a_daily(symbol=with_market_prefix(symbol), start_date=start_date, end_date=end_date, adjust=adjust),
        ),
        (
            "腾讯",
            lambda: ak.stock_zh_a_hist_tx(symbol=with_market_prefix(symbol), start_date=start_date, end_date=end_date, adjust=adjust),
        ),
    ]
    for provider_name, loader in providers:
        try:
            df = loader()
            if not df.empty:
                normalized = normalize_daily_columns(df)
                normalized["provider"] = provider_name
                return normalized
        except Exception as exc:
            errors.append(f"{provider_name}: {exc}")
    raise RuntimeError("；".join(errors) or "未获取到行情数据")


def fetch_realtime_quote(symbol: str) -> dict:
    market_symbol = with_market_prefix(symbol)
    response = requests.get(
        f"https://hq.sinajs.cn/list={market_symbol}",
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=15,
    )
    response.encoding = "gb18030"
    response.raise_for_status()
    match = re.search(r'"(.*)"', response.text)
    if match is None or not match.group(1):
        raise RuntimeError("新浪实时行情为空")

    fields = match.group(1).split(",")
    if len(fields) < 32:
        raise RuntimeError("新浪实时行情字段不完整")

    return {
        "name": fields[0],
        "open": float(fields[1]),
        "prev_close": float(fields[2]),
        "price": float(fields[3]),
        "high": float(fields[4]),
        "low": float(fields[5]),
        "volume": float(fields[8]),
        "amount": float(fields[9]),
        "date": fields[30],
        "time": fields[31],
        "provider": "新浪实时",
    }


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for window in MA_WINDOWS:
        result[f"ma{window}"] = result["close"].rolling(window).mean()
    result["prev_20_high"] = result["high"].rolling(20).max().shift(1)
    result["prev_5_volume_avg"] = result["volume"].rolling(5).mean().shift(1)
    result["volume_ratio"] = result["volume"] / result["prev_5_volume_avg"]
    return result


def mark_pullback(df: pd.DataFrame, volume_ratio_limit: float, tolerance_pct: float) -> pd.DataFrame:
    result = df.copy()
    tolerance = tolerance_pct / 100
    touches_ma20 = (
        result["ma20"].notna()
        & (result["low"] <= result["ma20"] * (1 + tolerance))
        & (result["low"] >= result["ma20"] * (1 - tolerance * 2))
    )
    result["ma20_pullback"] = (
        touches_ma20
        & (result["close"] >= result["ma20"] * (1 - tolerance))
        & (result["volume_ratio"] <= volume_ratio_limit)
    )
    return result


def scan_watchlist() -> tuple[list[dict], list[dict]]:
    data = load_app_data()
    watchlist = data.get("watchlist", [])
    end = date.today()
    start = end - timedelta(days=365 * 2 + 90)
    adjust = env("ADJUST", "qfq")
    volume_ratio_limit = float(env("PULLBACK_VOLUME_RATIO", "0.8"))
    tolerance_pct = float(env("PULLBACK_TOLERANCE_PCT", "2.0"))
    rows = []
    alerts = []

    for item in watchlist:
        code = normalize_symbol(item.get("code", ""))
        name = item.get("name") or code
        if not code:
            continue
        try:
            daily = fetch_daily_data(code, format_ak_date(start), format_ak_date(end), adjust)
            review = enrich_indicators(daily)
            latest = mark_pullback(review, volume_ratio_limit, tolerance_pct).iloc[-1]
            quote = None
            try:
                quote = fetch_realtime_quote(code)
            except Exception:
                quote = None

            if quote and pd.notna(latest["ma20"]) and pd.notna(latest["prev_5_volume_avg"]):
                tolerance = tolerance_pct / 100
                volume_ratio = quote["volume"] / float(latest["prev_5_volume_avg"])
                triggered = (
                    quote["low"] <= float(latest["ma20"]) * (1 + tolerance)
                    and quote["low"] >= float(latest["ma20"]) * (1 - tolerance * 2)
                    and quote["price"] >= float(latest["ma20"]) * (1 - tolerance)
                    and volume_ratio <= volume_ratio_limit
                )
                latest_date = quote["date"]
                close_or_price = quote["price"]
                provider = quote["provider"]
            else:
                volume_ratio = latest["volume_ratio"]
                triggered = bool(latest["ma20_pullback"])
                latest_date = latest["date"].strftime("%Y-%m-%d")
                close_or_price = latest["close"]
                provider = latest.get("provider", "AKShare")

            row = {
                "name": name,
                "code": code,
                "date": latest_date,
                "close": round(float(close_or_price), 2),
                "ma20": round(float(latest["ma20"]), 2) if pd.notna(latest["ma20"]) else None,
                "volume_ratio": round(float(volume_ratio), 2) if pd.notna(volume_ratio) else None,
                "triggered": triggered,
                "provider": provider,
            }
            rows.append(row)
            if row["triggered"]:
                alerts.append(row)
        except Exception as exc:
            rows.append({"name": name, "code": code, "error": str(exc), "triggered": False})

    data["reminders"] = [
        {
            "time": datetime.now().isoformat(timespec="seconds"),
            "alerts": alerts,
            "rows": rows,
        },
        *data.get("reminders", []),
    ][:30]
    save_app_data(data)
    return alerts, rows


def build_email(alerts: list[dict], rows: list[dict]) -> EmailMessage:
    sender = env("SMTP_USER")
    receiver = env("MAIL_TO") or sender
    subject_prefix = env("MAIL_SUBJECT_PREFIX", "A股回踩提醒")
    today = date.today().strftime("%Y-%m-%d")

    if alerts:
        subject = f"{subject_prefix}：{len(alerts)}只触发 {today}"
        lines = ["以下重点监测股票触发缩量回踩20日线：", ""]
        for item in alerts:
            lines.append(
                f"- {item['name']}({item['code']}): 收盘/现价 {item['close']}, MA20 {item['ma20']}, 量比 {item['volume_ratio']}"
            )
    else:
        subject = f"{subject_prefix}：暂无触发 {today}"
        lines = ["重点监测列表暂无缩量回踩20日线触发。"]

    lines.extend(["", "扫描明细："])
    for item in rows:
        if "error" in item:
            lines.append(f"- {item['name']}({item['code']}): 扫描失败：{item['error']}")
        else:
            status = "触发" if item["triggered"] else "未触发"
            lines.append(
                f"- {item['name']}({item['code']}): {status}, 日期 {item['date']}, 收盘/现价 {item['close']}, MA20 {item['ma20']}, 量比 {item['volume_ratio']}"
            )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = receiver
    message["Subject"] = subject
    message.set_content("\n".join(lines))
    return message


def send_email(message: EmailMessage) -> None:
    host = env("SMTP_HOST", "smtp.qq.com")
    port = int(env("SMTP_PORT", "465"))
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    if not user or not password:
        raise RuntimeError("缺少 SMTP_USER 或 SMTP_PASSWORD，无法发送邮件")

    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(user, password)
        server.send_message(message)


def main() -> None:
    alerts, rows = scan_watchlist()
    message = build_email(alerts, rows)
    send_email(message)
    print(f"sent reminder email, alerts={len(alerts)}, scanned={len(rows)}")


if __name__ == "__main__":
    main()
