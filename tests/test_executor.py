"""executor 안전장치 테스트 — 실제 주문을 내지 않고 순수 로직만 검증한다.

실행: ./.venv/bin/python tests/test_executor.py

증권사 호출은 전부 스텁으로 대체한다. 실제 API 연동은 이 테스트로 검증되지 않으므로,
첫 실거래는 반드시 `alerter.py --execute` 드라이런부터 시작할 것.
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import toss_api, executor
from toss_api import dec_str

ok=fail=0
def check(name, cond, extra=""):
    global ok,fail
    if cond: ok+=1; print(f"  PASS {name}")
    else: fail+=1; print(f"  FAIL {name} {extra}")

print("== dec_str (주문 문자열 형식) ==")
check("정수 내림", dec_str(3.999,0)=="3", dec_str(3.999,0))
check("금액 2자리 내림", dec_str(4999.999,2)=="4999.99", dec_str(4999.999,2))
check("소수 6자리", dec_str(141.5234567,6)=="141.523456", dec_str(141.5234567,6))
check("지수표기 없음", "e" not in dec_str(1e-3,6).lower(), dec_str(1e-3,6))
try: dec_str(0,2); check("0 거부", False)
except ValueError: check("0 거부", True)
try: dec_str(-5,2); check("음수 거부", False)
except ValueError: check("음수 거부", True)

print("\n== build_plans ==")
day=dt.datetime(2026,9,22)
d_open=dict(action="OPEN",ticker="SOXL",date=day,reason="상승 레짐",regime="상승",z=-0.2,close=500.0)
plans=executor.build_plans(d_open,None,5000.0)
p=plans[0]
check("OPEN 1건", len(plans)==1)
check("OPEN 금액주문", p.side=="BUY" and p.amount_usd==5000.0 and p.quantity is None)
check("멱등키 형식", p.client_order_id=="soxx-20260922-OPEN-SOXL", p.client_order_id)
check("멱등키 36자 이하", len(p.client_order_id)<=36)
check("사이징된 금액을 그대로 쓴다", executor.build_plans(d_open,None,2500.0)[0].amount_usd==2500.0)
d_close=dict(action="CLOSE",ticker="SOXS",date=day,reason="레짐 이탈",exit_reason="regime_change",
             regime="횡보",z=0.1,close=500.0)
pc=executor.build_plans(d_close,{"ticker":"SOXS","shares":141.52},5000.0)[0]
check("CLOSE 수량주문", pc.side=="SELL" and pc.quantity==141.52 and pc.amount_usd is None)
check("HOLD은 계획 없음", executor.build_plans(dict(action="HOLD",date=day),None,5000.0)==[])
check("NONE은 계획 없음", executor.build_plans(dict(action="NONE",date=day),None,5000.0)==[])

print("\n== 회귀 방지: 자산 0 함정과 사이징 우회 ==")
# 1) equity_after 가 문자열 "0.0" 이면 참이라 자산 0으로 읽혔던 버그
import journal as _jj
_realr=_jj.read_all
try:
    _jj.read_all=lambda: [
        {"timestamp":"2026-09-22 08:00","action":"OPEN","equity_after":"5000.0","note":""},
        {"timestamp":"2026-09-22 18:45","action":"SMOKE","equity_after":"0.0","note":"smoke"}]
    check("SMOKE(자산 0) 기록을 건너뛴다", _jj.current_equity(1234.0)==5000.0, _jj.current_equity(1234.0))
    _jj.read_all=lambda: []
    check("기록 없으면 default", _jj.current_equity(1234.0)==1234.0)
    _jj.read_all=lambda: [{"timestamp":"x","action":"OPEN","equity_after":"","note":""}]
    check("빈 값도 건너뛴다", _jj.current_equity(1234.0)==1234.0)
finally:
    _jj.read_all=_realr
# 2) build_plans 는 자본 하한이 적용된 금액을 그대로 써야 한다
from risk_manager import position_size_usd as _psz, RiskConfig as _RC
_amt=_psz(0.0,_RC())            # 자산 0 → 하한 5000
check("자산 0이어도 하한 금액", _amt==5000.0, _amt)
check("build_plans가 그 금액을 씀", executor.build_plans(d_open,None,_amt)[0].amount_usd==5000.0)

print("\n== SWITCH (같은 장 매도 → 매수) ==")
d_sw=dict(action="SWITCH",ticker="SOXL",close_ticker="SOXS",date=day,reason="교체",
          exit_reason="regime_change",regime="상승",z=-0.3,close=500.0)
sw=executor.build_plans(d_sw,{"ticker":"SOXS","shares":141.52},5000.0)
check("2건 생성", len(sw)==2, [x.describe() for x in sw])
check("매도가 먼저", sw[0].side=="SELL" and sw[0].symbol=="SOXS")
check("매수가 나중", sw[1].side=="BUY" and sw[1].symbol=="SOXL")
check("멱등키 서로 다름", sw[0].client_order_id!=sw[1].client_order_id)
check("보유 없으면 매수만", len(executor.build_plans(d_sw,None,5000.0))==1)

print("\n== preflight 안전장치 (스텁 클라이언트) ==")
class Stub:
    def __init__(self,**kw): self.__dict__.update(kw)
    def us_regular_session(self):
        now=dt.datetime.now(dt.timezone.utc)
        if self.session=="open":
            return ((now-dt.timedelta(hours=1)).isoformat(), (now+dt.timedelta(hours=3)).isoformat())
        if self.session=="closing":  # 종료 30분 전 → 금액주문 불가 구간
            return ((now-dt.timedelta(hours=6)).isoformat(), (now+dt.timedelta(minutes=30)).isoformat())
        if self.session=="holiday": return None
        if self.session=="unparsable": return ("아침","저녁")
        raise AssertionError
    def held_quantity(self,sym): return self.held.get(sym,0.0)
    def buying_power_usd(self): return self.power
    def sellable_quantity(self,sym): return self.sellable

base=dict(session="open",held={},power=10000.0,sellable=141.52)
b=executor.preflight(Stub(**base),p,executor.Guards(),None)
check("정상 OPEN 통과", b==[], b)
b=executor.preflight(Stub(**{**base,"session":"holiday"}),p,executor.Guards(),None)
check("휴장 차단", any("정규장이 열리지" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"session":"closing"}),p,executor.Guards(),None)
check("개장 후 상한 초과 차단", any("상한 120분" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"session":"unparsable"}),p,executor.Guards(),None)
check("시각 해석 실패 차단", any("해석할 수 없습니다" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"session":"unparsable"}),p,executor.Guards(),None,ignore_session=True)
check("--ignore-session 우회", b==[], b)
b=executor.preflight(Stub(**{**base,"held":{"SOXS":50.0}}),p,executor.Guards(),None)
check("이미 보유 중이면 매수 차단", any("보유 중입니다" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"power":100.0}),p,executor.Guards(),None)
check("매수가능 금액 부족 차단", any("매수 가능" in x for x in b), b)
b=executor.preflight(Stub(**base),p,executor.Guards(max_order_usd=1000.0),None)
check("주문 상한 초과 차단", any("상한" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"sellable":0.0}),pc,executor.Guards(),{"ticker":"SOXS","shares":141.52})
check("판매가능 0이면 매도 차단", any("판매 가능 수량이 0" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"sellable":100.0}),pc,executor.Guards(),{"ticker":"SOXS","shares":141.52})
check("수량 불일치 차단", any("불일치" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"sellable":141.0}),pc,executor.Guards(),{"ticker":"SOXS","shares":141.52})
check("2% 이내 차이는 통과", b==[], b)

print("\n== SWITCH 매수 다리 안전장치 ==")
# 직전에 SOXS를 팔았는데 잔고에 아직 남아 보이는 상황 → sold 로 예외 처리해야 통과
b=executor.preflight(Stub(**{**base,"held":{"SOXS":141.52}}),sw[1],executor.Guards(),None)
check("sold 없으면 차단", any("보유 중입니다" in x for x in b), b)
b=executor.preflight(Stub(**{**base,"held":{"SOXS":141.52}}),sw[1],executor.Guards(),None,
                     sold=frozenset({"SOXS"}))
check("sold 지정하면 통과", b==[], b)
b=executor.preflight(Stub(**{**base,"held":{"SOXL":10.0}}),sw[1],executor.Guards(),None,
                     sold=frozenset({"SOXS"}))
check("다른 종목 보유는 여전히 차단", any("보유 중입니다" in x for x in b), b)

print("\n== fund_buy_plan (매도 대금 반영 확인) ==")
funded,why=executor.fund_buy_plan(Stub(**{**base,"power":3000.0}),sw[1],executor.Guards())
check("가능 금액으로 축소", why is None and funded.amount_usd==3000.0, (funded.amount_usd,why))
funded,why=executor.fund_buy_plan(Stub(**{**base,"power":1.0}),sw[1],executor.Guards())
check("미반영이면 사유 반환", why is not None and "T+1" in why, why)
funded,why=executor.fund_buy_plan(Stub(**{**base,"power":99999.0}),sw[1],
                                  executor.Guards(max_order_usd=1500.0))
check("상한도 함께 적용", funded.amount_usd==1500.0, funded.amount_usd)

print("\n== 킬 스위치 ==")
executor.HALT_FILE.write_text("test")
try:
    b=executor.preflight(Stub(**base),p,executor.Guards(),None)
    check("HALT 파일이면 차단", any("킬 스위치" in x for x in b), b)
finally:
    executor.HALT_FILE.unlink()
b=executor.preflight(Stub(**base),p,executor.Guards(),None)
check("HALT 제거 후 통과", b==[], b)

print("\n== filled_summary ==")
q,px,c=executor.filled_summary({"execution":{"filledQuantity":"141.523456","averageFilledPrice":"35.3312","commission":"0.5","tax":"0.1"}})
check("체결 파싱", abs(q-141.523456)<1e-9 and abs(px-35.3312)<1e-9 and abs(c-0.6)<1e-9, (q,px,c))
q,px,c=executor.filled_summary({"execution":{"filledQuantity":"10","averageFilledPrice":None,"filledAmount":"1000"}})
check("평균가 없으면 금액/수량", px==100.0, px)
check("미체결은 0", executor.filled_summary({})==(0.0,0.0,0.0))

print("\n== execute 드라이런은 주문을 만들지 않는다 ==")
class Boom:
    def create_order(self,**kw): raise AssertionError("드라이런에서 주문이 나갔다!")
    def sellable_quantity(self,s): return 1.0
r=executor.execute(Boom(),p,live=False)
check("드라이런 무주문", r.get("dryRun") is True, r)

print("\n== 자본 하한 규칙 (risk_manager) ==")
from risk_manager import RiskConfig as RC, position_size_usd, topup_needed
_c=RC()
check("하한 위면 자산 전체", position_size_usd(12000,_c)==12000)
check("하한이면 그대로", position_size_usd(5000,_c)==5000)
check("하한 아래면 채워서 매수", position_size_usd(1200,_c)==5000, position_size_usd(1200,_c))
check("추가입금액 계산", topup_needed(1200,_c)==3800, topup_needed(1200,_c))
check("하한 위면 입금 0", topup_needed(9000,_c)==0)
check("노출 반영", position_size_usd(1200,RC(exposure=0.5))==2500)
check("하한 0이면 순수 복리", position_size_usd(1200,RC(min_capital_usd=0.0))==1200)
check("하한 0이면 입금 없음", topup_needed(1200,RC(min_capital_usd=0.0))==0)
try: RC(min_capital_usd=-1); check("음수 하한 거부", False)
except ValueError: check("음수 하한 거부", True)

print("\n== 체결 시각 정책: 개장을 겨냥한다 ==")
# 1시간봉 35건 실측 근거: 09:30 30.9배 / 10:30 30.2배 / 11:30 27.3배 / 12:30 16.2배
class SessStub(Stub):
    def us_regular_session(self):
        now=dt.datetime.now(dt.timezone.utc)
        st=now-dt.timedelta(minutes=self.elapsed)
        return (st.isoformat(), (st+dt.timedelta(hours=6,minutes=30)).isoformat())
def at(elapsed, guards=None):
    w=[]
    blocks=executor.preflight(SessStub(**{**base,"elapsed":elapsed}),p,
                              guards or executor.Guards(),None,warn_out=w)
    return blocks,w
b,w=at(-10); check("개장 전이면 차단", any("개장 전" in x for x in b), b)
b,w=at(5);   check("개장 +5분 통과, 경고 없음", b==[] and w==[], (b,w))
b,w=at(25);  check("개장 +25분 통과, 경고 없음", b==[] and w==[], (b,w))
b,w=at(45);  check("개장 +45분 통과하되 경고", b==[] and any("목표" in x for x in w), (b,w))
b,w=at(115); check("개장 +115분 통과하되 경고", b==[] and w!=[], (b,w))
b,w=at(125); check("개장 +125분 차단", any("상한 120분" in x for x in b), b)
b,w=at(300); check("개장 +300분 차단", any("상한" in x for x in b), b)
b,w=at(125,executor.Guards(max_late_min=180))
check("상한을 늘리면 +125분도 통과", b==[], b)

print("\n== 멱등성: 재실행 시 중복 주문 차단 ==")
import journal as _j
_real=_j.read_all
_today=dt.datetime.now().strftime("%Y-%m-%d")
def _fake(rows):
    _j.read_all=lambda: rows
try:
    _fake([])
    check("기록 없으면 통과", not executor.already_done("soxx-20260922-OPEN-SOXL"))
    _fake([{"timestamp":f"{_today} 22:35:00","action":"OPEN","ticker":"SOXL",
            "note":"executor live FILLED soxx-20260922-OPEN-SOXL 상승 레짐 추세 진입"}])
    check("같은 멱등키면 차단", executor.already_done("soxx-20260922-OPEN-SOXL"))
    check("다른 날짜 키는 통과", not executor.already_done("soxx-20260923-OPEN-SOXL"))
    check("다른 액션 키는 통과", not executor.already_done("soxx-20260922-CLOSE-SOXL"))
    # 미국 장은 KST 자정을 넘는다 — 날짜가 바뀌어도 같은 키면 막혀야 한다
    _tmr=(dt.datetime.now()+dt.timedelta(days=1)).strftime("%Y-%m-%d")
    _fake([{"timestamp":f"{_today} 23:50:00","action":"OPEN","ticker":"SOXL",
            "note":"executor live FILLED soxx-20260922-OPEN-SOXL"}])
    check("KST 자정 넘어도 같은 키 차단", executor.already_done("soxx-20260922-OPEN-SOXL"))
    # preflight 에서도 막히는지
    _blk=executor.preflight(Stub(**base),p,executor.Guards(),None)
    check("preflight가 멱등키로 차단", any("멱등키" in x for x in _blk), _blk)
    _fake([])
    check("기록 지우면 다시 통과", executor.preflight(Stub(**base),p,executor.Guards(),None)==[])
finally:
    _j.read_all=_real

print("\n== 부분 봉 가드 (data_fetcher.drop_incomplete_bar) ==")
import pandas as pd
from data_fetcher import drop_incomplete_bar
_idx=pd.to_datetime(["2026-09-18","2026-09-21","2026-09-22"])
_df=pd.DataFrame({"Close":[1,2,3]},index=_idx)
def _last(t):
    return drop_incomplete_bar(_df,pd.Timestamp(t,tz="America/New_York")).index[-1].date()
check("장중(09:35 ET)은 당일 봉 제외", str(_last("2026-09-22 09:35"))=="2026-09-21", _last("2026-09-22 09:35"))
check("주문마감(14:59 ET)도 제외",    str(_last("2026-09-22 14:59"))=="2026-09-21", _last("2026-09-22 14:59"))
check("개장전(06:00 ET)도 제외",      str(_last("2026-09-22 06:00"))=="2026-09-21", _last("2026-09-22 06:00"))
check("마감후(16:10 ET)는 당일 봉 사용", str(_last("2026-09-22 16:10"))=="2026-09-22", _last("2026-09-22 16:10"))
check("과거 데이터는 손대지 않음",
      str(drop_incomplete_bar(_df,pd.Timestamp("2026-09-25 10:00",tz="America/New_York")).index[-1].date())=="2026-09-22")
check("빈 프레임도 안전", len(drop_incomplete_bar(_df.iloc[:0],pd.Timestamp("2026-09-22 10:00",tz="America/New_York")))==0)

print(f"\n결과: {ok} PASS / {fail} FAIL")
sys.exit(1 if fail else 0)
