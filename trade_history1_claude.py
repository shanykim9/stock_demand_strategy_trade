"""
hts_screenshot_chart.py — HTS 스크린샷 매매내역 차트 분석기
═══════════════════════════════════════════════════════════════
실행: streamlit run hts_screenshot_chart.py

[기능]
1. 키움 HTS 0328 스크린샷 업로드
2. Claude Vision API로 표 데이터 자동 추출
   (매도일, 종목명, 수량, 매입가, 매도가, 손익률)
3. 종목명 → 종목코드 변환 (키움 REST API)
4. ka10081 일봉 차트에 매매내역 오버레이
   - ▼ 매도 마커 (매도일 + 매도가)
   - ━ 매입가 수평선
   - MA5 / MA20 / MA60
5. 종목별 정리 테이블 출력
"""
from __future__ import annotations
import os, base64, json
from datetime import datetime, timedelta, date as date_
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
try:
    from dotenv import load_dotenv; load_dotenv()
except: pass
import demand as core

# ── 상수 ──────────────────────────────────────────────────────
COLORS = {
    "bull": "#E53935", "bear": "#1E88E5",
    "bv":   "#EF9A9A", "bev":  "#90CAF9",
    "ma5":  "#F48FB1", "ma20": "#FFD600", "ma60": "#26A69A",
    "bg":   "#131722", "grid": "#1E222D", "txt":  "#D1D4DC",
    "sell_profit": "#00E676", "sell_loss": "#F44336",
    "buy_line": "#FF9800",
}
CACHE_DIR = Path(".cache/hts_chart"); CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CSS = """<style>
[data-testid="stMetric"]{background:linear-gradient(135deg,#1a1f2e,#151926);
  border:1px solid #2a2f42;border-radius:10px;padding:14px 18px}
[data-testid="stMetric"] label{color:#8b8fa3!important;font-size:.78rem!important}
[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#e8eaed!important;
  font-size:1.15rem!important;font-weight:600!important}
section[data-testid="stSidebar"]{background:#0f1117}
</style>"""

# ── 유틸 ──────────────────────────────────────────────────────
def _int(v, d=0):
    if v is None: return d
    s = str(v).strip().replace(",","").replace("+","").replace("-","",1).strip()
    if not s: return d
    try: return int(float(s))
    except: return d

def _float(v, d=0.0):
    if v is None: return d
    s = str(v).strip().replace(",","").replace("+","")
    if s.startswith("-"): neg=True; s=s[1:]
    else: neg=False
    if not s: return d
    try: r=float(s); return -r if neg else r
    except: return d

def _f(row, keys):
    for k in keys:
        if k in row and str(row.get(k,"")).strip() not in ("","-"): return row.get(k)
    return None

def _pdt(v):
    if v is None: return None
    s = str(v).strip().replace("/","-").replace(".","-")
    if len(s)>=8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s)>=10 and s[4]=="-": return s[:10]
    return None

