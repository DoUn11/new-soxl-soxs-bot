"""장기 추세 거부권을 방향별로 켜고 끈 백테스트 (없음 / 양방향 / 롱만 / 숏만 × 5개 구간). docs/STRATEGY.md 4.6.1.

실험 스크립트(2026-09-25). src/ 는 건드리지 않는다. 프로젝트 루트에서 실행:
    ./.venv/bin/python scripts/backtest_veto_sides.py
"""
import sys; sys.path.insert(0,"src")
import pandas as pd, strategy, backtest
from strategy import Params, build_signals
from regime import UP, DOWN
from risk_manager import RiskConfig
from data_fetcher import fetch_history
p=Params(); risk=RiskConfig(total_capital_usd=5000.0, fee_rate=0.001)
s=fetch_history(p.tickers["signal"],"max"); L={k:fetch_history(p.tickers[k],"max") for k in("long","short")}
c=s.index.intersection(L["long"].index).intersection(L["short"].index)
s=s.loc[c]; L={k:v.loc[c] for k,v in L.items()}
sig=build_signals(s,p).dropna(subset=["z","regime"])
orig=strategy.lt_veto
V={"none":lambda side,row:False,"both":orig,
 "long-only(하락추세서 롱차단)":lambda side,row: side=="long" and row.get("lt_trend")==DOWN,
 "short-only(상승추세서 숏차단)":lambda side,row: side=="short" and row.get("lt_trend")==UP}
end=sig.index[-1]
wins={"학습2.5":(end-pd.DateOffset(years=5),end-pd.DateOffset(months=30)),"검증2.5":(end-pd.DateOffset(months=30),end),
 "5y":(end-pd.DateOffset(years=5),end),"3y":(end-pd.DateOffset(years=3),end),"전체":(sig.index[0],end)}
print("%-28s"%"", *["%9s"%w for w in wins])
for n,f in V.items():
    strategy.lt_veto=f
    row=[]
    for a,b in wins.values():
        sg=sig[(sig.index>=a)&(sig.index<=b)]; lg={k:v.loc[sg.index] for k,v in L.items()}
        cv,tr,inj=backtest.run_backtest(sg,lg,p,risk)
        row.append(cv.iloc[-1]/(5000+sum(x for _,x in inj)))
    print("%-28s"%n,*["%8.2fx"%r for r in row])
