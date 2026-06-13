"""
pattern_scan5.py — 5MA 반등 매수 시뮬레이션 (v5)
═══════════════════════════════════════════════════════════════
실행: streamlit run pattern_scan5.py

[0] 공통
    - 키움 REST API, OHLCV 캐시(parquet/csv)
    - 장중 당일봉 불완전 → 전일 봉까지만 판단
    - 단일 입력 / 파일 다중 입력

[1] 거래량 기준 (트리거용)
    차트표시기간 내 거래량 min/max 산출
    vol_threshold = min + (max - min) × 비율%
    시총 필터는 별도 유지 (ka10001 API)

[2] UI 파라미터
    - 차트표시기간 = 거래량 산출기간 (공유)
    - vol_threshold = min + (max-min) × 거래량비율%
    - MA 선택: 120 / 240
    - MA 상승: 240→5일연속, 120→10일연속
    - 종가/저가 선택 (price_ref)
    - 최대 감시기간(거래일) 기본 60
    - 상한 확정 옵션: peak_high only / max(peak, bear_high)
    - 투자금 설정 (기본 300만원)
    - 손절 기준: 5~10% 설정
    - 폭증 거래량 기준 없음 (트리거 거래량만 사용)

[3] 트리거 (v4 동일)
    양봉 + 거래량 >= vol_threshold + MA 연속 상승

[4] 하한기준가 / 상한기준가 (v4 동일)

[5] 기준선 (v4 동일)
    3/4 = lower + range * 3/4  (눌림 판단선)

[6] 상한 갱신 루프 (v4 동일)

[7] 눌림 시작
    종가(또는 저가) < 3/4선 → 눌림 시작 (ST_PULL 진입)

[8] 매수 시그널 (v5 핵심 변경)
    5MA가 하락하다가 상승 전환하는 양봉 발생 → 매수 시그널
    - 5MA[i-1] < 5MA[i-2] (전전일 대비 전일 5MA 하락)
    - 5MA[i] > 5MA[i-1]   (전일 대비 당일 5MA 상승)
    - 당일 양봉 (종가 >= 시가)
    → 다음날 시가에 매수

    음봉 발생 시: 매수하지 않고 다시 5MA 하락→상승 양봉 대기

[9] 매수
    매수 시그널 다음날 시가에 1차 매수 (투자금 전액)

[10] 익절
    일봉 종가 >= 5MA × 1.12 이고 양봉 → 다음날 시가에 전량 매도

[11] 손절
    매수가 대비 N% (5~10% 설정) 하락 시 당일 종가에 전량 매도

[12] 공통 규칙
    - 같은 날 매수+매도 금지 (매도 우선)
    - 익절/손절 후 동일 패턴 재진입 가능
    - 익절 매도와 함께 트리거가 발생하면 다시 매매 반복

[13] 자동 실행
    MA(120/240) × 거래량비율(10~100%) 조합 순차 실행
    요약 테이블: 조합별 익절성공율, 수익률, 누적손익

[14] 상태머신 흐름도
    IDLE → TRACK → MON → PULL → BUY_WAIT → POS → IDLE
    IDLE:      트리거 탐색 (양봉+거래량+MA상승)
    TRACK:     상한 추적 (양봉 고가 갱신)
    MON:       감시 (상한갱신/눌림진입)
    PULL:      눌림 중 5MA 반등 양봉 탐색
    BUY_WAIT:  다음날 시가 매수 대기
    POS:       보유 중 (익절/손절)
"""
from __future__ import annotations
import os, math
from datetime import datetime, timedelta
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
try:
    from dotenv import load_dotenv; load_dotenv()
except: pass
import demand as core

COLORS = {"bull":"#E53935","bear":"#1E88E5","bv":"#EF9A9A","bev":"#90CAF9",
    "trig":"#FFD600","upper":"#FF5252","lower":"#448AFF",
    "g34":"#FF9800","g12":"#AB47BC","g14":"#795548","g58":"#00BCD4","g78":"#E91E63",
    "buy":"#00E676","tp":"#2196F3","sl":"#F44336","surge":"#FF6D00",
    "ma120":"#26A69A","ma240":"#FF7043","ma5":"#FBC02D","bg":"#131722","grid":"#1E222D","txt":"#D1D4DC"}
PERIOD_OPTIONS=["6개월","1년","1년6개월","2년","3년","5년","전체"]
PERIOD_MAP={"6개월":180,"1년":365,"1년6개월":548,"2년":730,"3년":1095,"5년":1825,"전체":0}
CACHE_DIR=Path(".cache/pattern_scan5"); CACHE_DIR.mkdir(parents=True,exist_ok=True)

# ── 유틸 ──────────────────────────────────────────────────────────
def _int(v,d=0):
    if v is None: return d
    if isinstance(v,int): return v
    s=str(v).strip().replace(",","").replace("+","").replace("-","",1).strip()
    if not s: return d
    try: return int(float(s))
    except: return d

def _pdt(v):
    if v is None: return None
    s=str(v).strip()
    if len(s)>=8 and s[:8].isdigit(): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s)>=10 and s[4]=="-": return s[:10]
    return None

def _f(row,keys):
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
    return tk,nm

def _parse_file(txt):
    return [t.strip() for t in txt.replace("\n",",").replace("\r",",").split(",") if t.strip()]