# ── Claude Vision API: 이미지에서 표 추출 ────────────────────
def _extract_trades_from_image(img_bytes: bytes, mime: str):
    """
    Claude Vision API로 HTS 스크린샷에서 매매내역 추출
    반환: (trades_list, raw_text)
    """
    import requests as req, re as _re
    b64 = base64.b64encode(img_bytes).decode()

    api_key = os.getenv("ANTHROPIC_API_KEY","")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 환경변수가 없습니다. .env 파일에 추가해 주세요.")

    prompt = (
        "이 이미지는 키움증권 HTS 실현손익 화면입니다.\n"
        "표에 있는 모든 행의 데이터를 JSON 배열로 출력하세요.\n"
        "각 행은 다음 필드를 포함합니다:\n"
        "sell_date(매도일 YYYY-MM-DD), stk_nm(종목명), qty(수량 정수),\n"
        "buy_price(매입가 소수점포함), sell_price(매도체결가 정수),\n"
        "pnl(실현손익 정수), pnl_rate(수익률 소수점포함)\n\n"
        "출력 형식(JSON 배열만, 다른 텍스트 없이):\n"
        '[{"sell_date":"2026-05-04","stk_nm":"종목명","qty":13,'
        '"buy_price":24050.0,"sell_price":27500,"pnl":44045,"pnl_rate":14.09}]'
    )

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    }

    r = req.post("https://api.anthropic.com/v1/messages",
                 headers={"Content-Type": "application/json",
                          "x-api-key": api_key,
                          "anthropic-version": "2023-06-01"},
                 json=payload, timeout=90)
    r.raise_for_status()
    resp = r.json()

    # 텍스트 추출
    raw = "".join(b.get("text","") for b in resp.get("content",[])
                  if b.get("type")=="text").strip()

    if not raw:
        return [], f"Vision API 응답 없음. HTTP상태={r.status_code}, 응답={str(resp)[:400]}"

    # JSON 배열 추출 시도 (여러 패턴)
    json_str = raw
    # 1) ```json ... ``` 블록
    m = _re.search(r"```json\s*(.*?)```", raw, _re.DOTALL)
    if m: json_str = m.group(1).strip()
    else:
        # 2) ``` ... ``` 블록
        m = _re.search(r"```\s*(.*?)```", raw, _re.DOTALL)
        if m: json_str = m.group(1).strip()

    # 3) [ ... ] 배열 부분
    s = json_str.find("["); e = json_str.rfind("]")
    if s != -1 and e != -1 and e > s:
        json_str = json_str[s:e+1]
    else:
        return [], "JSON 배열 없음. 원본응답:\n" + raw[:1000]

    try:
        trades = json.loads(json_str)
    except json.JSONDecodeError as ex:
        return [], "JSON파싱실패: " + str(ex) + "\n원본:\n" + raw[:1000]

    if not isinstance(trades, list):
        return [], "배열이 아님. 원본:\n" + raw[:500]

    # 날짜 정규화
    for t in trades:
        if t.get("sell_date"):
            t["sell_date"] = _pdt(str(t["sell_date"])) or t["sell_date"]

    return trades, raw

# ── 종목명 → 코드 변환 ────────────────────────────────────────
def _resolve_name(token, nm):
    nm = nm.strip()
    try:
        # demand.resolve_ticker 활용
        tk, err = core.resolve_ticker(nm)
        if tk and not err: return tk
    except: pass
    # KRX 캐시 직접 탐색
    try:
        cache = core._krx_cache.get("code_by_name") or {}
        if nm in cache: return cache[nm]
        # 부분 매칭
        for name, code in cache.items():
            if nm in name or name in nm: return code
    except: pass
    return None

