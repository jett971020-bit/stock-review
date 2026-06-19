from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

LOCAL_PACKAGES = Path(__file__).with_name(".python_packages")
if LOCAL_PACKAGES.exists():
    sys.path.insert(0, str(LOCAL_PACKAGES))

import akshare as ak
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots
from storage import load_app_data, save_app_data


MA_WINDOWS = (5, 10, 20, 60)


st.set_page_config(page_title="A股复盘", page_icon="📈", layout="wide")


def normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if symbol.startswith(prefix):
            symbol = symbol[len(prefix) :]
    return symbol


def is_code_like(raw_query: str) -> bool:
    query = raw_query.strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if query.startswith(prefix):
            query = query[len(prefix) :]
    return bool(re.fullmatch(r"\d{6}", query))


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


def stock_display_name(item: dict) -> str:
    name = item.get("name") or item.get("code") or ""
    code = item.get("code") or ""
    return f"{name} ({code})" if name and name != code else code


def upsert_history(data: dict, query: str, code: str, name: str) -> None:
    record = {"query": query.strip(), "code": code, "name": name or code}
    history = [
        item for item in data.get("history", [])
        if item.get("query") != record["query"] and item.get("code") != code
    ]
    data["history"] = [record, *history][:12]
    save_app_data(data)


def add_watchlist_item(data: dict, code: str, name: str) -> bool:
    watchlist = data.get("watchlist", [])
    if any(item.get("code") == code for item in watchlist):
        return False
    watchlist.append({"code": code, "name": name or code})
    data["watchlist"] = sorted(watchlist, key=lambda item: item.get("code", ""))
    save_app_data(data)
    return True


def delete_watchlist_items(data: dict, codes: set[str]) -> None:
    data["watchlist"] = [
        item for item in data.get("watchlist", [])
        if item.get("code") not in codes
    ]
    save_app_data(data)


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def search_stock_names(query: str, max_results: int = 30) -> pd.DataFrame:
    keyword = query.strip()
    if not keyword:
        return pd.DataFrame(columns=["code", "name"])

    url = (
        "https://suggest3.sinajs.cn/suggest/"
        f"type=11,12&key={quote(keyword)}&name=suggestdata"
    )
    response = requests.get(url, timeout=15)
    response.raise_for_status()

    match = re.search(r'"(.*)"', response.text)
    if match is None or not match.group(1):
        return pd.DataFrame(columns=["code", "name", "market_symbol"])

    rows = []
    for item in match.group(1).split(";"):
        fields = item.split(",")
        if len(fields) < 4:
            continue
        name = fields[0].strip()
        code = fields[2].strip()
        market_symbol = fields[3].strip().lower()
        if not re.fullmatch(r"\d{6}", code):
            continue
        if not market_symbol.startswith(("sh", "sz", "bj")):
            continue
        rows.append({"code": code, "name": name, "market_symbol": market_symbol})

    result = pd.DataFrame(rows).drop_duplicates("code")
    if result.empty:
        return pd.DataFrame(columns=["code", "name", "market_symbol"])

    result["rank"] = result["name"].apply(
        lambda name: 0 if name == keyword else 1 if name.startswith(keyword) else 2
    )
    return result.sort_values(["rank", "code"]).head(max_results).drop(columns=["rank"])


def normalize_daily_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
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
    if "change" not in result.columns:
        result["change"] = result["close"].diff()
    if "turnover" not in result.columns:
        result["turnover"] = pd.NA

    required = ["date", "open", "high", "low", "close", "volume", "amount", "pct_change", "change", "turnover"]
    result = result[[column for column in required if column in result.columns]]
    numeric_columns = [col for col in result.columns if col != "date"]
    result[numeric_columns] = result[numeric_columns].apply(pd.to_numeric, errors="coerce")
    return result.sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=60 * 20, show_spinner=False)