def _today(): return datetime.now(core.TZ).strftime("%Y-%m-%d")

def _intraday():
    now=datetime.now(core.TZ); return now<now.replace(hour=15,minute=50,second=0)

# ── 시총 조회 (직접 HTTP 호출) ─────────────────────────────────────
def _get_cap(token,ticker):
    try:
        url=core.HOST+"/api/dostk/stkinfo"
        headers={
            "Content-Type":"application/json;charset=UTF-8",
            "authorization":f"Bearer {token}",
            "cont-yn":"N","next-key":"","api-id":"ka10001"
        }
        resp=core._KIWOOM_SESSION.post(url,headers=headers,json={"stk_cd":ticker},timeout=10)
        d=resp.json()
        raw=_f(d,["mac","mktc","market_cap","시가총액","mkt_cap","tot_mktc","stk_mktc"])
        return abs(_int(raw))
    except: return 0

# ── OHLCV 조회 ────────────────────────────────────────────────────
def _fetch_raw(token,ticker,mp=40):
    edt=datetime.now(core.TZ).strftime("%Y%m%d")
    stex=(os.getenv("KIWOOM_DMST_STEX_TP") or "KRX").strip().upper()
    upd=(os.getenv("KIWOOM_OHLCV_UPD_STKPC_TP") or "1").strip()
    cm={"stk_cd":ticker,"stex_tp":stex,"dmst_stex_tp":stex}
    for body in [{**cm,"base_dt":edt,"upd_stkpc_tp":upd},{**cm,"base_dt":edt},{**cm,"dt":edt,"upd_stkpc_tp":upd},{**cm,"dt":edt}]:
        try:
            res=core.call_tr_all_pages(token=token,api_id="ka10081",body=body,endpoint="/api/dostk/chart",max_pages=mp)
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

def _cpath(tk): return CACHE_DIR/f"{tk}.parquet",CACHE_DIR/f"{tk}.csv"

def _load_c(tk):
    for p,rd in [(_cpath(tk)[0],lambda p:pd.read_parquet(p)),(_cpath(tk)[1],lambda p:pd.read_csv(p))]:
        try:
            if p.exists():
                df=rd(p); df["dt"]=df["dt"].astype(str)
                if not df.empty: return df.to_dict("records")
        except: pass
    return []

def _save_c(tk,days):
    if not days: return
    pp,pc=_cpath(tk); df=pd.DataFrame(days)
    try: df.to_parquet(pp,index=False); return
    except: pass
    try: df.to_csv(pc,index=False,encoding="utf-8-sig")
    except: pass

def _fetch(token,tk,mp=40):
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

def _calc_vol_range(days,period_days):
    if not days: return 0,0
    if period_days>0:
        cutoff=(datetime.now()-timedelta(days=period_days)).strftime("%Y-%m-%d")
        subset=[d for d in days if d["dt"]>=cutoff]
    else: subset=days
    if not subset: subset=days
    vols=[d["volume"] for d in subset if d["volume"]>0]
    if not vols: return 0,0
    return min(vols),max(vols)

def _vol_threshold(vol_min,vol_max,pct): return vol_min+(vol_max-vol_min)*pct/100.0

def _ma(vals,w):
    out=[None]*len(vals); s=0.0
    for i,v in enumerate(vals):
        s+=float(v)
        if i>=w: s-=float(vals[i-w])
        if i>=w-1: out[i]=s/float(w)
    return out

def _ma_up(mv,i,n):
    if i<n: return False
    for k in range(n):
        a,b=mv[i-k],mv[i-k-1]
        if a is None or b is None or a<=b: return False
    return True

def _base_lower(days,ti):
    mn=days[ti]["open"]; i=ti-1
    while i>=0:
        d=days[i]
        if d["close"]>=d["open"]: mn=min(mn,d["open"]); i-=1; continue
        if i>=1 and d["close"]>days[i-1]["close"]: mn=min(mn,d["open"]); i-=1; continue
        break
    return mn

def _base_upper(days,si,use_bear_high):
    peak=days[si]["high"]
    for i in range(si+1,len(days)):
        d=days[i]; is_up=d["close"]>=d["open"]
        if not is_up and i>=1 and d["close"]>days[i-1]["close"]: is_up=True
        if is_up: peak=max(peak,d["high"]); continue
        if use_bear_high: return max(peak,d["high"]),i
        return peak,i
    return peak,len(days)-1

def _guides(lo,up):
    r=up-lo
    return {"lo":lo,"up":up,"rng":r,"p14":lo+r*0.25,"mid":lo+r*0.5,"p34":lo+r*0.75,"p58":lo+r*0.625,"p78":lo+r*0.875}

