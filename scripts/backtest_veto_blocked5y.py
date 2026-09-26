"""거부권이 차단한 진입 vs 새로 생긴 진입, 같은 진입인데 결과가 달라진 거래 (최근 5년). docs/STRATEGY.md 4.6.1.

실험 스크립트(2026-09-25). src/ 는 건드리지 않는다. 프로젝트 루트에서 실행:
    ./.venv/bin/python scripts/backtest_veto_blocked5y.py
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
import numpy as np
sg=sig[sig.index>=sig.index[-1]-pd.DateOffset(years=5)]; L5={k:v.loc[sg.index] for k,v in L.items()}
def r(fn):
    strategy.lt_veto=fn; return backtest.run_backtest(sg,L5,p,risk)[1]
tn=r(lambda a,b:False); tb=r(orig)
K=lambda x:(str(x.entry_date)[:10],x.side)
kb={K(x) for x in tb}; kn={K(x) for x in tn}
pr=lambda a:round(float(np.prod([1+x.ret_pct/100 for x in a])),3)
print("none",len(tn),pr(tn),"veto",len(tb),pr(tb))
print("none에만",[ (K(x),round(x.ret_pct,1)) for x in tn if K(x) not in kb], pr([x for x in tn if K(x) not in kb]))
print("veto에만",[ (K(x),round(x.ret_pct,1)) for x in tb if K(x) not in kn], pr([x for x in tb if K(x) not in kn]))
for x in tn:
    for y in tb:
        if K(x)==K(y) and abs(x.ret_pct-y.ret_pct)>0.05:
            print("같은진입 다른결과",K(x),"none",round(x.ret_pct,1),str(x.exit_date)[:10],x.exit_reason,"| veto",round(y.ret_pct,1),str(y.exit_date)[:10],y.exit_reason)