def fetch_daily_data(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    errors: list[str] = []

    providers = [
        (
            "东方财富",
            lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            ),
        ),
        (
            "新浪",
            lambda: ak.stock_zh_a_daily(
                symbol=with_market_prefix(symbol),
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            ),
        ),
        (
            "腾讯",
            lambda: ak.stock_zh_a_hist_tx(
                symbol=with_market_prefix(symbol),
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            ),
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


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for window in MA_WINDOWS:
        result[f"ma{window}"] = result["close"].rolling(window).mean()

    result["prev_20_high"] = result["high"].rolling(20).max().shift(1)
    result["prev_5_volume_avg"] = result["volume"].rolling(5).mean().shift(1)
    result["volume_ratio"] = result["volume"] / result["prev_5_volume_avg"]
    return result


def mark_signals(
    df: pd.DataFrame,
    breakout_volume_ratio: float,
    pullback_volume_ratio: float,
    pullback_tolerance_pct: float,
) -> pd.DataFrame:
    result = df.copy()
    tolerance = pullback_tolerance_pct / 100

    result["breakout"] = (
        result["prev_20_high"].notna()
        & (result["close"] > result["prev_20_high"])
        & (result["volume_ratio"] >= breakout_volume_ratio)
        & (result["close"] > result["ma20"])
    )

    touches_ma20 = (
        result["ma20"].notna()
        & (result["low"] <= result["ma20"] * (1 + tolerance))
        & (result["low"] >= result["ma20"] * (1 - tolerance * 2))
    )
    result["ma20_pullback"] = (
        touches_ma20
        & (result["close"] >= result["ma20"] * (1 - tolerance))
        & (result["volume_ratio"] <= pullback_volume_ratio)
    )
    return result


def latest_metric(df: pd.DataFrame, column: str, suffix: str = "") -> str:
    value = df[column].dropna().iloc[-1] if df[column].notna().any() else None
    if value is None:
        return "-"
    return f"{value:,.2f}{suffix}"


def build_chart(df: pd.DataFrame) -> go.Figure:
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.62, 0.22, 0.16],
        specs=[[{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": False}]],
    )

    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="日K",
            increasing_line_color="#d62728",
            decreasing_line_color="#2ca02c",
        ),
        row=1,
        col=1,
    )

    ma_colors = {
        "ma5": "#1f77b4",
        "ma10": "#9467bd",
        "ma20": "#ff7f0e",
        "ma60": "#17becf",
    }
    for column, color in ma_colors.items():
        fig.add_trace(
            go.Scatter(
                x=df["date"],
                y=df[column],
                mode="lines",
                name=column.upper(),
                line={"width": 1.6, "color": color},
            ),
            row=1,
            col=1,
        )

    breakout_points = df[df["breakout"]]
    if not breakout_points.empty:
        fig.add_trace(
            go.Scatter(
                x=breakout_points["date"],
                y=breakout_points["high"] * 1.018,
                mode="markers+text",
                name="放量突破",
                marker={"symbol": "triangle-up", "size": 13, "color": "#d62728"},
                text=["突破"] * len(breakout_points),
                textposition="top center",
                textfont={"size": 11, "color": "#d62728"},
            ),
            row=1,
            col=1,
        )

    pullback_points = df[df["ma20_pullback"]]
    if not pullback_points.empty:
        fig.add_trace(
            go.Scatter(
                x=pullback_points["date"],
                y=pullback_points["low"] * 0.982,
                mode="markers+text",
                name="缩量回踩20日线",
                marker={"symbol": "circle", "size": 11, "color": "#2ca02c"},
                text=["回踩"] * len(pullback_points),
                textposition="bottom center",
                textfont={"size": 11, "color": "#2ca02c"},
            ),
            row=1,
            col=1,
        )

    volume_colors = df.apply(lambda row: "#d62728" if row["close"] >= row["open"] else "#2ca02c", axis=1)
    fig.add_trace(
        go.Bar(
            x=df["date"],
            y=df["volume"],
            name="成交量",
            marker_color=volume_colors,
            opacity=0.72,
        ),
        row=2,
        col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["volume_ratio"],
            mode="lines",
            name="量比",
            line={"width": 1.8, "color": "#222222"},
        ),
        row=3,
        col=1,
    )
    fig.add_hline(y=1, line_dash="dot", line_color="#888888", row=3, col=1)

    fig.update_layout(
        height=760,
        margin={"l": 24, "r": 24, "t": 24, "b": 24},
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    fig.update_yaxes(title_text="量比", row=3, col=1)
    return fig


def signal_table(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    return df.loc[df["breakout"] | df["ma20_pullback"], list(columns)].sort_values("date", ascending=False)


def scan_watchlist(
    watchlist: list[dict],
    start_date: str,
    end_date: str,
    adjust: str,
    breakout_volume_ratio: float,
    pullback_volume_ratio: float,
    pullback_tolerance_pct: float,
) -> tuple[list[dict], list[dict]]:
    alerts = []
    rows = []

    for item in watchlist:
        code = item.get("code", "")
        name = item.get("name") or code
        if not code:
            continue
        try:
            daily = fetch_daily_data(code, start_date, end_date, adjust)
            review = mark_signals(
                enrich_indicators(daily),
                breakout_volume_ratio=breakout_volume_ratio,
                pullback_volume_ratio=pullback_volume_ratio,
                pullback_tolerance_pct=pullback_tolerance_pct,
            )
            latest_row = review.iloc[-1]
            record = {
                "名称": name,
                "代码": code,
                "日期": latest_row["date"].strftime("%Y-%m-%d"),
                "收盘": round(float(latest_row["close"]), 2),
                "MA20": round(float(latest_row["ma20"]), 2) if pd.notna(latest_row["ma20"]) else None,
                "量比": round(float(latest_row["volume_ratio"]), 2) if pd.notna(latest_row["volume_ratio"]) else None,
                "状态": "缩量回踩20日线" if bool(latest_row["ma20_pullback"]) else "未触发",
            }
            rows.append(record)
            if bool(latest_row["ma20_pullback"]):
                alerts.append(record)
        except Exception as exc:
            rows.append({
                "名称": name,
                "代码": code,
                "日期": "-",
                "收盘": None,
                "MA20": None,
                "量比": None,
                "状态": f"扫描失败：{exc}",
            })

    return alerts, rows


st.title("A股复盘")

app_data = load_app_data()
if "stock_query" not in st.session_state:
    st.session_state["stock_query"] = "000001"

with st.sidebar:
    history = app_data.get("history", [])
    if history:
        with st.expander("历史搜索", expanded=False):
            for index, item in enumerate(history[:8]):
                label = stock_display_name(item)
                if st.button(label, key=f"history_{index}_{item.get('code')}", width="stretch"):
                    st.session_state["stock_query"] = item.get("query") or item.get("code") or ""
                    st.rerun()

    raw_query = st.text_input("股票代码或名称", help="支持 000001、sh600519、平安银行、贵州茅台 等格式", key="stock_query")
    st.button("查询", width="stretch")
    selected_stock_name = ""
    if is_code_like(raw_query):
        symbol = normalize_symbol(raw_query)
    else:
        try:
            matches = search_stock_names(raw_query)
        except Exception as exc:
            st.error(f"股票名称搜索失败：{exc}")
            st.stop()

        if matches.empty:
            st.warning("没有找到匹配的股票名称，请换个关键词或直接输入 6 位代码。")
            st.stop()

        options = [f"{row.name} ({row.code})" for row in matches.itertuples(index=False)]
        selected_option = st.selectbox("选择股票", options)
        selected_code = selected_option.rsplit("(", 1)[-1].rstrip(")")
        selected_stock_name = selected_option.rsplit(" (", 1)[0]
        symbol = normalize_symbol(selected_code)

    with st.expander("重点监测", expanded=True):
        watchlist = app_data.get("watchlist", [])
        if st.button("添加当前股票", width="stretch"):
            if add_watchlist_item(app_data, symbol, selected_stock_name or symbol):
                st.success("已加入重点监测。")
            else:
                st.info("这只股票已经在重点监测里。")
            st.rerun()

        if watchlist:
            delete_labels = {stock_display_name(item): item.get("code") for item in watchlist}
            selected_delete = st.multiselect("删除股票", list(delete_labels.keys()))
            if st.button("删除选中", width="stretch", disabled=not selected_delete):
                delete_watchlist_items(app_data, {delete_labels[label] for label in selected_delete})
                st.rerun()
        else:
            st.caption("还没有重点监测股票。")

        auto_scan_watchlist = st.checkbox("打开页面自动扫描", value=True)
        manual_scan_watchlist = st.button("刷新监测", width="stretch")

    adjust_label = st.radio("复权方式", ["前复权", "不复权", "后复权"], horizontal=True)
    years = st.slider("回看年限", min_value=1, max_value=8, value=2)
    breakout_volume_ratio = st.slider("放量突破量比阈值", 1.0, 3.0, 1.5, 0.1)
    pullback_volume_ratio = st.slider("缩量回踩量比上限", 0.3, 1.2, 0.8, 0.05)
    pullback_tolerance_pct = st.slider("20日线回踩容差", 0.5, 5.0, 2.0, 0.5, format="%.1f%%")

adjust_map = {"前复权": "qfq", "不复权": "", "后复权": "hfq"}
end = date.today()
start = end - timedelta(days=365 * years + 90)

if not symbol:
    st.info("请输入 A 股股票代码或股票名称。")
    st.stop()

try:
    with st.spinner("正在获取行情数据..."):
        daily = fetch_daily_data(symbol, format_ak_date(start), format_ak_date(end), adjust_map[adjust_label])
except Exception as exc:
    st.error(f"获取行情失败：{exc}")
    st.stop()

if daily.empty:
    st.warning("没有获取到行情数据，请检查股票代码或日期范围。")
    st.stop()

upsert_history(app_data, raw_query, symbol, selected_stock_name or symbol)

review = mark_signals(
    enrich_indicators(daily),
    breakout_volume_ratio=breakout_volume_ratio,
    pullback_volume_ratio=pullback_volume_ratio,
    pullback_tolerance_pct=pullback_tolerance_pct,
)

latest = review.iloc[-1]
metric_cols = st.columns(5)
metric_cols[0].metric("最新收盘", f"{latest['close']:.2f}", f"{latest['pct_change']:.2f}%")
metric_cols[1].metric("MA20", latest_metric(review, "ma20"))
metric_cols[2].metric("MA60", latest_metric(review, "ma60"))
metric_cols[3].metric("成交量", f"{latest['volume']:,.0f}")
metric_cols[4].metric("量比", latest_metric(review, "volume_ratio"))

provider = review["provider"].dropna().iloc[-1] if "provider" in review.columns else "AKShare"
stock_label = f"{selected_stock_name}（{symbol}）" if selected_stock_name else symbol
st.caption(f"当前股票：{stock_label} ｜ 数据源：AKShare / {provider}")

watchlist = app_data.get("watchlist", [])
if watchlist and (auto_scan_watchlist or manual_scan_watchlist):
    with st.spinner("正在扫描重点监测股票..."):
        pullback_alerts, watchlist_rows = scan_watchlist(
            watchlist,
            format_ak_date(start),
            format_ak_date(end),
            adjust_map[adjust_label],
            breakout_volume_ratio,
            pullback_volume_ratio,
            pullback_tolerance_pct,
        )

    if pullback_alerts:
        alert_names = "、".join(f"{item['名称']}({item['代码']})" for item in pullback_alerts)
        st.warning(f"回踩提醒：{alert_names} 最新交易日触发缩量回踩20日线。")
    else:
        st.success("重点监测列表暂无缩量回踩20日线提醒。")

    with st.expander("重点监测扫描结果", expanded=bool(pullback_alerts)):
        st.dataframe(pd.DataFrame(watchlist_rows), hide_index=True, width="stretch")

st.plotly_chart(build_chart(review), width="stretch")

signals = signal_table(
    review,
    [
        "date",
        "close",
        "pct_change",
        "volume",
        "volume_ratio",
        "ma20",
        "prev_20_high",
        "breakout",
        "ma20_pullback",
    ],
)

st.subheader("信号明细")
if signals.empty:
    st.caption("当前参数下没有识别到放量突破或缩量回踩20日线。")
else:
    display = signals.copy()
    display["date"] = display["date"].dt.strftime("%Y-%m-%d")
    display["信号"] = display.apply(
        lambda row: "放量突破" if row["breakout"] else "缩量回踩20日线",
        axis=1,
    )
    display = display.rename(
        columns={
            "date": "日期",
            "close": "收盘",
            "pct_change": "涨跌幅%",
            "volume": "成交量",
            "volume_ratio": "量比",
            "ma20": "MA20",
            "prev_20_high": "前20日高点",
        }
    )
    st.dataframe(
        display[["日期", "信号", "收盘", "涨跌幅%", "成交量", "量比", "MA20", "前20日高点"]],
        hide_index=True,
        width="stretch",
    )

st.caption("量比按当日成交量 / 前5个交易日平均成交量计算；放量突破按收盘价突破前20日高点且量比达到阈值识别。")
