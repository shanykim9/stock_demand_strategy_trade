"""
low_overcom_claude1.py — 저점 돌파 MA 스캐너 (v1)
═══════════════════════════════════════════════════════════════
실행: streamlit run low_overcom_claude1.py

[조건 1] 장기MA 연속 상승 5일 이상 (120 or 240)
[조건 2] 단기MA 상승 1일 이상 (20 or 40)
[조건 3] 하락MA 오늘 하락 (5 or 3)
[조건 4] 아래꼬리 음봉 + 다음날 양봉 종가 > 음봉 고가 → 시그널
[조건 5] 시그널 다음날: diff=양봉종가-음봉저가, 1차=저가+diff×3/4, 2차=×2/4, 3차=×1/4 (1:2:4)
[조건 6] 익절: 평균매수가 × (1 + tp%) 달성 시 즉시 매도
[조건 7] 손절: 종가 < 음봉 저가 즉시 손절

[자동모드] MA장기(120/240) × MA단기(20/40) × MA하락(5/3) × 익절률(5/7/10%) = 24케이스
"""
from __future__ import annotations
import os, math
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
    "bv": "#EF9A9A",   "bev": "#90CAF9",
    "ma3":  "#CE93D8", "ma5":  "#F48FB1",
    "ma20": "#FFD600", "ma40": "#FF9800",
    "ma120":"#26A69A", "ma240":"#FF7043",
    "bg": "#131722",   "grid": "#1E222D", "txt": "#D1D4DC",
    "sig": "#FF6D00",  "bull_sig": "#FFAB00",
    "buy1": "#00E676", "buy2": "#69F0AE", "buy3": "#B9F6CA",
    "tp": "#2196F3",   "sl": "#F44336",
}
PERIOD_OPTIONS = ["6개월","1년","1년6개월","2년","3년","5년","전체"]
PERIOD_MAP = {"6개월":180,"1년":365,"1년6개월":548,
              "2년":730,"3년":1095,"5년":1825,"전체":0}
CACHE_DIR = Path(".cache/low_overcom1"); CACHE_DIR.mkdir(parents=True, exist_ok=True)
TOTAL_INVEST = 3_000_000
BUY_RATIO = (1, 2, 4)
# 24케이스: MA장기 × MA단기 × MA하락 × 익절률
AUTO_CASES = [
    (lma, sma, dma, tp)
    for lma in [120, 240]
    for sma in [20, 40]
    for dma in [5, 3]
    for tp  in [5, 7, 10]
]

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
    if isinstance(v, int): return v
    s = str(v).strip().replace(",","").replace("+","").replace("-","",1).strip()
    if not s: return d
    try: return int(float(s))
    except: return d

def _pdt(v):
    if v is None: return None
    s = str(v).strip()
    if len(s)>=8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s)>=10 and s[4]=="-": return s[:10]
    return None

def _f(row, keys):
    for k in keys:
        if k in row and str(row.get(k)).strip()!="": return row.get(k)
    return None

def _resolve(q):
    q=(q or "").strip()
    if not q: raise RuntimeError("종목명 입력 필요")
    if q.isdigit() and len(q)==6: tk=q
    else:
        tk,err=core.resolve_ticker(q)
        if err or not tk: raise RuntimeError(err or f"'{q}' 확인 필요")
    nm=tk
    try: nm=(core._krx_cache.get("name_by_code") or {}).get(tk,tk)
    except: pass
    return tk, nm

def _parse_file(txt):
    return [t.strip() for t in txt.replace("\n",",").replace("\r",",").split(",") if t.strip()]

def _today(): return datetime.now(core.TZ).strftime("%Y-%m-%d")

def _intraday():
    now=datetime.now(core.TZ); return now<now.replace(hour=15,minute=50,second=0)