# ── 일봉 데이터 조회 ─────────────────────────────────────────
def _fetch_daily(token, ticker, mp=40):
    edt = datetime.now(core.TZ).strftime("%Y%m%d")
    stex = (os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd  = (os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    cm   = {"stk_cd": ticker, "stex_tp": stex,
            "dmst_stex_tp": stex, "upd_stkpc_tp": upd}
    for body in [{**cm,"base_dt":edt},{**cm,"dt":edt},{**cm}]:
        try:
            res = core.call_tr_all_pages(
                token=token, api_id="ka10081", body=body,
                endpoint="/api/dostk/chart", max_pages=mp)
            rows = res.get("rows") or []
            if not rows: continue
            dd = {}
            for r in rows:
                dt = _pdt(_f(r,["dt","date","bas_dt","base_dt","trde_dt","trd_dt"]))
                if not dt: continue
                o = _int(_f(r,["open_pric","open","stck_oprc","opn_prc"]),0)
                h = _int(_f(r,["high_pric","high","stck_hgpr","hgh_prc"]),0)
                l = _int(_f(r,["low_pric","low","stck_lwpr","low_prc"]),0)
                c = _int(_f(r,["close_pric","close","stck_clpr","cur_prc","cur_pric"]),0)
                v = _int(_f(r,["trde_qty","volume","acml_vol","acc_trde_qty"]),0)
                if c<=0: continue
                if o<=0: o=c
                if h<=0: h=max(o,c)
                if l<=0: l=min(o,c)
                dd[dt]={"dt":dt,"open":o,"high":h,"low":l,"close":c,"volume":max(0,v)}
            out = sorted(dd.values(), key=lambda x:x["dt"])
            if out: return out
        except: continue
    return []

def _ma(vals, w):
    out=[None]*len(vals); s=0.0
    for i,v in enumerate(vals):
        s+=float(v)
        if i>=w: s-=float(vals[i-w])
        if i>=w-1: out[i]=s/float(w)
    return out

# ── 차트 생성 ─────────────────────────────────────────────────
def _chart(days, trade_list, nm, code, d_from, d_to):
    df = pd.DataFrame(days); df["dt"] = pd.to_datetime(df["dt"])
    if d_from: df = df[df["dt"] >= pd.to_datetime(d_from)]
    if d_to:   df = df[df["dt"] <= pd.to_datetime(d_to)]
    df = df.copy().reset_index(drop=True)

    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="차트 데이터 없음", showarrow=False)
        return fig

    closes = df["close"].tolist()
    mv5  = _ma(closes, 5)
    mv20 = _ma(closes, 20)
    mv60 = _ma(closes, 60)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.02, row_heights=[0.75, 0.25])

    # 캔들
    fig.add_trace(go.Candlestick(
        x=df["dt"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]), fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]), fillcolor=COLORS["bear"]),
        name="일봉"), row=1, col=1)

    # MA
    for mv, lbl, clr, w, dash in [
        (mv5,  "MA5",  COLORS["ma5"],  1.0, "dot"),
        (mv20, "MA20", COLORS["ma20"], 1.5, "solid"),
        (mv60, "MA60", COLORS["ma60"], 1.5, "dash"),
    ]:
        fig.add_trace(go.Scatter(x=df["dt"], y=mv, name=lbl,
            line=dict(color=clr, width=w, dash=dash),
            hoverinfo="skip"), row=1, col=1)

    # 거래량
    bull_mask = df["close"] >= df["open"]
    vc = [COLORS["bv"] if b else COLORS["bev"] for b in bull_mask]
    fig.add_trace(go.Bar(x=df["dt"], y=df["volume"], name="거래량",
        marker_color=vc, opacity=0.55), row=2, col=1)

    # 매매내역 오버레이
    for tr in trade_list:
        sdt = pd.to_datetime(tr["sell_date"])
        if d_from and sdt < pd.to_datetime(d_from): continue
        if d_to   and sdt > pd.to_datetime(d_to):   continue

        rate = _float(tr.get("pnl_rate", 0))
        clr = COLORS["sell_profit"] if rate >= 0 else COLORS["sell_loss"]
        sp  = _float(tr.get("sell_price", 0))
        bp  = _float(tr.get("buy_price", 0))

        # 매도 마커
        if sp > 0:
            fig.add_trace(go.Scatter(
                x=[sdt], y=[sp * 1.03],
                mode="markers+text",
                text=[f"{rate:+.1f}%"],
                textposition="top center",
                textfont=dict(size=9, color=clr),
                showlegend=False,
                marker=dict(symbol="triangle-down", size=14, color=clr,
                            line=dict(width=1, color="#000")),
                hovertemplate=(f"{tr['sell_date']}<br>매도: {sp:,.0f}원<br>"
                               f"수익률: {rate:+.2f}%<extra></extra>")),
                row=1, col=1)

        # 매입가 수평선 (매도일 기준 앞뒤 30봉)
        if bp > 0 and not df.empty:
            idx = df[df["dt"] <= sdt].index
            if len(idx) > 0:
                end_i = idx[-1]
                start_i = max(0, end_i - 60)
                x0 = df["dt"].iloc[start_i]
                x1 = sdt
                fig.add_shape(type="line",
                    x0=x0, x1=x1, y0=bp, y1=bp,
                    line=dict(color=COLORS["buy_line"], width=1.2, dash="dash"),
                    row=1, col=1)
                # 매입가 레이블
                fig.add_trace(go.Scatter(
                    x=[x0], y=[bp * 0.97],
                    mode="text",
                    text=[f"매입 {bp:,.0f}"],
                    textfont=dict(size=8, color=COLORS["buy_line"]),
                    showlegend=False,
                    hoverinfo="skip"), row=1, col=1)

    period_str = ""
    if d_from: period_str += f" ({d_from}"
    if d_to:   period_str += f" ~ {d_to})"
    elif d_from: period_str += " ~ 현재)"

    fig.update_layout(
        title=f"📊 {nm} ({code}){period_str}",
        template="plotly_dark", height=640,
        paper_bgcolor=COLORS["bg"], plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["txt"]),
        xaxis_rangeslider_visible=False, showlegend=True,
        legend=dict(orientation="h", y=1.06, x=0, font=dict(size=9)),
        margin=dict(l=50, r=20, t=80, b=10))
    for rn in (1,2):
        fig.update_yaxes(row=rn, col=1, gridcolor=COLORS["grid"], zeroline=False)
    at=set(df["dt"]); cal=pd.bdate_range(df["dt"].min(), df["dt"].max())
    nt=[d for d in cal if d not in at]
    for rn in (1,2):
        fig.update_xaxes(row=rn, col=1, gridcolor=COLORS["grid"],
                         zeroline=False, showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2, col=1, tickformat="%y/%m/%d")
    return fig