# ── 핵심 분석 함수 (v5) ────────────────────────────────────────────
def _analyze(days,ma_period,vol_thr,invest,price_ref,max_watch,use_bear_high,stop_loss_pct):
    n=len(days)
    if n<30: return [],[],[]
    closes=[d["close"] for d in days]
    opens=[d["open"] for d in days]
    highs=[d["high"] for d in days]
    lows=[d["low"] for d in days]
    vols=[d["volume"] for d in days]
    mv=_ma(closes,ma_period)   # 선택 MA (120 or 240)
    mv5=_ma(closes,5)          # 5MA (매수시그널/익절 판단용)
    ma_n=5 if ma_period==240 else 10

    dets,trades,tlog=[],[],[]
    ST_IDLE,ST_TRACK,ST_MON,ST_PULL,ST_BUY_WAIT,ST_POS="IDLE","TRACK","MON","PULL","BUY_WAIT","POS"

    state=ST_IDLE
    ti_idx=0; b_lo=b_up=0.0; g={}; peak_h=0.0
    above_34=True; det_info={}
    signal_idx=-1; signal_dt=""
    buy_p=0.0; avg_p=0.0; tot_cost=0.0; tot_qty=0; buy_dt=""

    def _reset():
        nonlocal state,ti_idx,b_lo,b_up,g,peak_h,above_34,det_info
        nonlocal signal_idx,signal_dt,buy_p,avg_p,tot_cost,tot_qty,buy_dt
        state=ST_IDLE; ti_idx=0; b_lo=b_up=0.0; g={}; peak_h=0.0; above_34=True; det_info={}
        signal_idx=-1; signal_dt=""; buy_p=0.0; avg_p=0.0; tot_cost=0.0; tot_qty=0; buy_dt=""

    def _mk_trade(sell_dt,sell_p,sell_type):
        nonlocal tot_cost,tot_qty,avg_p,buy_dt,signal_dt
        sa=sell_p*tot_qty; pnl=sa-tot_cost
        roi=(sell_p/avg_p-1)*100 if avg_p else 0
        trades.append({
            "trigger_dt":days[ti_idx]["dt"],
            "signal_dt":signal_dt,
            "base_lower":b_lo,"base_upper":b_up,
            "buy_dt":buy_dt,"avg_price":avg_p,
            "total_cost":tot_cost,"total_qty":tot_qty,
            "sell_dt":sell_dt,"sell_price":sell_p,
            "sell_type":sell_type,"sell_amount":sa,"pnl":pnl,"roi_pct":roi
        })
        tot_cost=0.0; tot_qty=0; avg_p=0.0; buy_dt=""

    for i in range(1,n):
        d=days[i]; o,h,l,c=d["open"],d["high"],d["low"],d["close"]; bull=c>=o
        ref=c if price_ref=="종가" else l

        # ── ST_POS: 보유 중 익절/손절 체크 ──────────────────────────
        if state==ST_POS:
            # 익절: 종가 >= 5MA × 1.12 이고 양봉 → 다음날 시가 매도
            if bull and mv5[i] is not None and c>=mv5[i]*1.12:
                if i+1<n:
                    sell_p=days[i+1]["open"]
                    tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"익절(5MA이격)","dt":days[i+1]["dt"],"price":sell_p,"qty":tot_qty})
                    _mk_trade(days[i+1]["dt"],sell_p,"익절(5MA이격)")
                else:
                    tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"익절(5MA이격)","dt":d["dt"],"price":c,"qty":tot_qty})
                    _mk_trade(d["dt"],c,"익절(5MA이격)")
                _reset()
                # 익절 후 당일 트리거 재탐색을 위해 IDLE 상태로 전환 후 아래 로직 계속 실행
                # (continue 없이 IDLE 상태로 떨어지도록 함)

            # 손절: 저가 <= 매수가 × (1 - stop_loss_pct/100)
            if state==ST_POS:  # 익절 안 된 경우에만 체크
                sl_p=avg_p*(1.0-stop_loss_pct/100.0)
                if l<=sl_p:
                    sell_p=min(o,sl_p)
                    tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"손절","dt":d["dt"],"price":sell_p,"qty":tot_qty})
                    _mk_trade(d["dt"],sell_p,"손절")
                    _reset()
                else:
                    continue  # 보유 중, 다음 봉으로

        # ── ST_BUY_WAIT: 매수 시그널 다음날 시가 매수 대기 ────────────
        if state==ST_BUY_WAIT:
            if i>signal_idx:
                buy_p=o; qty=int(invest/buy_p) if buy_p>0 else 0
                if qty>0:
                    avg_p=buy_p; tot_cost=buy_p*qty; tot_qty=qty; buy_dt=d["dt"]
                    tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"매수","dt":d["dt"],"price":buy_p,"qty":qty})
                    state=ST_POS
                else:
                    _reset()
            continue

        # ── ST_PULL: 눌림 중 5MA 반등 양봉 탐색 ─────────────────────
        if state==ST_PULL:
            # 최대 감시기간 초과 시 패턴 종료
            if i-ti_idx>max_watch:
                tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"패턴종료(감시기간초과)","dt":d["dt"],"price":c,"qty":0})
                _reset(); continue
            # 상한 돌파 시 재추적
            if c>b_up:
                peak_h=h; state=ST_TRACK; above_34=True; continue
            # 5MA 하락→상승 전환 양봉 탐색
            if i>=2 and mv5[i] is not None and mv5[i-1] is not None and mv5[i-2] is not None:
                if mv5[i-1]<mv5[i-2] and mv5[i]>mv5[i-1] and bull:
                    signal_idx=i; signal_dt=d["dt"]
                    tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"매수시그널(5MA반등)","dt":d["dt"],"price":c,"qty":0})
                    state=ST_BUY_WAIT
            continue

        # ── ST_MON: 감시 중 ──────────────────────────────────────────
        if state==ST_MON:
            # 최대 감시기간 초과
            if i-ti_idx>max_watch:
                tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"패턴종료(감시기간초과)","dt":d["dt"],"price":c,"qty":0})
                _reset(); continue
            # 상한 갱신 (고가가 상한 돌파했지만 종가는 상한 이하)
            if h>b_up and c<=b_up:
                b_up=h; g=_guides(b_lo,b_up); det_info["base_upper"]=b_up; det_info["guides"]=dict(g)
                if dets and dets[-1]["trigger_dt"]==days[ti_idx]["dt"]: dets[-1]["base_upper"]=b_up; dets[-1]["guides"]=dict(g)
                continue
            # 종가 > 상한 → 재추적
            if c>b_up:
                peak_h=h; state=ST_TRACK; above_34=True; continue
            # 눌림 시작: ref < 3/4선
            if ref<g["p34"]:
                above_34=False
                tlog.append({"trigger_dt":days[ti_idx]["dt"],"action":"눌림시작","dt":d["dt"],"price":ref,"qty":0})
                state=ST_PULL; continue
            continue

        # ── ST_TRACK: 상한 추적 ──────────────────────────────────────
        if state==ST_TRACK:
            is_up=bull
            if not is_up and i>=1 and c>closes[i-1]: is_up=True
            if is_up: peak_h=max(peak_h,h); continue
            if use_bear_high: b_up=max(peak_h,h)
            else: b_up=peak_h
            if b_up<=b_lo: _reset(); continue
            g=_guides(b_lo,b_up)
            det_info={"trigger_idx":ti_idx,"trigger_dt":days[ti_idx]["dt"],"confirm_dt":d["dt"],
                      "base_lower":b_lo,"base_upper":b_up,"guides":dict(g)}
            dets.append(det_info); state=ST_MON; continue

        # ── ST_IDLE: 트리거 탐색 ─────────────────────────────────────
        if state==ST_IDLE:
            if mv[i] is None: continue
            if not _ma_up(mv,i,ma_n): continue
            if not bull: continue
            if vols[i]<vol_thr: continue
            ti_idx=i; b_lo=_base_lower(days,i); peak_h=highs[i]; state=ST_TRACK
            above_34=True; signal_idx=-1; signal_dt=""; buy_p=0.0; avg_p=0.0
            tot_cost=0.0; tot_qty=0; buy_dt=""; det_info={}
            continue

    # 마지막 봉까지 보유 중이면 보유중으로 기록
    if state==ST_POS and tot_qty>0:
        _mk_trade(days[-1]["dt"],days[-1]["close"],"보유중")

    return dets,trades,tlog

