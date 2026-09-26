"""세금을 다음 해 5월에 낼 때의 세후 배수. ⚠️ 입금이 있는 구간(5년)은 원본과 값이 안 맞아 신뢰 불가 — 추가입금 없는 전체 구간만 유효(현행 59.79배 재현). docs/STRATEGY.md 5.4.

실험 스크립트(2026-09-25). src/ 는 건드리지 않는다. 프로젝트 루트에서 실행:
    ./.venv/bin/python scripts/tax_timing.py
"""
import sys; sys.path.insert(0,"src")
import pandas as pd, strategy, backtest
from strategy import Params, build_signals
from risk_manager import RiskConfig
from data_fetcher import fetch_history
p=Params(); risk=RiskConfig(total_capital_usd=5000.0, fee_rate=0.001)
s=fetch_history(p.tickers["signal"],"max"); L={k:fetch_history(p.tickers[k],"max") for k in("long","short")}
c=s.index.intersection(L["long"].index).intersection(L["short"].index)
s=s.loc[c]; L={k:v.loc[c] for k,v in L.items()}
sig=build_signals(s,p).dropna(subset=["z","regime"])
orig=strategy.lt_veto
def run(f):
    strategy.lt_veto=f
    return backtest.run_backtest(sig,L,p,risk)[1]
tn=run(lambda a,b:False); tb=run(orig)
import dataclasses
def key(t): return (str(t.entry_date)[:10],t.side)
sys.path.insert(0,"scripts")
from build_dashboard import after_tax_annual
def sim(curve,inj,defer,capital=5000.0):
    injs=pd.Series([a for _,a in inj],index=pd.DatetimeIndex([d for d,_ in inj])) if len(inj) else pd.Series(dtype=float)
    ye=curve.resample("YE").last(); y=pd.concat([pd.Series([capital],index=[curve.index[0]]),ye])
    V=capital; tin=capital; taxes=0.0; pending=0.0; paid_prev=None
    idx=curve.index; vals=curve.values
    for k in range(len(y)-1):
        d0,a=y.index[k],y.values[k]; d1,b=y.index[k+1],y.values[k+1]
        added=float(injs[(injs.index>d0)&(injs.index<=d1)].sum()) if len(injs) else 0.0
        tin+=added
        seg=(idx>d0)&(idx<=d1) if k>0 else (idx<=d1)
        Vs=V; paid=0.0; prev=a
        may=pd.Timestamp(d1.year,5,31) if defer else None
        did=False
        for dt,c in zip(idx[seg],vals[seg]):
            V*=c/prev; prev=c
            if defer and not did and pending>0 and dt>=may:
                V-=pending; paid+=pending; pending=0.0; did=True
        if defer and pending>0:   # 그 해 5/31 이전에 구간이 끝난 경우(마지막 해 등)
            V-=pending; paid+=pending; pending=0.0
        G=V+paid-Vs-added if False else (V+paid-Vs)   # 입금 미반영 (원본과 같은 조건이 되도록 아래서 보정)
        G=Vs*(b/a)-Vs-added
        tax=max(0.0,G-1800)*0.22 if G>0 else 0.0
        V+=added
        if defer: pending=tax
        else: V-=tax
        taxes+=tax
    V-=pending
    return V/tin,taxes
for label,years in(("5y",5),("전체",None)):
    sg=sig if years is None else sig[sig.index>=sig.index[-1]-pd.DateOffset(years=years)]
    L2={k:v.loc[sg.index] for k,v in L.items()}
    strategy.lt_veto=orig
    cv,tr,inj=backtest.run_backtest(sg,L2,p,risk)
    pre=cv.iloc[-1]/(5000+sum(a for _,a in inj))
    print(label,"세전 %.2f | 원본함수 %.2f | 내엔진(즉시) %.2f | 내엔진(다음해5월) %.2f"%(pre,after_tax_annual(cv,inj),sim(cv,inj,False)[0],sim(cv,inj,True)[0]))
