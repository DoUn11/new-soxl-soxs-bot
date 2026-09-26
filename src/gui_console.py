"""⚠️ **지금은 쓰지 않는다 (2026-09-23).** 이 맥에서는 창이 비어서 뜬다.

이유: 이 맥(macOS 26.5.2)에 있는 파이썬은 CommandLineTools 3.9 하나뿐이고 **Tk가 8.5**다.
2010년판 deprecated Aqua Tk라 최신 macOS에서 **창은 뜨지만 Tk가 직접 그리는 위젯이
하나도 렌더링되지 않는다** — 네이티브 위젯(tk.Button)만 보인다. 위젯 배치·색은 멀쩡해서
(측정하면 전부 mapped, 대비도 정상) 원인을 찾기 어려웠다.

그래서 콘솔은 **크롬 앱 창**으로 간다: `scripts/console_launch.sh` (= SOXL-SOXS 봇 스테이터스.app).
이 파일은 지우지 않고 남겨 뒀다 — 나중에 Tk 8.6 이상인 파이썬(brew python@3.13 +
python-tk 등)을 깔면 그대로 살아난다. 다크모드 팔레트까지 맞춰 둔 상태다.

아래는 원래 설명이다.

레짐 콘솔 — 맥 전용 데스크톱 창. 웹 서버·폰 접속이 필요 없을 때 쓴다. (2026-09-23)

`server.py`(웹 버전)는 폰에서 볼 필요가 있을 때 만들었다. 이제 폰 접속이 필요 없다는
사용자 요청으로, **같은 상태 계산(`server.build_state`)을 그대로 재사용해** tkinter 창으로
띄운다. HTTP 서버도, 접속 토큰도, 와이파이 공유도 필요 없다 — 이 맥에서만 뜨는 로컬
프로세스 하나뿐이다. `build_state()`를 다시 구현하지 않는 이유는 CLAUDE.md의 "판단 로직을
복제하지 마라" 원칙과 같다 — 상태 계산도 한 곳(server.py)에만 두고, GUI는 그 결과를
그리기만 한다.

**읽기 전용, 주문 기능 없음** — server.py와 같은 원칙. 표준 라이브러리(tkinter)만 쓰고
공용 venv에 새 의존성을 넣지 않았다.

15초마다 백그라운드 스레드에서 `build_state()`를 다시 부른다(내부 TTL 캐시가 실제
네트워크 호출은 알아서 줄인다). 창이 얼지 않도록 네트워크 호출은 항상 백그라운드
스레드에서 하고, 결과만 큐를 통해 메인 스레드(tkinter)로 넘겨 그린다.

실행:
    ./scripts/gui.sh
    ./.venv/bin/python src/gui_console.py
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from server import build_state  # noqa: E402 — 웹 버전과 완전히 같은 상태 계산을 재사용한다

REFRESH_SEC = 15

# ⚠️ **다크모드를 반드시 따라가야 한다.** macOS의 Tk 8.5(Aqua)는 다크모드에서 tk.Label /
# tk.Frame 의 `bg` 를 무시하고 시스템 어두운 배경으로 그리면서 `fg` 는 그대로 적용한다.
# 그래서 라이트모드용 검은 글씨를 쓰면 **어두운 배경에 검은 글씨가 그려져 아무것도 안
# 보인다.** 창은 떴는데 네이티브 위젯(tk.Button)만 보이는 증상이 정확히 이것이다.
# 2026-09-23에 실제로 겪었고, 위젯 배치는 멀쩡한데(측정해 보면 전부 mapped) 색만
# 안 보이는 것이라 원인을 찾기 어려웠다.
LIGHT = dict(
    bg="#f7f7f5", card="#ffffff", ink="#1c1c1a", ink2="#6b6b64", line="#e7e6e1",
    up="#0f766e", up_soft="#dcf2ef", side="#a15c07", side_soft="#fbeed7",
    down="#9f1239", down_soft="#fbe3e9", accent="#3730a3", accent_soft="#e8e7fb",
)
DARK = dict(   # dashboard/app.html 의 다크 팔레트와 같은 값을 쓴다
    bg="#17171a", card="#202024", ink="#eceae3", ink2="#a7a59d", line="#34333a",
    up="#5eead4", up_soft="#103833", side="#fbbf60", side_soft="#3a2a0c",
    down="#fda4af", down_soft="#3d1420", accent="#a5b4fc", accent_soft="#26254a",
)
C = LIGHT              # use_palette() 가 시작할 때 채운다
KIND_COLORS: dict = {}
REGIME_KIND = {"상승": "up", "횡보": "side", "하락": "down"}


def system_is_dark() -> bool:
    """macOS가 다크모드인가. 확인할 수 없으면 라이트로 본다."""
    try:
        import subprocess
        r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, text=True, timeout=3)
        return "dark" in (r.stdout or "").strip().lower()
    except Exception:
        return False


def use_palette(dark: bool) -> None:
    """팔레트를 고정한다. 위젯을 만들기 **전에** 불러야 한다."""
    global C, KIND_COLORS, BG, CARD, INK, INK2, LINE
    C = DARK if dark else LIGHT
    BG, CARD, INK, INK2, LINE = C["bg"], C["card"], C["ink"], C["ink2"], C["line"]
    KIND_COLORS = {
        "up": (C["up"], C["up_soft"]), "side": (C["side"], C["side_soft"]),
        "down": (C["down"], C["down_soft"]), "accent": (C["accent"], C["accent_soft"]),
        "flat": (C["ink2"], C["line"]),
    }


use_palette(system_is_dark())


def regime_kind(label: str | None) -> str:
    return REGIME_KIND.get(label or "", "flat")


def fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


class Pill(tk.Label):
    """레짐 색상 배지. 색은 kind(up/side/down/accent/flat)로만 정한다."""

    def __init__(self, master, **kw):
        super().__init__(master, font=("Helvetica", 12, "bold"), padx=10, pady=2, bd=0, **kw)
        self.set("—", "flat")

    def set(self, text: str | None, kind: str) -> None:
        fg, bg = KIND_COLORS.get(kind, KIND_COLORS["flat"])
        self.config(text=text or "—", fg=fg, bg=bg)


class Card(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=CARD, highlightbackground=LINE, highlightthickness=1,
                         padx=14, pady=12)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.last_state: dict | None = None
        self._build_ui()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(300, self._drain_queue)

    # ---------------------------------------------------------------- UI 골격
    def _build_ui(self) -> None:
        r = self.root
        r.title("레짐 콘솔")
        r.configure(bg=BG)
        r.geometry("420x760")
        r.minsize(380, 600)

        # ttk 스타일은 이 스타일을 쓰는 위젯보다 **먼저** 정의해야 한다.
        style = ttk.Style()
        try:
            style.theme_use("clam")      # aqua 기본 테마는 색 지정을 대부분 무시한다
        except tk.TclError:
            pass
        style.configure("Console.Treeview", background=CARD, fieldbackground=CARD,
                        foreground=INK, borderwidth=0, rowheight=22)
        style.configure("Console.Treeview.Heading", background=C["line"], foreground=INK2,
                        borderwidth=0)
        style.map("Console.Treeview", background=[("selected", C["accent_soft"])],
                  foreground=[("selected", C["accent"])])
        style.configure("Console.TSeparator", background=C["line"])

        outer = tk.Frame(r, bg=BG)
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="레짐 콘솔", font=("Helvetica", 16, "bold"),
                 bg=BG, fg=INK).pack(anchor="w")
        self.clock_lbl = tk.Label(header, text="불러오는 중…", font=("Helvetica", 11),
                                  bg=BG, fg=INK2)
        self.clock_lbl.pack(anchor="w")

        self.halt_lbl = tk.Label(outer, text="⛔ 킬 스위치 켜짐 — 어떤 주문도 나가지 않습니다",
                                 font=("Helvetica", 11, "bold"),
                                 bg=C["down_soft"], fg=C["down"], padx=10, pady=6)
        # halt_lbl은 필요할 때만 pack한다 (기본 숨김)

        # --- 레짐 카드 --------------------------------------------------
        c1 = Card(outer); c1.pack(fill="x", pady=(10, 0))
        row = tk.Frame(c1, bg=CARD); row.pack(fill="x")
        tk.Label(row, text="SOXX", font=("Helvetica", 11), bg=CARD, fg=INK2).pack(side="left")
        self.market_pill = Pill(c1); self.market_pill.pack(in_=row, side="right")
        self.price_lbl = tk.Label(c1, text="—", font=("Helvetica", 24, "bold"), bg=CARD, fg=INK)
        self.price_lbl.pack(anchor="w", pady=(2, 0))
        self.market_detail_lbl = tk.Label(c1, text="", font=("Helvetica", 11), bg=CARD, fg=INK2)
        self.market_detail_lbl.pack(anchor="w")
        ttk.Separator(c1, style="Console.TSeparator").pack(fill="x", pady=8)

        def kv_row(parent, label):
            row = tk.Frame(parent, bg=CARD); row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=("Helvetica", 11), bg=CARD, fg=INK2).pack(side="left")
            return row

        row = kv_row(c1, "매매 레짐 (확정)")
        self.regime_pill = Pill(c1); self.regime_pill.pack(in_=row, side="right")
        self.streak_lbl = tk.Label(row, text="", font=("Helvetica", 10), bg=CARD, fg=INK2)
        self.streak_lbl.pack(side="right", padx=(0, 6))

        self.live_row = tk.Frame(c1, bg=CARD)
        tk.Label(self.live_row, text="지금 종가라면", font=("Helvetica", 11),
                bg=CARD, fg=INK2).pack(side="left")
        self.live_pill = Pill(self.live_row); self.live_pill.pack(side="right")
        # live_row도 조건부로만 보인다

        row = kv_row(c1, "z-score")
        self.z_lbl = tk.Label(row, text="—", font=("Helvetica", 11), bg=CARD, fg=INK2)
        self.z_lbl.pack(side="right")

        row = kv_row(c1, "장기 추세(120일)")
        self.lt_pill = Pill(c1); self.lt_pill.pack(in_=row, side="right")
        self.lt_chg_lbl = tk.Label(row, text="", font=("Helvetica", 10), bg=CARD, fg=INK2)
        self.lt_chg_lbl.pack(side="right", padx=(0, 6))

        row = kv_row(c1, "장세 참고 (20일)")
        self.view_pill = Pill(c1); self.view_pill.pack(in_=row, side="right")
        tk.Label(row, text="매매엔 안 씀", font=("Helvetica", 9), bg=CARD, fg=INK2
                ).pack(side="right", padx=(0, 6))

        # --- 포지션 카드 --------------------------------------------------
        self.pos_card = Card(outer)
        row = tk.Frame(self.pos_card, bg=CARD); row.pack(fill="x")
        tk.Label(row, text="보유", font=("Helvetica", 11), bg=CARD, fg=INK2).pack(side="left")
        self.pos_ticker_pill = Pill(self.pos_card); self.pos_ticker_pill.pack(in_=row, side="right")
        row2 = tk.Frame(self.pos_card, bg=CARD); row2.pack(fill="x", pady=(4, 0))
        self.pos_pnl_pct_lbl = tk.Label(row2, text="—", font=("Helvetica", 22, "bold"), bg=CARD)
        self.pos_pnl_pct_lbl.pack(side="left")
        self.pos_pnl_lbl = tk.Label(row2, text="", font=("Helvetica", 11), bg=CARD, fg=INK2)
        self.pos_pnl_lbl.pack(side="left", padx=(8, 0))
        self.pos_detail_lbl = tk.Label(self.pos_card, text="", font=("Helvetica", 10),
                                       bg=CARD, fg=INK2, wraplength=380, justify="left")
        self.pos_detail_lbl.pack(anchor="w", pady=(4, 0))
        ttk.Separator(self.pos_card, style="Console.TSeparator").pack(fill="x", pady=8)
        self.gaps_frame = tk.Frame(self.pos_card, bg=CARD)
        self.gaps_frame.pack(fill="x")

        self.nopos_card = Card(outer)
        tk.Label(self.nopos_card, text="보유 포지션 없음", font=("Helvetica", 11),
                bg=CARD, fg=INK2).pack(anchor="w")

        # --- 지시 카드 --------------------------------------------------
        c3 = Card(outer); c3.pack(fill="x", pady=(10, 0))
        row = tk.Frame(c3, bg=CARD); row.pack(fill="x")
        tk.Label(row, text="오늘의 지시", font=("Helvetica", 11), bg=CARD, fg=INK2).pack(side="left")
        self.action_pill = Pill(c3); self.action_pill.pack(in_=row, side="right")
        self.reason_lbl = tk.Label(c3, text="", font=("Helvetica", 10), bg=CARD, fg=INK2,
                                   wraplength=380, justify="left")
        self.reason_lbl.pack(anchor="w", pady=(6, 0))
        self.amount_lbl = tk.Label(c3, text="", font=("Helvetica", 10), bg=CARD, fg=INK2)
        self.amount_lbl.pack(anchor="w", pady=(2, 0))

        # --- 최근 주문 --------------------------------------------------
        c4 = Card(outer); c4.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(c4, text="최근 주문 (봇 실행분)", font=("Helvetica", 11), bg=CARD, fg=INK2
                ).pack(anchor="w", pady=(0, 6))
        cols = ("time", "action", "ticker", "price")
        self.orders_tree = ttk.Treeview(c4, columns=cols, show="headings", height=6,
                                        style="Console.Treeview")
        for c, w, t in zip(cols, (110, 70, 60, 80), ("시각", "동작", "종목", "체결가")):
            self.orders_tree.heading(c, text=t)
            self.orders_tree.column(c, width=w, anchor="w")
        self.orders_tree.pack(fill="both", expand=True)

        footer = tk.Label(outer, text="읽기 전용 · 주문 기능 없음 · 로컬 전용 · 15초마다 자동 갱신",
                          font=("Helvetica", 9), bg=BG, fg=INK2)
        footer.pack(pady=(10, 0))

        btn = tk.Button(outer, text="지금 새로고침", command=self._force_refresh)
        btn.pack(pady=(6, 0))

    # ------------------------------------------------------------- 갱신 루프
    def _poll_loop(self) -> None:
        while True:
            try:
                state = build_state()
                self.q.put(("ok", state))
            except Exception as exc:
                self.q.put(("err", str(exc)))
            time.sleep(REFRESH_SEC)

    def _force_refresh(self) -> None:
        threading.Thread(target=self._refresh_once, daemon=True).start()

    def _refresh_once(self) -> None:
        try:
            self.q.put(("ok", build_state()))
        except Exception as exc:
            self.q.put(("err", str(exc)))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    self.last_state = payload
                    self._render(payload)
                else:
                    self.clock_lbl.config(text=f"갱신 실패 — 다시 시도 중… ({payload})")
        except queue.Empty:
            pass
        self.root.after(300, self._drain_queue)

    # ------------------------------------------------------------------ 렌더
    def _render(self, d: dict) -> None:
        self.clock_lbl.config(text=f"미국 {d['now'][11:16]} ET · 한국 {d['now_kst'][11:16]} · "
                                   f"{d['market']['state']} ({d['market']['detail']})")
        if d.get("halted"):
            self.halt_lbl.pack(fill="x", pady=(6, 0))
        else:
            self.halt_lbl.pack_forget()

        px = d["quotes"].get("SOXX")
        self.price_lbl.config(text=(f"${px:,.2f}" if px else "—"))
        self.market_pill.set(d["market"]["state"], "up" if d["market"]["open"] else "flat")
        self.market_detail_lbl.config(text=d["market"]["detail"])

        s = d["settled"]
        self.regime_pill.set(s["regime"], regime_kind(s["regime"]))
        self.streak_lbl.config(text=f"{s['date']} 종가 기준")
        self.z_lbl.config(text=f"{s['z']:+.2f}")

        live = d.get("live")
        if live and live["regime"] != s["regime"]:
            self.live_row.pack(fill="x", pady=2, before=self._first_kv_after_live())
            self.live_pill.set(f"{live['regime']} (잠정)", regime_kind(live["regime"]))
        else:
            self.live_row.pack_forget()

        self.lt_pill.set(s.get("lt_trend") or "판단 불가", regime_kind(s.get("lt_trend")))
        self.lt_chg_lbl.config(text=(f"{fmt_pct(s['lt_chg'])} (120일)" if s.get("lt_chg") is not None else ""))
        self.view_pill.set(s.get("view"), regime_kind(s.get("view")))

        pos = d.get("position")
        if pos:
            self.nopos_card.pack_forget()
            self.pos_card.pack(fill="x", pady=(10, 0))
            self.pos_ticker_pill.set(pos["ticker"], "accent")
            pct = pos["unrealized"]
            self.pos_pnl_pct_lbl.config(text=fmt_pct(pct),
                                        fg=(C["up"] if pct >= 0 else C["down"]))
            self.pos_pnl_lbl.config(text=(f"{'+' if pos['pnl'] and pos['pnl']>=0 else ''}"
                                          f"${pos['pnl']:,.0f}" if pos.get("pnl") is not None else ""))
            self.pos_detail_lbl.config(
                text=f"{pos['shares']}주 @ ${pos['entry']:.2f} · "
                     f"현재 {'$'+format(pos['price'],'.3f') if pos.get('price') else '—'} · "
                     f"{pos['opened'][:10]} 진입")
            for w in self.gaps_frame.winfo_children():
                w.destroy()
            gaps = d.get("gaps") or {}
            for g in gaps.values():
                row = tk.Frame(self.gaps_frame, bg=CARD); row.pack(fill="x", pady=2)
                tk.Label(row, text=g["label"], font=("Helvetica", 10), bg=CARD, fg=INK2
                        ).pack(side="left")
                txt, color = ("발동", C["down"]) if g["hit"] else (g["detail"], INK2)
                tk.Label(row, text=txt, font=("Helvetica", 10, "bold" if g["hit"] else "normal"),
                        bg=CARD, fg=color).pack(side="right")
        else:
            self.pos_card.pack_forget()
            self.nopos_card.pack(fill="x", pady=(10, 0))

        dec = d["decision"]
        kind = "accent" if dec["action"] == "HOLD" else ("flat" if dec["action"] == "NONE" else
               ("down" if dec["action"] == "CLOSE" else "up"))
        self.action_pill.set(dec["action"], kind)
        self.reason_lbl.config(text=dec.get("reason") or "")
        amt = dec.get("amount")
        self.amount_lbl.config(text=(f"예상 매수 금액 ${amt:,.0f}" if amt else ""))

        for row_id in self.orders_tree.get_children():
            self.orders_tree.delete(row_id)
        for o in (d.get("orders") or []):
            ts = (o.get("timestamp") or "")[5:16]
            fp = o.get("fill_price")
            price = f"${float(fp):.2f}" if fp not in (None, "", "0", "0.0") else "미체결"
            self.orders_tree.insert("", "end", values=(ts, o.get("action"), o.get("ticker"), price))

    def _first_kv_after_live(self):
        # live_row를 z-score 행 바로 위에 끼워 넣기 위한 기준 위젯.
        return self.z_lbl.master


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