# ── 데이터 로딩 ───────────────────────────────────────────────
def _fetch_raw(token, ticker, mp=40):
    edt=datetime.now(core.TZ).strftime("%Y%m%d")
    stex=(os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd=(os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    cm={"stk_cd":ticker,"stex_tp":stex,"dmst_stex_tp":stex}
    for body in [{**cm,"base_dt":edt,"upd_stkpc_tp":upd},{**cm,"base_dt":edt},
                 {**cm,"dt":edt,"upd_stkpc_tp":upd},{**cm,"dt":edt}]:
        try:
            res=core.call_tr_all_pages(token=token,api_id="ka10081",body=body,
                                        endpoint="/api/dostk/chart",max_pages=mp)
            rows=res.get("rows") or []
            if not rows: continue
            dd={}
            for r in rows:
                dt=_pdt(_f(r,["dt","date","bas_dt","base_dt","trde_dt","trd_dt"]))
                if not dt: continue
                o=_int(_f(r,["open_pric","open","stck_oprc","opn_prc"]),0)
                h=_int(_f(r,["high_pric","high","stck_hgpr","hgh_prc"]),0)
                l=_int(_f(r,["low_pric","low","stck_lwpr","low_prc"]),0)
                c=_int(_f(r,["close_pric","close","stck_clpr","cur_prc","cur_pric"]),0)
                v=_int(_f(r,["trde_qty","volume","acml_vol","acc_trde_qty"]),0)
                if c<=0: continue
                if o<=0: o=c
                if h<=0: h=max(o,c)
                if l<=0: l=min(o,c)
                dd[dt]={"dt":dt,"open":o,"high":h,"low":l,"close":c,"volume":max(0,v)}
            out=sorted(dd.values(),key=lambda x:x["dt"])
            if out: return out
        except: continue
    raise RuntimeError("일봉 조회 실패")

def _cpath(tk): return CACHE_DIR/f"{tk}.parquet", CACHE_DIR/f"{tk}.csv"

def _load_c(tk):
    for p,rd in [(_cpath(tk)[0],lambda p:pd.read_parquet(p)),
                 (_cpath(tk)[1],lambda p:pd.read_csv(p))]:
        try:
            if p.exists():
                df=rd(p); df["dt"]=df["dt"].astype(str)
                if not df.empty: return df.to_dict("records")
        except: pass
    return []

def _save_c(tk, days):
    if not days: return
    pp,pc=_cpath(tk); df=pd.DataFrame(days)
    try: df.to_parquet(pp,index=False); return
    except: pass
    try: df.to_csv(pc,index=False,encoding="utf-8-sig")
    except: pass

def _fetch(token, tk, mp=40):
    today=_today(); intra=_intraday(); cached=_load_c(tk)
    if cached:
        last=cached[-1]["dt"]
        if intra:
            fresh=_fetch_raw(token,tk,min(mp,3)); m={d["dt"]:d for d in cached}
            for d in fresh: m[d["dt"]]=d
            out=sorted(m.values(),key=lambda x:x["dt"]); _save_c(tk,out); return out
        if last>=today: return cached
        try: delta=(datetime.strptime(today,"%Y-%m-%d")-datetime.strptime(last,"%Y-%m-%d")).days
        except: delta=30
        pg=min(mp,max(2,math.ceil((delta+10)/80))); fresh=_fetch_raw(token,tk,pg)
        m={d["dt"]:d for d in cached}
        for d in fresh: m[d["dt"]]=d
        out=sorted(m.values(),key=lambda x:x["dt"]); _save_c(tk,out); return out
    fresh=_fetch_raw(token,tk,mp); _save_c(tk,fresh); return fresh

# ── MA 계산 ───────────────────────────────────────────────────
def _ma(vals, w):
    out=[None]*len(vals); s=0.0
    for i,v in enumerate(vals):
        s+=float(v)
        if i>=w: s-=float(vals[i-w])
        if i>=w-1: out[i]=s/float(w)
    return out

def _ma_consec_up(mv, i, n):
    """i번째 기준 n일 연속 상승"""
    if i<n: return False
    for k in range(n):
        a,b=mv[i-k],mv[i-k-1]
        if a is None or b is None or a<=b: return False
    return True

def _ma_up_today(mv, i):
    """오늘 MA > 어제 MA"""
    if i<1: return False
    a,b=mv[i],mv[i-1]
    if a is None or b is None: return False
    return a>b

def _ma_down_today(mv, i):
    """오늘 MA < 어제 MA (하락)"""
    if i<1: return False
    a,b=mv[i],mv[i-1]
    if a is None or b is None: return False
    return a<b

# ── 핵심 분석 ─────────────────────────────────────────────────
def _analyze(days, long_ma_p, short_ma_p, decline_ma_p, tp_pct):
    """
    조건1: long_ma 5일 연속 상승
    조건2: short_ma 오늘 상승
    조건3: decline_ma 오늘 하락
    조건4: 전전봉=아래꼬리음봉, 전봉=양봉(종가>음봉고가) → 시그널
    조건5: 익절 tp_pct%
    조건6: 손절 음봉저가
    """
    n=len(days)
    if n<max(long_ma_p, short_ma_p, decline_ma_p)+10: return [],[],[]

    closes=[d["close"] for d in days]
    lmv=_ma(closes,long_ma_p)
    smv=_ma(closes,short_ma_p)
    dmv=_ma(closes,decline_ma_p)

    signals,trades,tlog=[],[],[]
    ST_IDLE,ST_BUY_WAIT,ST_POS="IDLE","BUY_WAIT","POS"
    state=ST_IDLE

    bear_dt=bull_dt=""
    bear_high=bear_low=bull_close=0.0
    buy1_p=buy2_p=buy3_p=sl_p=0.0
    tot_cost=0.0; tot_qty=0
    buy1_done=buy2_done=buy3_done=False
    first_buy_dt=""

    r_sum=sum(BUY_RATIO)
    amt1=int(TOTAL_INVEST*BUY_RATIO[0]/r_sum)
    amt2=int(TOTAL_INVEST*BUY_RATIO[1]/r_sum)
    amt3=int(TOTAL_INVEST*BUY_RATIO[2]/r_sum)

    def _reset():
        nonlocal state,bear_dt,bull_dt,bear_high,bear_low,bull_close
        nonlocal buy1_p,buy2_p,buy3_p,sl_p
        nonlocal tot_cost,tot_qty,buy1_done,buy2_done,buy3_done,first_buy_dt
        state=ST_IDLE; bear_dt=bull_dt=""
        bear_high=bear_low=bull_close=0.0
        buy1_p=buy2_p=buy3_p=sl_p=0.0
        tot_cost=0.0; tot_qty=0
        buy1_done=buy2_done=buy3_done=False; first_buy_dt=""

    def _avg(): return tot_cost/tot_qty if tot_qty>0 else 0.0

    def _mk_trade(sell_dt, sell_p, sell_type):
        avg=_avg(); sa=sell_p*tot_qty; pnl=sa-tot_cost
        roi=(sell_p/avg-1)*100 if avg else 0
        trades.append({"bear_dt":bear_dt,"bull_dt":bull_dt,
            "buy1_p":buy1_p,"buy2_p":buy2_p,"buy3_p":buy3_p,"sl_p":sl_p,
            "buy_dt":first_buy_dt,"avg_price":avg,
            "total_cost":tot_cost,"total_qty":tot_qty,
            "sell_dt":sell_dt,"sell_price":sell_p,
            "sell_type":sell_type,"sell_amount":sa,"pnl":pnl,"roi_pct":roi})

    for i in range(2,n):
        d=days[i]; o,h,l,c=d["open"],d["high"],d["low"],d["close"]

        # ── IDLE: 시그널 탐색 ──
        if state==ST_IDLE:
            if lmv[i] is None or smv[i] is None or dmv[i] is None: continue
            # 조건1: 장기MA 5일 연속 상승
            if not _ma_consec_up(lmv,i,5): continue
            # 조건2: 단기MA 오늘 상승
            if not _ma_up_today(smv,i): continue
            # 조건3: 하락MA 오늘 하락
            if not _ma_down_today(dmv,i): continue
            # 조건4: 전전봉=아래꼬리음봉, 전봉=양봉 종가>음봉고가
            p2=days[i-2]; p1=days[i-1]
            is_bear=(p2["close"]<p2["open"]) and (p2["low"]<p2["close"])
            if not is_bear: continue
            if not (p1["close"]>=p1["open"]): continue
            if p1["close"]<=p2["high"]: continue
            # 시그널 확정
            bear_dt=p2["dt"]; bull_dt=p1["dt"]
            bear_high=p2["high"]; bear_low=p2["low"]; bull_close=p1["close"]
            diff=bull_close-bear_low
            buy1_p=bear_low+diff*3/4
            buy2_p=bear_low+diff*2/4
            buy3_p=bear_low+diff*1/4
            sl_p=bear_low
            signals.append({"bear_dt":bear_dt,"bull_dt":bull_dt,"entry_dt":d["dt"],
                "bear_high":bear_high,"bear_low":bear_low,"bull_close":bull_close,
                "buy1_p":buy1_p,"buy2_p":buy2_p,"buy3_p":buy3_p,"sl_p":sl_p})
            tlog.append({"action":"시그널","dt":d["dt"],"price":0,"qty":0})
            state=ST_BUY_WAIT
            buy1_done=buy2_done=buy3_done=False
            tot_cost=0.0; tot_qty=0; first_buy_dt=""
            # continue 제거 → 시그널 당일 즉시 BUY_WAIT 체결 체크 fall-through

        # ── BUY_WAIT: 매수 체결 대기 ──
        if state==ST_BUY_WAIT:
            if c<sl_p:
                if tot_qty>0:
                    tlog.append({"action":"손절","dt":d["dt"],"price":c,"qty":tot_qty})
                    _mk_trade(d["dt"],c,"손절")
                else:
                    tlog.append({"action":"시그널취소","dt":d["dt"],"price":c,"qty":0})
                _reset(); continue
            if not buy1_done and l<=buy1_p:
                fp=min(o,buy1_p); q=int(amt1/fp) if fp>0 else 0
                if q>0:
                    buy1_done=True; tot_cost+=fp*q; tot_qty+=q
                    if not first_buy_dt: first_buy_dt=d["dt"]
                    tlog.append({"action":"매수1차","dt":d["dt"],"price":fp,"qty":q})
            if not buy2_done and l<=buy2_p:
                fp=min(o,buy2_p); q=int(amt2/fp) if fp>0 else 0
                if q>0:
                    buy2_done=True; tot_cost+=fp*q; tot_qty+=q
                    if not first_buy_dt: first_buy_dt=d["dt"]
                    tlog.append({"action":"매수2차","dt":d["dt"],"price":fp,"qty":q})
            if not buy3_done and l<=buy3_p:
                fp=min(o,buy3_p); q=int(amt3/fp) if fp>0 else 0
                if q>0:
                    buy3_done=True; tot_cost+=fp*q; tot_qty+=q
                    if not first_buy_dt: first_buy_dt=d["dt"]
                    tlog.append({"action":"매수3차","dt":d["dt"],"price":fp,"qty":q})
            if tot_qty>0: state=ST_POS
            continue

        # ── POS: 보유 중 ──
        if state==ST_POS:
            if not buy2_done and l<=buy2_p:
                fp=min(o,buy2_p); q=int(amt2/fp) if fp>0 else 0
                if q>0:
                    buy2_done=True; tot_cost+=fp*q; tot_qty+=q
                    tlog.append({"action":"매수2차","dt":d["dt"],"price":fp,"qty":q})
            if not buy3_done and l<=buy3_p:
                fp=min(o,buy3_p); q=int(amt3/fp) if fp>0 else 0
                if q>0:
                    buy3_done=True; tot_cost+=fp*q; tot_qty+=q
                    tlog.append({"action":"매수3차","dt":d["dt"],"price":fp,"qty":q})
            avg=_avg()
            if c<sl_p:
                tlog.append({"action":"손절","dt":d["dt"],"price":c,"qty":tot_qty})
                _mk_trade(d["dt"],c,"손절"); _reset(); continue
            tp_price=avg*(1+tp_pct/100.0)
            if h>=tp_price:
                tlog.append({"action":f"익절(+{tp_pct}%)","dt":d["dt"],"price":tp_price,"qty":tot_qty})
                _mk_trade(d["dt"],tp_price,f"익절(+{tp_pct}%)"); _reset(); continue

    if state==ST_POS and tot_qty>0:
        _mk_trade(days[-1]["dt"],days[-1]["close"],"보유중")
    return signals, trades, tlog

# ── 헬퍼 ──────────────────────────────────────────────────────
def _enrich(days, long_ma_p, short_ma_p, decline_ma_p):
    closes=[d["close"] for d in days]
    lmv=_ma(closes,long_ma_p)
    smv=_ma(closes,short_ma_p)
    dmv=_ma(closes,decline_ma_p)
    return [{**d,
             f"ma{long_ma_p}":lmv[i],
             f"ma{short_ma_p}":smv[i],
             f"ma{decline_ma_p}":dmv[i]}
            for i,d in enumerate(days)]

def _stats(trades):
    if not trades:
        return {"n":0,"w":0,"l":0,"h":0,"wr":0,"ar":0,"tp":0,"tl":0,"pnl":0}
    cl=[t for t in trades if t["sell_type"]!="보유중"]
    w=[t for t in cl if "익절" in t["sell_type"]]
    lo=[t for t in cl if "손절" in t["sell_type"]]
    ho=[t for t in trades if t["sell_type"]=="보유중"]
    wa=[t for t in cl if t["pnl"]>0]
    pr=sum(t["pnl"] for t in cl if t["pnl"]>0)
    ls=sum(t["pnl"] for t in cl if t["pnl"]<0)
    return {"n":len(trades),"w":len(w),"l":len(lo),"h":len(ho),
            "wr":(len(wa)/len(cl)*100) if cl else 0,
            "ar":(sum(t["roi_pct"] for t in cl)/len(cl)) if cl else 0,
            "tp":pr,"tl":ls,"pnl":pr+ls}

def _trade_df(trades):
    rows=[]
    for i,t in enumerate(trades):
        rows.append({"No":i+1,"음봉일":t["bear_dt"],"양봉일":t["bull_dt"],
            "매수일":t.get("buy_dt",""),
            "1차매수가":f"{t['buy1_p']:,.0f}","2차매수가":f"{t['buy2_p']:,.0f}",
            "3차매수가":f"{t['buy3_p']:,.0f}","손절가":f"{t['sl_p']:,.0f}",
            "평균매수가":f"{t['avg_price']:,.0f}" if t["avg_price"] else "-",
            "투자금":f"{t['total_cost']:,.0f}",
            "매도일":t["sell_dt"],"매도유형":t["sell_type"],
            "매도금":f"{t.get('sell_amount',0):,.0f}",
            "손익금":f"{t['pnl']:+,.0f}","수익률":f"{t['roi_pct']:+.1f}%"})
    return pd.DataFrame(rows)

# ── 기간 처리 ─────────────────────────────────────────────────
def _get_display_range(period_mode, cp, custom_from, custom_to):
    if period_mode=="날짜 직접 입력":
        return (str(custom_from) if custom_from else None,
                str(custom_to)   if custom_to   else None)
    days_back=PERIOD_MAP.get(cp,0)
    if days_back==0: return None, None
    return (datetime.now()-timedelta(days=days_back)).strftime("%Y-%m-%d"), None

def _filt_period(signals, trades, tlog, d_from, d_to):
    def ok(dt_str):
        if d_from and dt_str<d_from: return False
        if d_to   and dt_str>d_to:   return False
        return True
    return ([s for s in signals if ok(s["bear_dt"])],
            [t for t in trades  if ok(t["bear_dt"])],
            tlog)

# ── 차트 ──────────────────────────────────────────────────────
def _chart(en, signals, trades, tlog, name,
           long_ma_p, short_ma_p, decline_ma_p,
           disp_from=None, disp_to=None):
    df=pd.DataFrame(en); df["dt"]=pd.to_datetime(df["dt"])
    if disp_from: df=df[df["dt"]>=pd.to_datetime(disp_from)]
    if disp_to:   df=df[df["dt"]<=pd.to_datetime(disp_to)]
    df=df.copy().reset_index(drop=True)
    if df.empty:
        fig=go.Figure(); fig.add_annotation(text="데이터 없음",showarrow=False); return fig

    fig=make_subplots(rows=2,cols=1,shared_xaxes=True,
                      vertical_spacing=0.02,row_heights=[0.75,0.25])
    fig.add_trace(go.Candlestick(
        x=df["dt"],open=df["open"],high=df["high"],low=df["low"],close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]),fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]),fillcolor=COLORS["bear"]),
        name="일봉"),row=1,col=1)

    # MA 라인들
    ma_styles = [
        (f"ma{long_ma_p}",    f"MA{long_ma_p}",    COLORS.get(f"ma{long_ma_p}",  "#26A69A"), 1.5, "dash"),
        (f"ma{short_ma_p}",   f"MA{short_ma_p}",   COLORS.get(f"ma{short_ma_p}", "#FFD600"), 1.2, "solid"),
        (f"ma{decline_ma_p}", f"MA{decline_ma_p}",  COLORS.get(f"ma{decline_ma_p}","#F48FB1"), 1.0, "dot"),
    ]
    for col, nm_ma, clr, w, dash in ma_styles:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["dt"],y=df[col],name=nm_ma,
                line=dict(color=clr,width=w,dash=dash),hoverinfo="skip"),row=1,col=1)

    bull_mask=df["close"]>=df["open"]
    vc=[COLORS["bv"] if b else COLORS["bev"] for b in bull_mask]
    fig.add_trace(go.Bar(x=df["dt"],y=df["volume"],name="거래량",
        marker_color=vc,opacity=0.55),row=2,col=1)

    ds_str=df["dt"].dt.strftime("%Y-%m-%d")
    bear_dts={s["bear_dt"] for s in signals}
    bull_dts={s["bull_dt"] for s in signals}

    bd=df[ds_str.isin(bear_dts)]
    if not bd.empty:
        fig.add_trace(go.Scatter(x=bd["dt"],y=bd["low"]*0.97,mode="markers",
            name="◆시그널음봉",
            marker=dict(symbol="diamond",size=10,color=COLORS["sig"]),
            hovertemplate="%{x|%Y-%m-%d}<br>시그널음봉<extra></extra>"),row=1,col=1)

    bud=df[ds_str.isin(bull_dts)]
    if not bud.empty:
        fig.add_trace(go.Scatter(x=bud["dt"],y=bud["high"]*1.03,mode="markers",
            name="★시그널양봉",
            marker=dict(symbol="star",size=13,color=COLORS["bull_sig"],
                        line=dict(width=1,color="#FF6D00")),
            hovertemplate="%{x|%Y-%m-%d}<br>시그널양봉<extra></extra>"),row=1,col=1)

    for s in signals:
        x0=pd.to_datetime(s["bull_dt"]); x1=df["dt"].iloc[-1]
        if x0>=x1: continue
        for price,clr,dash in [
            (s["buy1_p"],COLORS["buy1"],"solid"),
            (s["buy2_p"],COLORS["buy2"],"dot"),
            (s["buy3_p"],COLORS["buy3"],"dashdot"),
            (s["sl_p"],  COLORS["sl"],  "dash"),
        ]:
            fig.add_shape(type="line",x0=x0,x1=x1,y0=price,y1=price,
                line=dict(color=clr,width=0.9,dash=dash),row=1,col=1)

    for e in tlog:
        edt=pd.to_datetime(e["dt"]); act=e.get("action",""); price=e.get("price",0)
        if "매수" in act and price>0:
            fig.add_trace(go.Scatter(x=[edt],y=[price*0.97],mode="markers",showlegend=False,
                marker=dict(symbol="triangle-up",size=11,color=COLORS["buy1"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{price:,.0f}<extra></extra>"),row=1,col=1)
        elif "익절" in act and price>0:
            fig.add_trace(go.Scatter(x=[edt],y=[price*1.03],mode="markers",showlegend=False,
                marker=dict(symbol="triangle-down",size=11,color=COLORS["tp"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{price:,.0f}<extra></extra>"),row=1,col=1)
        elif "손절" in act and price>0:
            fig.add_trace(go.Scatter(x=[edt],y=[price*1.03],mode="markers",showlegend=False,
                marker=dict(symbol="triangle-down",size=11,color=COLORS["sl"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{price:,.0f}<extra></extra>"),row=1,col=1)

    period_str=""
    if disp_from: period_str+=f" ({disp_from}"
    if disp_to:   period_str+=f" ~ {disp_to})"
    elif disp_from: period_str+=" ~ 현재)"

    fig.update_layout(
        title=f"📊 {name} — MA{long_ma_p}↑ MA{short_ma_p}↑ MA{decline_ma_p}↓{period_str}",
        template="plotly_dark",height=640,
        paper_bgcolor=COLORS["bg"],plot_bgcolor=COLORS["bg"],
        font=dict(color=COLORS["txt"]),
        xaxis_rangeslider_visible=False,showlegend=True,
        legend=dict(orientation="h",y=1.06,x=0,font=dict(size=9)),
        margin=dict(l=50,r=20,t=80,b=10))
    for rn in (1,2):
        fig.update_yaxes(row=rn,col=1,gridcolor=COLORS["grid"],zeroline=False)
    at=set(df["dt"]); cal=pd.bdate_range(df["dt"].min(),df["dt"].max())
    nt=[d for d in cal if d not in at]
    for rn in (1,2):
        fig.update_xaxes(row=rn,col=1,gridcolor=COLORS["grid"],zeroline=False,showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2,col=1,tickformat="%y/%m/%d")
    return fig

# ── 결과 표시 ─────────────────────────────────────────────────
def _show_result(nm, tk, en, signals, trades, tlog,
                 long_ma_p, short_ma_p, decline_ma_p,
                 disp_from, disp_to, tp_pct, ksuf=""):
    s=_stats(trades)
    c1,c2,c3,c4,c5,c6=st.columns(6)
    c1.metric("시그널",f"{len(signals)}건")
    c2.metric("트레이드",f"{s['n']}건")
    c3.metric("익절",f"{s['w']}건")
    c4.metric("손절",f"{s['l']}건")
    c5.metric("승률",f"{s['wr']:.0f}%")
    c6.metric("평균수익률",f"{s['ar']:+.1f}%")
    c7,c8,c9=st.columns(3)
    c7.metric("수익금",f"{s['tp']:+,.0f}원")
    c8.metric("손실금",f"{s['tl']:,.0f}원")
    ic="🟢" if s["pnl"]>=0 else "🔴"
    c9.metric(f"{ic} 순손익",f"{s['pnl']:+,.0f}원")
    if s["h"]>0: st.info(f"📌 보유중 {s['h']}건")

    fig=_chart(en,signals,trades,tlog,nm,long_ma_p,short_ma_p,decline_ma_p,disp_from,disp_to)
    st.plotly_chart(fig,use_container_width=True,key=f"lvc{ksuf}",
        config={"displayModeBar":True,"displaylogo":False,"scrollZoom":True,
                "modeBarButtonsToRemove":["lasso2d","select2d","autoScale2d"]})

    if trades:
        st.markdown("#### 📋 트레이드 상세")
        st.dataframe(_trade_df(trades),use_container_width=True,hide_index=True,key=f"lvt{ksuf}")
        csv=_trade_df(trades).to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 CSV",csv,f"{nm}_scan.csv","text/csv",key=f"lvd{ksuf}")
    elif signals:
        st.success(f"시그널 {len(signals)}개 탐지 (매수 미발생)")
    else:
        st.info("시그널 미탐지")

def _show_cases(cases, disp_from, disp_to, nm, tk, sort_roi=False):
    ordered=sorted(cases,key=lambda c:c["stats"]["ar"],reverse=True) if sort_roi else cases
    rows=[{"케이스":c["label"],"시그널":len(c["signals_f"]),"트레이드":c["stats"]["n"],
           "익절":c["stats"]["w"],"손절":c["stats"]["l"],
           "승률":f"{c['stats']['wr']:.0f}%",
           "평균수익률":f"{c['stats']['ar']:+.1f}%",
           "순손익":f"{c['stats']['pnl']:+,.0f}"} for c in ordered]
    st.markdown(f"#### 📊 {len(ordered)}케이스 요약")
    st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    for ci,c in enumerate(ordered):
        s=c["stats"]; ic="🟢" if s["ar"]>=0 else "🔴"
        with st.expander(
            f"{ic} {c['label']} — {s['n']}건 / 평균 {s['ar']:+.1f}% / {s['pnl']:+,.0f}원",
            expanded=False):
            _show_result(nm,tk,c["en"],c["signals_f"],c["trades_f"],c["tlog"],
                         c["long_ma"],c["short_ma"],c["decline_ma"],
                         disp_from,disp_to,c["tp_pct"],
                         ksuf=f"_ac{ci}{'s' if sort_roi else ''}")

# ── 메인 ──────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="저점돌파 스캐너 v1",page_icon="📈",
                       layout="wide",initial_sidebar_state="expanded")
    st.markdown(_CSS,unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("## ⚙️ 저점돌파 스캐너 v1")

        input_mode=st.radio("입력",["직접 입력","파일 업로드"],horizontal=True)
        ti=uf=None
        if input_mode=="직접 입력":
            ti=st.text_input("종목명/코드",placeholder="삼성전자")
        else:
            uf=st.file_uploader("종목목록(.txt/.md)",type=["txt","md"])

        st.markdown("---")
        st.markdown("##### 📐 MA 설정")
        ma_mode=st.radio("분석 모드",["수동","자동"],horizontal=True,
                         help="자동: MA장기×MA단기×MA하락×익절률 = 24케이스")

        if ma_mode=="수동":
            long_ma_p   = st.selectbox("조건1 — 장기MA (연속 5일↑)", [120,240],
                                        format_func=lambda x:f"MA{x}")
            short_ma_p  = st.selectbox("조건2 — 단기MA (오늘 상승)", [20,40],
                                        format_func=lambda x:f"MA{x}")
            decline_ma_p= st.selectbox("조건3 — 하락MA (오늘 하락)", [5,3],
                                        format_func=lambda x:f"MA{x}")
            tp_pct      = st.select_slider("익절률 (%)",options=list(range(5,16)),value=10)
        else:
            st.info("24케이스 자동 분석\nMA장기(120/240) × MA단기(20/40)\n× MA하락(5/3) × 익절(5/7/10%)")
            long_ma_p=120; short_ma_p=20; decline_ma_p=5; tp_pct=10

        st.markdown("---")
        st.caption(f"총 투자금 {TOTAL_INVEST:,}원 / 1차:2차:3차 = 1:2:4")

        st.markdown("---")
        st.markdown("##### 📅 차트/분석 기간")
        period_mode=st.radio("기간 설정",["기간 선택","날짜 직접 입력"],horizontal=True)
        cp=None; custom_from=custom_to=None
        if period_mode=="기간 선택":
            cp=st.selectbox("표시 기간",PERIOD_OPTIONS,index=3)
        else:
            col_f,col_t=st.columns(2)
            custom_from=col_f.date_input("시작일",value=date_(2022,1,1),
                                          min_value=date_(2000,1,1),max_value=date_.today())
            custom_to  =col_t.date_input("종료일",value=date_.today(),
                                          min_value=date_(2000,1,1),max_value=date_.today())

        max_pages=st.slider("OHLCV 페이지",10,80,40,5)

        st.markdown("---")
        st.markdown(f"""<div style="font-size:.72rem;color:#888;line-height:1.9">
        <b style="color:{COLORS['ma120']}">━</b> MA120 &nbsp;
        <b style="color:{COLORS['ma240']}">━</b> MA240<br>
        <b style="color:{COLORS['ma20']}">━</b> MA20 &nbsp;
        <b style="color:{COLORS['ma40']}">━</b> MA40<br>
        <b style="color:{COLORS['ma5']}">┅</b> MA5 &nbsp;
        <b style="color:{COLORS['ma3']}">┅</b> MA3 (하락조건)<br>
        <b style="color:{COLORS['sig']}">◆</b> 시그널음봉 &nbsp;
        <b style="color:{COLORS['bull_sig']}">★</b> 시그널양봉<br>
        <b style="color:{COLORS['buy1']}">▲</b> 매수 &nbsp;
        <b style="color:{COLORS['tp']}">▼</b> 익절 &nbsp;
        <b style="color:{COLORS['sl']}">▼</b> 손절
        </div>""",unsafe_allow_html=True)

        scan=st.button("🔍 스캔 실행",use_container_width=True,type="primary")

    # session state 초기화
    for k in ["lov1_results","lov1_mode","lov1_disp_from","lov1_disp_to",
              "lov1_nm","lov1_tk","lov1_sort_roi","lov1_multi_results"]:
        if k not in st.session_state: st.session_state[k]=None
    if not st.session_state.lov1_sort_roi:
        st.session_state.lov1_sort_roi=False

    disp_from,disp_to=_get_display_range(period_mode,cp,custom_from,custom_to)

    # ── 스캔 실행 ──────────────────────────────────────────────
    if scan:
        st.session_state.lov1_results=None
        st.session_state.lov1_multi_results=None
        st.session_state.lov1_sort_roi=False
        st.session_state.lov1_disp_from=disp_from
        st.session_state.lov1_disp_to=disp_to
        token=core.get_token(core.APP_KEY,core.APP_SECRET)

        # ══ 단일 종목 ══════════════════════════════════════════
        if input_mode=="직접 입력":
            if not ti: st.warning("종목명 입력 필요"); return
            try:
                tk,nm=_resolve(ti)
                st.session_state.lov1_nm=nm; st.session_state.lov1_tk=tk
                days=_fetch(token,tk,max_pages)
                st.markdown(f"### {nm} ({tk})")

                if ma_mode=="수동":
                    signals,trades,tlog=_analyze(days,long_ma_p,short_ma_p,decline_ma_p,tp_pct)
                    en=_enrich(days,long_ma_p,short_ma_p,decline_ma_p)
                    sig_f,trd_f,tlog_f=_filt_period(signals,trades,tlog,disp_from,disp_to)
                    _show_result(nm,tk,en,sig_f,trd_f,tlog_f,
                                 long_ma_p,short_ma_p,decline_ma_p,
                                 disp_from,disp_to,tp_pct,ksuf="_ms")
                    st.session_state.lov1_mode="single_manual"
                    st.session_state.lov1_results={
                        "nm":nm,"tk":tk,"en":en,
                        "signals_f":sig_f,"trades_f":trd_f,"tlog":tlog_f,
                        "long_ma":long_ma_p,"short_ma":short_ma_p,
                        "decline_ma":decline_ma_p,"tp_pct":tp_pct}
                else:
                    st.session_state.lov1_mode="single_auto"
                    cases=[]; summary_rows=[]
                    summary_ph=st.empty()
                    pg=st.progress(0)
                    for ci,(lma,sma,dma,tp) in enumerate(AUTO_CASES):
                        pg.progress((ci+1)/len(AUTO_CASES),
                                    f"케이스 {ci+1}/{len(AUTO_CASES)}: MA{lma}↑ MA{sma}↑ MA{dma}↓ +{tp}%")
                        signals,trades,tlog=_analyze(days,lma,sma,dma,tp)
                        en=_enrich(days,lma,sma,dma)
                        sig_f,trd_f,_=_filt_period(signals,trades,tlog,disp_from,disp_to)
                        s=_stats(trd_f)
                        label=f"MA{lma}↑ MA{sma}↑ MA{dma}↓ 익절{tp}%"
                        case={"label":label,"long_ma":lma,"short_ma":sma,"decline_ma":dma,
                              "tp_pct":tp,"en":en,"signals_f":sig_f,"trades_f":trd_f,
                              "tlog":tlog,"stats":s}
                        cases.append(case)
                        summary_rows.append({
                            "케이스":label,"시그널":len(sig_f),"트레이드":s["n"],
                            "익절":s["w"],"손절":s["l"],
                            "승률":f"{s['wr']:.0f}%",
                            "평균수익률":f"{s['ar']:+.1f}%",
                            "순손익":f"{s['pnl']:+,.0f}"})
                        summary_ph.dataframe(pd.DataFrame(summary_rows),
                                             use_container_width=True,hide_index=True)
                        ic="🟢" if s["ar"]>=0 else "🔴"
                        with st.expander(
                            f"{ic} 케이스 {ci+1}: {label} — {s['n']}건 / 평균 {s['ar']:+.1f}% / {s['pnl']:+,.0f}원",
                            expanded=False):
                            _show_result(nm,tk,en,sig_f,trd_f,tlog,lma,sma,dma,
                                         disp_from,disp_to,tp,ksuf=f"_ac{ci}")
                    pg.empty()
                    st.session_state.lov1_results=cases

            except Exception as e: st.error(f"오류: {e}"); st.exception(e)

        # ══ 다중 종목 ══════════════════════════════════════════
        else:
            if not uf: st.warning("파일 업로드 필요"); return
            qs=_parse_file(uf.read().decode("utf-8"))
            if not qs: st.error("종목 없음"); return

            if ma_mode=="수동":
                st.session_state.lov1_mode="multi_manual"
                results=[]; errors=[]
                st.info(f"📂 {len(qs)}개 종목 분석 중...")
                pg=st.progress(0)
                for idx,q in enumerate(qs):
                    pg.progress((idx+1)/len(qs),f"({idx+1}/{len(qs)}) {q}")
                    try:
                        tk,nm=_resolve(q)
                        days=_fetch(token,tk,max_pages)
                        signals,trades,tlog=_analyze(days,long_ma_p,short_ma_p,decline_ma_p,tp_pct)
                        en=_enrich(days,long_ma_p,short_ma_p,decline_ma_p)
                        sig_f,trd_f,_=_filt_period(signals,trades,tlog,disp_from,disp_to)
                        s=_stats(trd_f)
                        results.append({"q":q,"tk":tk,"nm":nm,"en":en,
                            "signals_f":sig_f,"trades_f":trd_f,"tlog":tlog,"stats":s,
                            "long_ma":long_ma_p,"short_ma":short_ma_p,
                            "decline_ma":decline_ma_p,"tp_pct":tp_pct})
                    except Exception as e:
                        errors.append({"종목":q,"오류":str(e)})
                pg.empty()

                total_pnl=sum(r["stats"]["pnl"] for r in results)
                m1,m2,m3,m4=st.columns(4)
                m1.metric("분석 종목",f"{len(results)}개")
                m2.metric("총 시그널",f"{sum(len(r['signals_f']) for r in results)}건")
                m3.metric("총 트레이드",f"{sum(r['stats']['n'] for r in results)}건")
                ic="🟢" if total_pnl>=0 else "🔴"
                m4.metric(f"{ic} 총 순손익",f"{total_pnl:+,.0f}원")

                ov=[{"종목명":r["nm"],"코드":r["tk"],
                     "시그널":len(r["signals_f"]),"트레이드":r["stats"]["n"],
                     "익절":r["stats"]["w"],"손절":r["stats"]["l"],
                     "승률":f"{r['stats']['wr']:.0f}%",
                     "평균수익률":f"{r['stats']['ar']:+.1f}%",
                     "순손익":f"{r['stats']['pnl']:+,.0f}"} for r in results]
                df_ov=pd.DataFrame(ov).sort_values("시그널",ascending=False)
                st.dataframe(df_ov,use_container_width=True,hide_index=True)
                csv=df_ov.to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
                st.download_button("📥 종합 CSV",csv,"lowovercom1_summary.csv","text/csv")

                for i,r in enumerate([x for x in results if x["signals_f"] or x["trades_f"]]):
                    s=r["stats"]; ic="🟢" if s["ar"]>=0 else "🔴"
                    with st.expander(f"{ic} {r['nm']}({r['tk']}) — {len(r['signals_f'])}건 / {s['pnl']:+,.0f}원"):
                        _show_result(r["nm"],r["tk"],r["en"],r["signals_f"],r["trades_f"],
                                     r["tlog"],long_ma_p,short_ma_p,decline_ma_p,
                                     disp_from,disp_to,tp_pct,ksuf=f"_mm{i}")
                if errors:
                    with st.expander(f"⚠️ 오류 {len(errors)}건"):
                        for e in errors: st.warning(f"{e['종목']}: {e['오류']}")
                st.session_state.lov1_multi_results=results

            else:  # 다중+자동
                st.session_state.lov1_mode="multi_auto"
                st.info(f"📂 {len(qs)}개 종목 × 24케이스 분석 중...")
                days_map={}; errors=[]
                pg0=st.progress(0)
                for idx,q in enumerate(qs):
                    pg0.progress((idx+1)/len(qs),f"데이터 수집 ({idx+1}/{len(qs)}) {q}")
                    try:
                        tk,nm=_resolve(q)
                        days_map[q]={"tk":tk,"nm":nm,"days":_fetch(token,tk,max_pages)}
                    except Exception as e:
                        errors.append({"종목":q,"오류":str(e)})
                pg0.empty()

                combo_rows=[]; combo_results=[]
                summary_ph=st.empty()
                pg=st.progress(0)
                for ci,(lma,sma,dma,tp) in enumerate(AUTO_CASES):
                    pg.progress((ci+1)/len(AUTO_CASES),
                                f"케이스 {ci+1}/24: MA{lma}↑ MA{sma}↑ MA{dma}↓ +{tp}%")
                    all_trd=[]
                    for q,info in days_map.items():
                        try:
                            _,trd,_=_analyze(info["days"],lma,sma,dma,tp)
                            _,trd_f,_=_filt_period([],trd,[],disp_from,disp_to)
                            all_trd.extend(trd_f)
                        except: pass
                    s=_stats(all_trd)
                    label=f"MA{lma}↑ MA{sma}↑ MA{dma}↓ 익절{tp}%"
                    combo_results.append({"label":label,"long_ma":lma,"short_ma":sma,
                                          "decline_ma":dma,"tp_pct":tp,"agg_stats":s})
                    combo_rows.append({"케이스":label,"트레이드":s["n"],
                        "익절":s["w"],"손절":s["l"],
                        "승률":f"{s['wr']:.0f}%",
                        "평균수익률":f"{s['ar']:+.1f}%",
                        "순손익":f"{s['pnl']:+,.0f}"})
                    summary_ph.dataframe(pd.DataFrame(combo_rows),
                                         use_container_width=True,hide_index=True)
                pg.empty()
                if errors:
                    with st.expander(f"⚠️ 오류 {len(errors)}건"):
                        for e in errors: st.warning(f"{e['종목']}: {e['오류']}")
                st.session_state.lov1_results=combo_results

    # ── 재표시 (sort 버튼 등) ───────────────────────────────────
    else:
        mode=st.session_state.get("lov1_mode")
        d_from=st.session_state.get("lov1_disp_from")
        d_to  =st.session_state.get("lov1_disp_to")

        def _sort_btn(key):
            lbl="🔀 원래 순서" if st.session_state.lov1_sort_roi else "📊 손익률 순서대로 표시"
            col_btn,_=st.columns([2,5])
            if col_btn.button(lbl,key=key):
                st.session_state.lov1_sort_roi=not st.session_state.lov1_sort_roi
                st.rerun()

        if mode=="single_auto" and st.session_state.lov1_results:
            nm=st.session_state.lov1_nm; tk=st.session_state.lov1_tk
            st.markdown(f"### {nm} ({tk})")
            _sort_btn("btn_sa")
            _show_cases(st.session_state.lov1_results,d_from,d_to,nm,tk,
                        sort_roi=st.session_state.lov1_sort_roi)

        elif mode=="single_manual" and st.session_state.lov1_results:
            r=st.session_state.lov1_results
            st.markdown(f"### {r['nm']} ({r['tk']})")
            _show_result(r["nm"],r["tk"],r["en"],r["signals_f"],r["trades_f"],r["tlog"],
                         r["long_ma"],r["short_ma"],r["decline_ma"],
                         d_from,d_to,r["tp_pct"],ksuf="_mr")

        elif mode=="multi_auto" and st.session_state.lov1_results:
            st.markdown("### 📊 다중 종목 × 24케이스 결과")
            _sort_btn("btn_ma")
            combos=st.session_state.lov1_results
            if st.session_state.lov1_sort_roi:
                combos=sorted(combos,key=lambda c:c["agg_stats"]["ar"],reverse=True)
            rows=[{"케이스":c["label"],"트레이드":c["agg_stats"]["n"],
                   "익절":c["agg_stats"]["w"],"손절":c["agg_stats"]["l"],
                   "승률":f"{c['agg_stats']['wr']:.0f}%",
                   "평균수익률":f"{c['agg_stats']['ar']:+.1f}%",
                   "순손익":f"{c['agg_stats']['pnl']:+,.0f}"} for c in combos]
            st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)

        elif mode=="multi_manual" and st.session_state.lov1_multi_results:
            _sort_btn("btn_mm")
            results=st.session_state.lov1_multi_results
            if st.session_state.lov1_sort_roi:
                results=sorted(results,key=lambda r:r["stats"]["ar"],reverse=True)
            for i,r in enumerate(results):
                if not r["signals_f"] and not r["trades_f"]: continue
                s=r["stats"]; ic="🟢" if s["ar"]>=0 else "🔴"
                with st.expander(f"{ic} {r['nm']}({r['tk']}) — {s['n']}건 / {s['pnl']:+,.0f}원"):
                    _show_result(r["nm"],r["tk"],r["en"],r["signals_f"],r["trades_f"],
                                 r["tlog"],r["long_ma"],r["short_ma"],r["decline_ma"],
                                 d_from,d_to,r["tp_pct"],ksuf=f"_sr{i}")

        else:
            st.info("👈 설정 후 **스캔 실행**을 누르세요.")
            with st.expander("💡 매매 로직 가이드",expanded=True):
                st.markdown("""
**[조건1]** 장기MA(120/240) 5일 연속 상승

**[조건2]** 단기MA(20/40) 오늘 상승

**[조건3]** 하락MA(5/3) 오늘 하락

**[조건4]** 시그널 (조건1+2+3 충족 전제)
- 음봉: close<open, low<close (아래꼬리 있는 음봉)
- 다음날 양봉 종가 > 음봉 고가 → 시그널

**[조건5]** 시그널 다음날부터 진입
- diff = 양봉종가 − 음봉저가
- 1차 = 음봉저가 + diff×3/4 (≈43만원)
- 2차 = 음봉저가 + diff×2/4 (≈86만원)
- 3차 = 음봉저가 + diff×1/4 (≈171만원)
- 총 300만원 / 비율 1:2:4

**[익절]** 평균매수가 × (1 + 익절률%) 도달 시 즉시 매도

**[손절]** 종가 < 음봉 저가 즉시 손절

**[자동모드]** MA장기(120/240) × MA단기(20/40) × MA하락(5/3) × 익절률(5/7/10%) = 24케이스
                """)

if __name__=="__main__": main()