# ── 메인 ──────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="HTS 스크린샷 차트 분석기",
                       page_icon="📸", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(_CSS, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## 📸 HTS 스크린샷 분석기")
        st.caption("0328 화면 스크린샷 → 차트 자동 분석")

        st.markdown("---")
        st.markdown("##### 📁 스크린샷 업로드")
        uploaded = st.file_uploader(
            "HTS 0328 화면 캡처 이미지",
            type=["png","jpg","jpeg","bmp"],
            help="키움 HTS 0328 종목별실현손익 화면을 캡처해서 업로드")

        st.markdown("---")
        st.markdown("##### 📅 차트 표시 기간")
        chart_period = st.selectbox("기간",
            ["3개월","6개월","1년","2년","전체"], index=2)
        period_days = {"3개월":90,"6개월":180,"1년":365,"2년":730,"전체":0}

        max_pages = st.slider("일봉 페이지수", 5, 80, 40, 5)

        st.markdown("---")
        st.markdown(f"""<div style="font-size:.72rem;color:#888;line-height:1.9">
        <b style="color:{COLORS['sell_profit']}">▼</b> 매도 (수익) &nbsp;
        <b style="color:{COLORS['sell_loss']}">▼</b> 매도 (손실)<br>
        숫자: 수익률%<br>
        <b style="color:{COLORS['buy_line']}">╌╌</b> 매입가 수평선<br><br>
        <b style="color:{COLORS['ma5']}">┅</b> MA5 &nbsp;
        <b style="color:{COLORS['ma20']}">━</b> MA20 &nbsp;
        <b style="color:{COLORS['ma60']}">╌</b> MA60
        </div>""", unsafe_allow_html=True)

        analyze = st.button("🔍 분석 실행",
                            use_container_width=True, type="primary",
                            disabled=(uploaded is None))

    # session state
    for k in ["hts_trades","hts_period"]:
        if k not in st.session_state: st.session_state[k] = None

    if analyze and uploaded:
        st.session_state.hts_trades = None

        img_bytes = uploaded.read()
        mime = uploaded.type or "image/png"

        # ── Step 1: Vision API로 데이터 추출 ──
        st.markdown("### 📸 이미지 분석 중...")
        with st.spinner("Claude Vision이 매매내역을 읽고 있습니다..."):
            try:
                trades = _extract_trades_from_image(img_bytes, mime)
            except Exception as e:
                st.error(f"이미지 분석 실패: {e}")
                return

        if not trades:
            st.error("매매내역을 추출하지 못했습니다. 다른 이미지를 업로드해 주세요.")
            return

        st.success(f"✅ {len(trades)}건 매매내역 추출 완료")

        # 추출 결과 미리보기
        with st.expander("📋 추출된 원본 데이터 확인", expanded=True):
            df_raw = pd.DataFrame([{
                "매도일": t.get("sell_date",""),
                "종목명": t.get("stk_nm",""),
                "수량": t.get("qty",""),
                "매입가": f"{_float(t.get('buy_price',0)):,.1f}",
                "매도가": f"{_float(t.get('sell_price',0)):,.0f}",
                "손익": f"{_float(t.get('pnl',0)):+,.0f}",
                "수익률": f"{_float(t.get('pnl_rate',0)):+.2f}%",
            } for t in trades])
            st.dataframe(df_raw, use_container_width=True, hide_index=True)

            # 수동 수정 안내
            st.caption("⚠️ 인식 오류가 있으면 아래에서 직접 수정 후 재분석 가능합니다.")

        # ── Step 2: 종목코드 변환 ──
        token = core.get_token(core.APP_KEY, core.APP_SECRET)
        code_map = {}
        with st.spinner("종목코드 변환 중..."):
            for t in trades:
                nm = t.get("stk_nm","")
                if nm and nm not in code_map:
                    code_map[nm] = _resolve_name(token, nm)

        # 코드 미확인 종목 수동 입력
        unknown = [nm for nm, cd in code_map.items() if not cd]
        if unknown:
            st.warning(f"아래 종목의 코드를 직접 입력해 주세요: {', '.join(unknown)}")
            for nm in unknown:
                code_map[nm] = st.text_input(f"{nm} 종목코드",
                                              placeholder="예: 005930",
                                              key=f"code_{nm}")

        # ── Step 3: 통계 요약 ──
        total_pnl  = sum(_float(t.get("pnl",0)) for t in trades)
        profit_cnt = sum(1 for t in trades if _float(t.get("pnl_rate",0)) >= 0)
        loss_cnt   = len(trades) - profit_cnt
        wr = (profit_cnt / len(trades) * 100) if trades else 0

        st.markdown("### 📊 매매 요약")
        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("총 매매건수", f"{len(trades)}건")
        c2.metric("수익 거래", f"{profit_cnt}건")
        c3.metric("손실 거래", f"{loss_cnt}건")
        c4.metric("승률", f"{wr:.0f}%")
        ic = "🟢" if total_pnl >= 0 else "🔴"
        c5.metric(f"{ic} 실현손익", f"{total_pnl:+,.0f}원")

        # ── Step 4: 종목별 차트 ──
        dd = period_days.get(chart_period, 0)

        # 종목 그룹화
        from collections import defaultdict
        by_stk = defaultdict(list)
        for t in trades:
            nm = t.get("stk_nm","")
            by_stk[nm].append(t)

        st.markdown(f"### 📈 종목별 차트 ({len(by_stk)}개 종목)")
        pg = st.progress(0)

        stk_list = sorted(by_stk.items(),
            key=lambda x: sum(_float(t.get("pnl",0)) for t in x[1]),
            reverse=True)

        for ci, (nm, tlist) in enumerate(stk_list):
            pg.progress((ci+1)/len(stk_list), f"차트 생성: {nm}")
            code = code_map.get(nm,"")
            total_p = sum(_float(t.get("pnl",0)) for t in tlist)
            cnt = len(tlist)
            ic2 = "🟢" if total_p >= 0 else "🔴"

            with st.expander(
                f"{ic2} {nm} ({code or '코드미확인'}) — {cnt}건 / {total_p:+,.0f}원",
                expanded=(ci < 3)):

                if not code:
                    st.warning("종목코드 미확인 — 사이드바에서 코드 입력 후 재실행")
                    continue

                # 차트 기간 계산
                sell_dates = sorted([t["sell_date"] for t in tlist if t.get("sell_date")])
                latest_sell = sell_dates[-1] if sell_dates else str(date_.today())
                if dd > 0:
                    chart_from = (datetime.strptime(latest_sell,"%Y-%m-%d")
                                  - timedelta(days=dd)).strftime("%Y-%m-%d")
                    chart_to = (datetime.strptime(latest_sell,"%Y-%m-%d")
                                + timedelta(days=30)).strftime("%Y-%m-%d")
                else:
                    chart_from = None
                    chart_to   = None

                with st.spinner(f"{nm} 차트 로딩..."):
                    try:
                        days = _fetch_daily(token, code, mp=max_pages)
                    except Exception as e:
                        st.warning(f"차트 로딩 실패: {e}"); continue

                if not days:
                    st.warning("일봉 데이터 없음"); continue

                fig = _chart(days, tlist, nm, code, chart_from, chart_to)
                st.plotly_chart(fig, use_container_width=True,
                    key=f"htsc_{code}_{ci}",
                    config={"displayModeBar":True,"displaylogo":False,
                            "scrollZoom":True,
                            "modeBarButtonsToRemove":["lasso2d","select2d"]})

                # 종목 상세
                df_s = pd.DataFrame([{
                    "매도일": t.get("sell_date",""),
                    "수량":   t.get("qty",""),
                    "매입가": f"{_float(t.get('buy_price',0)):,.1f}",
                    "매도가": f"{_float(t.get('sell_price',0)):,.0f}",
                    "손익":   f"{_float(t.get('pnl',0)):+,.0f}",
                    "수익률": f"{_float(t.get('pnl_rate',0)):+.2f}%",
                } for t in tlist])
                st.dataframe(df_s, use_container_width=True,
                             hide_index=True, key=f"htst_{code}_{ci}")

        pg.empty()

        # CSV 다운로드
        df_all = pd.DataFrame([{
            "매도일": t.get("sell_date",""),
            "종목명": t.get("stk_nm",""),
            "종목코드": code_map.get(t.get("stk_nm",""),""),
            "수량": t.get("qty",""),
            "매입가": _float(t.get("buy_price",0)),
            "매도가": _float(t.get("sell_price",0)),
            "손익": _float(t.get("pnl",0)),
            "수익률": _float(t.get("pnl_rate",0)),
        } for t in trades])
        csv = df_all.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 전체 내역 CSV", csv,
                           "hts_trades.csv", "text/csv")

        st.session_state.hts_trades  = trades
        st.session_state.hts_period  = chart_period

    else:
        if not uploaded:
            st.info("👈 HTS 0328 스크린샷을 업로드하고 **분석 실행**을 누르세요.")
            with st.expander("💡 사용 방법", expanded=True):
                st.markdown("""
**[1단계]** 키움 HTS 0328 화면을 캡처

**[2단계]** 사이드바에서 이미지 업로드

**[3단계]** 분석 실행 버튼 클릭

**[결과]**
- Claude Vision이 표 데이터 자동 추출
- 종목별 일봉 차트 표시
  - **▼ 초록**: 수익 매도 (수익률% 표시)
  - **▼ 빨강**: 손실 매도
  - **주황 점선**: 매입가 수평선 (언제 매수했는지 시각적 파악)
- 전체 내역 CSV 다운로드

**[주의]** 종목명 인식 오류 시 종목코드를 직접 입력할 수 있습니다.
                """)

if __name__ == "__main__": main()