# ── 데이터 보강 (MA 추가) ─────────────────────────────────────────
def _enrich(days,ma_p):
    if not days: return []
    c=[d["close"] for d in days]
    m=_ma(c,ma_p); m5=_ma(c,5)
    return [{**d,f"ma{ma_p}":m[i],"ma5":m5[i]} for i,d in enumerate(days)]

# ── 차트 ──────────────────────────────────────────────────────────
def _chart(en,dets,trades,tlog,name,ma_p,pd_=0):
    df=pd.DataFrame(en); df["dt"]=pd.to_datetime(df["dt"])
    if pd_>0: df=df[df["dt"]>=datetime.now()-timedelta(days=pd_)].copy().reset_index(drop=True)
    if df.empty: fig=go.Figure(); fig.add_annotation(text="데이터 없음",showarrow=False); return fig
    df["bull"]=df["close"]>=df["open"]; ts={d["trigger_dt"] for d in dets}
    ds=df["dt"].dt.strftime("%Y-%m-%d"); df["trig"]=ds.isin(ts)
    vc=[COLORS["trig"] if row["trig"] else (COLORS["bv"] if row["bull"] else COLORS["bev"]) for _,row in df.iterrows()]
    fig=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=0.02,row_heights=[0.75,0.25])
    fig.add_trace(go.Candlestick(x=df["dt"],open=df["open"],high=df["high"],low=df["low"],close=df["close"],
        increasing=dict(line=dict(color=COLORS["bull"]),fillcolor=COLORS["bull"]),
        decreasing=dict(line=dict(color=COLORS["bear"]),fillcolor=COLORS["bear"]),name="일봉"),row=1,col=1)
    mc=f"ma{ma_p}"
    if mc in df.columns:
        clr=COLORS["ma120"] if ma_p==120 else COLORS["ma240"]
        fig.add_trace(go.Scatter(x=df["dt"],y=df[mc],name=f"MA{ma_p}",line=dict(color=clr,width=1.2,dash="dash"),hoverinfo="skip"),row=1,col=1)
    if "ma5" in df.columns:
        fig.add_trace(go.Scatter(x=df["dt"],y=df["ma5"],name="MA5",line=dict(color=COLORS["ma5"],width=1),hoverinfo="skip"),row=1,col=1)
    fig.add_trace(go.Bar(x=df["dt"],y=df["volume"],name="거래량",marker_color=vc,opacity=0.55),row=2,col=1)
    dt_=df[df["trig"]]
    if not dt_.empty:
        fig.add_trace(go.Scatter(x=dt_["dt"],y=dt_["high"]*1.04,mode="markers",name="★트리거",
            marker=dict(symbol="star",size=14,color=COLORS["trig"],line=dict(width=1,color="#F9A825")),
            hovertemplate="%{x|%Y-%m-%d}<br><b>★트리거</b><extra></extra>"),row=1,col=1)
    for det in dets:
        gg=det.get("guides",{}); dt0=pd.to_datetime(det["trigger_dt"]); dt1=df["dt"].iloc[-1]
        for v,cl,da in [(det["base_upper"],COLORS["upper"],"dot"),(det["base_lower"],COLORS["lower"],"dot"),
            (gg.get("p34"),COLORS["g34"],"dashdot"),(gg.get("mid"),COLORS["g12"],"dash"),
            (gg.get("p14"),COLORS["g14"],"dashdot")]:
            if v: fig.add_shape(type="line",x0=dt0,x1=dt1,y0=v,y1=v,line=dict(color=cl,width=0.8,dash=da),row=1,col=1)
    for e in tlog:
        edt=pd.to_datetime(e["dt"]); act=e.get("action","")
        if "매수시그널" in act:
            fig.add_trace(go.Scatter(x=[edt],y=[e["price"]*1.04],mode="markers",showlegend=False,
                marker=dict(symbol="diamond",size=10,color="#FFAB00"),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{e['price']:,.0f}<extra></extra>"),row=1,col=1)
        elif "매수" in act and "시그널" not in act:
            fig.add_trace(go.Scatter(x=[edt],y=[e["price"]*0.97],mode="markers",showlegend=False,
                marker=dict(symbol="triangle-up",size=12,color=COLORS["buy"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{e['price']:,.0f}<extra></extra>"),row=1,col=1)
        elif "익절" in act:
            fig.add_trace(go.Scatter(x=[edt],y=[e["price"]*1.03],mode="markers",showlegend=False,
                marker=dict(symbol="triangle-down",size=12,color=COLORS["tp"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{e['price']:,.0f}<extra></extra>"),row=1,col=1)
        elif "손절" in act:
            fig.add_trace(go.Scatter(x=[edt],y=[e["price"]*1.03],mode="markers",showlegend=False,
                marker=dict(symbol="triangle-down",size=12,color=COLORS["sl"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{e['price']:,.0f}<extra></extra>"),row=1,col=1)
        elif "눌림시작" in act:
            fig.add_trace(go.Scatter(x=[edt],y=[e["price"]*1.02],mode="markers",showlegend=False,
                marker=dict(symbol="circle",size=8,color=COLORS["g34"]),
                hovertemplate=f"%{{x|%Y-%m-%d}}<br>{act}<br>{e['price']:,.0f}<extra></extra>"),row=1,col=1)
    fig.update_layout(title=f"📊 {name} — 5MA 반등 v5 (MA{ma_p})",template="plotly_dark",height=680,
        paper_bgcolor=COLORS["bg"],plot_bgcolor=COLORS["bg"],font=dict(color=COLORS["txt"]),
        xaxis_rangeslider_visible=False,showlegend=True,legend=dict(orientation="h",y=1.06,x=0,font=dict(size=9)),
        margin=dict(l=50,r=20,t=80,b=10))
    for rn in (1,2): fig.update_yaxes(row=rn,col=1,gridcolor=COLORS["grid"],zeroline=False)
    at=set(df["dt"]); cal=pd.bdate_range(df["dt"].min(),df["dt"].max()); nt=[d for d in cal if d not in at]
    for rn in (1,2):
        fig.update_xaxes(row=rn,col=1,gridcolor=COLORS["grid"],zeroline=False,showgrid=False,
                         rangebreaks=[dict(values=[d.strftime("%Y-%m-%d") for d in nt])])
    fig.update_xaxes(row=2,col=1,tickformat="%y/%m/%d"); return fig

# ── 필터/통계 ─────────────────────────────────────────────────────
def _filt(dets,trd,tlog,pd_):
    if pd_<=0: return dets,trd,tlog
    cut=(datetime.now()-timedelta(days=pd_)).strftime("%Y-%m-%d")
    fd=[d for d in dets if d["trigger_dt"]>=cut]; ft=[t for t in trd if t["trigger_dt"]>=cut]
    ts={t["trigger_dt"] for t in ft}; fl=[e for e in tlog if e["trigger_dt"] in ts]
    return fd,ft,fl

def _stats(trd):
    if not trd: return {"n":0,"w":0,"l":0,"h":0,"wr":0,"ar":0,"tp":0,"tl":0,"pnl":0}
    w=[t for t in trd if "익절" in t["sell_type"]]; lo=[t for t in trd if "손절" in t["sell_type"]]
    ho=[t for t in trd if "보유" in t["sell_type"] or "대기" in t["sell_type"]]
    cl=[t for t in trd if t["sell_type"] not in ("보유중","매수대기중")]
    pr=sum(t["pnl"] for t in cl if t["pnl"]>0); ls=sum(t["pnl"] for t in cl if t["pnl"]<0)
    wa=[t for t in cl if t["pnl"]>0]
    return {"n":len(trd),"w":len(w),"l":len(lo),"h":len(ho),
            "wr":(len(wa)/len(cl)*100) if cl else 0,"ar":(sum(t["roi_pct"] for t in cl)/len(cl)) if cl else 0,
            "tp":pr,"tl":ls,"pnl":pr+ls}

def _sdf(trd):
    rows=[]
    for i,t in enumerate(trd):
        pn=t.get("pnl",0)
        rows.append({"No":i+1,"트리거":t["trigger_dt"],"매수시그널":t.get("signal_dt",""),
            "하한":f"{t['base_lower']:,.0f}","상한":f"{t['base_upper']:,.0f}",
            "매수일":t.get("buy_dt",""),"평균매수가":f"{t['avg_price']:,.0f}" if t["avg_price"] else "-",
            "투자금":f"{t['total_cost']:,.0f}","매도일":t.get("sell_dt",""),"매도유형":t["sell_type"],
            "매도금":f"{t.get('sell_amount',0):,.0f}","손익금":f"{pn:+,.0f}","수익률":f"{t['roi_pct']:+.1f}%"})
    return pd.DataFrame(rows)

_CSS="""<style>
[data-testid="stMetric"]{background:linear-gradient(135deg,#1a1f2e,#151926);border:1px solid #2a2f42;border-radius:10px;padding:14px 18px}
[data-testid="stMetric"] label{color:#8b8fa3!important;font-size:.78rem!important}
[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#e8eaed!important;font-size:1.15rem!important;font-weight:600!important}
section[data-testid="stSidebar"]{background:#0f1117}
</style>"""

def _r1(nm,tk,en,dets,trd,tlog,map_,pd_,ksuf=""):
    dets,trd,tlog=_filt(dets,trd,tlog,pd_); s=_stats(trd)
    c1,c2,c3,c4,c5=st.columns(5)
    c1.metric("트레이드",f"{s['n']}건"); c2.metric("익절",f"{s['w']}건"); c3.metric("손절",f"{s['l']}건")
    c4.metric("승률",f"{s['wr']:.0f}%"); c5.metric("평균수익률",f"{s['ar']:+.1f}%")
    c6,c7,c8=st.columns(3); ic="🟢" if s["pnl"]>=0 else "🔴"
    c6.metric("수익금",f"{s['tp']:+,.0f}원"); c7.metric("손실금",f"{s['tl']:,.0f}원"); c8.metric(f"{ic} 순손익",f"{s['pnl']:+,.0f}원")
    if s["h"]>0: st.info(f"📌 보유/대기 {s['h']}건")
    fig=_chart(en,dets,trd,tlog,nm,map_,pd_); uk=f"{ksuf}_{tk}"
    st.plotly_chart(fig,use_container_width=True,key=f"c5{uk}",
        config={"displayModeBar":True,"displaylogo":False,"scrollZoom":True,"modeBarButtonsToRemove":["lasso2d","select2d","autoScale2d","toggleSpikelines"]})
    if trd:
        st.markdown("#### 📋 트레이드 상세")
        st.dataframe(_sdf(trd),use_container_width=True,hide_index=True,key=f"t5{uk}")
        csv=_sdf(trd).to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 CSV",csv,f"{nm}_scan5.csv","text/csv",key=f"d5{uk}")
    elif dets: st.success(f"패턴 {len(dets)}개 (매수 미발생)")
    else: st.info("패턴 미탐지")

def _rm(results,map_,pd_,ksuf=""):
    gp=gl=gn=gw=glo=0; ov=[]
    for it in results:
        if it.get("error"): continue
        _,ft,_=_filt(it.get("dets",[]),it["trd"],it.get("tlog",[]),pd_); s=_stats(ft)
        gp+=s["tp"]; gl+=s["tl"]; gn+=s["n"]; gw+=s["w"]; glo+=s["l"]
        if s["n"]==0 and not it.get("dets"): continue
        ov.append({"종목":f"{it['name']}({it['tk']})","패턴":len(it.get("dets",[])),"트레이드":s["n"],"익절":s["w"],"손절":s["l"],
                   "승률":f"{s['wr']:.0f}%","평균수익률":f"{s['ar']:+.1f}%","수익금":f"{s['tp']:+,.0f}","손실금":f"{s['tl']:,.0f}","순손익":f"{s['pnl']:+,.0f}"})
    gpnl=gp+gl; st.markdown("#### 📊 전체 종합")
    g1,g2,g3,g4,g5,g6=st.columns(6)
    g1.metric("총트레이드",f"{gn}건"); g2.metric("총익절",f"{gw}건"); g3.metric("총손절",f"{glo}건")
    g4.metric("수익금",f"{gp:+,.0f}원"); g5.metric("손실금",f"{gl:,.0f}원")
    ic="🟢" if gpnl>=0 else "🔴"; g6.metric(f"{ic} 총순손익",f"{gpnl:+,.0f}원")
    if ov:
        st.dataframe(pd.DataFrame(ov),use_container_width=True,hide_index=True)
        csv=pd.DataFrame(ov).to_csv(index=False,encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 종합CSV",csv,"scan5_summary.csv","text/csv",key=f"gcsv{ksuf}")
    errs=[r for r in results if r.get("error")]
    if errs:
        with st.expander(f"⚠️ 오류 {len(errs)}건"):
            for e in errs: st.warning(f"{e['query']}: {e['error']}")

def _auto_run(token,queries,infos,days_map,vol_pcts,ma_list,invest,price_ref,max_watch,use_bear_high,cd,stop_loss_pct):
    rows=[]; combos=[(ma,vp) for ma in ma_list for vp in vol_pcts]
    pg=st.progress(0)
    for ci,(ma_p,vp) in enumerate(combos):
        pg.progress((ci+1)/len(combos),f"MA{ma_p}/{vp}%")
        all_trades=[]
        for q in queries:
            info=infos.get(q)
            if not info or info.get("error"): continue
            days=days_map.get(q)
            if not days: continue
            vm,vx=info.get("vol_min",0),info.get("vol_max",0)
            if vx<=0: vm,vx=_calc_vol_range(days,cd)
            if vx<=0: continue
            vt=int(_vol_threshold(vm,vx,vp))
            if vt<=0: continue
            _,trd,_=_analyze(days,ma_p,vt,invest,price_ref,max_watch,use_bear_high,stop_loss_pct)
            if cd>0: _,trd,_=_filt([],trd,[],cd)
            all_trades.extend(trd)
        s=_stats(all_trades)
        rows.append({"MA":f"MA{ma_p}","거래비율":f"{vp}%",
            "트레이드":s["n"],"익절":s["w"],"손절":s["l"],
            "익절성공율":f"{(s['w']/s['n']*100):.1f}%" if s["n"] else "-",
            "평균수익률":f"{s['ar']:+.1f}%","누적수익":f"{s['tp']:+,.0f}","누적손실":f"{s['tl']:,.0f}","누적손익":f"{s['pnl']:+,.0f}"})
    pg.empty()
    if rows: st.markdown("#### 🤖 자동 실행 결과"); st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    else: st.warning("분석 결과 없음")

# ── 메인 ──────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="패턴 스캔 v5",page_icon="📊",layout="wide",initial_sidebar_state="expanded")
    st.markdown(_CSS,unsafe_allow_html=True)
    with st.sidebar:
        st.markdown("## ⚙️ 5MA 반등 스캔 v5")
        input_mode=st.radio("입력 방식",["직접 입력","파일 업로드"],horizontal=True)
        ti=uf=None
        if input_mode=="직접 입력": ti=st.text_input("종목명/코드",placeholder="삼성전자")
        else: uf=st.file_uploader("종목 목록(.txt/.md)",type=["txt","md"])
        st.markdown("---"); st.markdown("##### 📐 기본")
        cp=st.selectbox("차트 표시 기간",PERIOD_OPTIONS,index=len(PERIOD_OPTIONS)-1,help="거래량 min/max 산출 기간도 동일 적용")
        min_cap=st.number_input("최소 시총(억원)",100,100000,1000,100)
        st.markdown("---")
        auto_mode=st.checkbox("🤖 자동 실행 모드",value=False,help="MA × 거래비율 조합 자동 분석")
        if not auto_mode:
            ma_p=st.selectbox("이동평균선",[240,120],format_func=lambda x:f"MA{x}")
            trade_pct=st.select_slider("거래량 기준 비율(%)",options=list(range(10,110,10)),value=30,
                help="기간 내 거래량 min(0%)~max(100%) 범위에서 비율 위치")
        else:
            st.markdown("##### 🤖 자동 실행 설정")
            auto_ma=st.multiselect("MA 선택",[120,240],default=[120,240],format_func=lambda x:f"MA{x}")
            auto_pcts=st.multiselect("거래량 기준 비율(%)",list(range(10,110,10)),default=[10,30,50])
            ma_p=240; trade_pct=30
        st.markdown("---"); st.markdown("##### 💰 매수 설정")
        invest=st.number_input("총 투자금(원)",100_000,100_000_000,3_000_000,100_000)
        stop_loss_pct=st.slider("손절 기준 (%)",5,10,5,1,help="매수가 대비 손절 비율")
        price_ref=st.radio("눌림 판단 기준",["종가","저가"],horizontal=True)
        st.markdown("---"); st.markdown("##### ⚙️ 고급")
        max_watch=st.number_input("최대 감시기간(거래일)",10,500,60,10)
        use_bear_high=st.checkbox("상한 확정 시 음봉고가 포함",value=True,help="OFF: peak_high만 / ON: max(peak, 음봉고가)")
        max_pages=st.slider("OHLCV 페이지",10,80,40,5)
        st.markdown("---")
        st.markdown(f"""<div style="font-size:.72rem;color:#888;line-height:1.8">
        <b style="color:#FFD600">★</b> 트리거: 양봉+거래량(기간min~max%)+MA상승<br>
        <b style="color:#FF9800">●</b> 눌림시작: ref &lt; 3/4선<br>
        <b style="color:{COLORS['buy']}">▲</b> 매수시그널: 5MA 하락→상승 전환 양봉<br>
        <b style="color:{COLORS['buy']}">▲</b> 매수: 시그널 다음날 시가<br>
        <b style="color:{COLORS['tp']}">▼</b> 익절: 종가 ≥ 5MA×1.12 양봉 → 다음날 시가<br>
        <b style="color:{COLORS['sl']}">▼</b> 손절: 매수가 대비 {stop_loss_pct}% 하락<br><br>
        <b>재진입</b>: 익절/손절 후 패턴 반복 가능</div>""",unsafe_allow_html=True)
        scan=st.button("🔍 스캔 실행",use_container_width=True,type="primary")

    for k in ["s5r","s5m","s5p"]:
        if k not in st.session_state: st.session_state[k]=None

    cd=PERIOD_MAP.get(cp,0)
    if scan:
        queries=[]
        if input_mode=="직접 입력":
            if ti: queries=[ti.strip()]
        else:
            if uf: queries=_parse_file(uf.read().decode("utf-8","ignore"))
        if not queries: st.warning("종목을 입력하세요."); st.stop()

        _appkey=os.getenv("KIWOOM_APP_KEY") or os.getenv("APP_KEY") or ""
        _secretkey=os.getenv("KIWOOM_APP_SECRET") or os.getenv("APP_SECRET") or ""
        if not _appkey or not _secretkey:
            st.error("환경변수 KIWOOM_APP_KEY / KIWOOM_APP_SECRET 가 설정되지 않았습니다. .env 파일을 확인하세요."); st.stop()
        token=core.get_token(_appkey,_secretkey)
        infos={}; days_map={}
        prog_area=st.empty()
        with st.spinner("데이터 수집 중..."):
            for qi,q in enumerate(queries):
                prog_area.info(f"📡 데이터 수집 중... [{qi+1}/{len(queries)}] {q}")
                try:
                    tk,nm=_resolve(q)
                    cap=_get_cap(token,tk)
                    if cap>0 and cap<min_cap:
                        infos[q]={"error":f"시총 {cap:,}억 < 최소 {min_cap:,}억"}; continue
                    days=_fetch(token,tk,max_pages)
                    if _intraday() and len(days)>1: days=days[:-1]
                    vm,vx=_calc_vol_range(days,cd)
                    infos[q]={"tk":tk,"name":nm,"cap":cap,"vol_min":vm,"vol_max":vx}
                    days_map[q]=days
                except Exception as e:
                    infos[q]={"error":str(e)}
        prog_area.empty()

        if auto_mode:
            if not auto_ma: st.warning("MA를 선택하세요."); st.stop()
            if not auto_pcts: st.warning("거래량 비율을 선택하세요."); st.stop()
            _auto_run(token,queries,infos,days_map,auto_pcts,auto_ma,invest,price_ref,max_watch,use_bear_high,cd,stop_loss_pct)
        else:
            vt_val=int(_vol_threshold(
                infos.get(queries[0],{}).get("vol_min",0) if len(queries)==1 else 0,
                infos.get(queries[0],{}).get("vol_max",0) if len(queries)==1 else 0,
                trade_pct)) if len(queries)==1 else 0

            results=[]
            ok_qs=[q for q in queries if infos.get(q) and not infos[q].get("error")]
            err_qs=[q for q in queries if not infos.get(q) or infos[q].get("error")]
            for e in err_qs:
                results.append({"query":e,"error":(infos.get(e) or {}).get("error","데이터 없음"),"trd":[],"dets":[],"tlog":[]})
            ana_prog=st.empty(); ana_bar=st.progress(0)
            for qi,q in enumerate(ok_qs):
                info=infos[q]
                ana_prog.info(f"🔍 분석 중... [{qi+1}/{len(ok_qs)}] {info['name']} ({info['tk']})")
                ana_bar.progress((qi+1)/max(len(ok_qs),1))
                days=days_map.get(q,[])
                if not days:
                    results.append({"query":q,"error":"일봉 없음","trd":[],"dets":[],"tlog":[]}); continue
                vm,vx=info.get("vol_min",0),info.get("vol_max",0)
                if vx<=0: vm,vx=_calc_vol_range(days,cd)
                vt=int(_vol_threshold(vm,vx,trade_pct))
                dets,trd,tlog=_analyze(days,ma_p,vt,invest,price_ref,max_watch,use_bear_high,stop_loss_pct)
                en=_enrich(days,ma_p)
                results.append({"query":q,"tk":info["tk"],"name":info["name"],"enriched":en,"dets":dets,"trd":trd,"tlog":tlog})
            ana_prog.empty(); ana_bar.empty()

            st.session_state.s5r=results; st.session_state.s5m=ma_p; st.session_state.s5p=cd

    if st.session_state.s5r:
        results=st.session_state.s5r; map_=st.session_state.s5m; pd_=st.session_state.s5p
        errs=[r for r in results if r.get("error")]
        oks=[r for r in results if not r.get("error")]
        if len(oks)>1:
            _rm(results,map_,pd_,f"{map_}_{trade_pct if not auto_mode else 'auto'}")
            st.markdown("---")
            for it in oks:
                with st.expander(f"📈 {it['name']} ({it['tk']})",expanded=False):
                    _r1(it["name"],it["tk"],it["enriched"],it["dets"],it["trd"],it["tlog"],map_,pd_,f"{map_}_{trade_pct if not auto_mode else 'auto'}")
        elif len(oks)==1:
            it=oks[0]
            _r1(it["name"],it["tk"],it["enriched"],it["dets"],it["trd"],it["tlog"],map_,pd_,f"{map_}_{trade_pct if not auto_mode else 'auto'}")
        if errs:
            with st.expander(f"⚠️ 오류 {len(errs)}건"):
                for e in errs: st.warning(f"{e['query']}: {e['error']}")

if __name__=="__main__":
    main()
