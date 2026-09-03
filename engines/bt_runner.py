"""backtrader 엔진 — 이벤트 기반. 라이브 체결(토스 API)과 모델이 가장 유사.

전략 신호(entry/exit)를 PandasData 추가 라인으로 주입 → 범용 SignalStrategy 가 소비.
신호 로직은 strategies/ 에서 한 번만 정의 (엔진 간 단일 소스).
"""
import os
import backtrader as bt
import pandas as pd

from . import metrics


class SignalData(bt.feeds.PandasData):
    lines = ("entry", "exit_sig")
    params = (
        ("datetime", None),
        ("open", "Open"), ("high", "High"), ("low", "Low"),
        ("close", "Close"), ("volume", "Volume"),
        ("openinterest", -1),
        ("entry", "entry"), ("exit_sig", "exit_sig"),
    )


class SignalStrategy(bt.Strategy):
    params = (("alloc", 0.95),)

    def __init__(self):
        self.entries = 0

    def next(self):
        if not self.position:
            if self.data.entry[0] > 0.5:
                price = self.data.close[0]
                size = int((self.broker.getvalue() * self.p.alloc) / price)
                if size > 0:
                    self.buy(size=size)
                    self.entries += 1
        else:
            if self.data.exit_sig[0] > 0.5:
                self.close()


def run(df, signals, cash=10000.0, fee=0.0005, label="strategy", outdir="results", plot=True):
    feed_df = df.copy()
    feed_df["entry"] = signals["entry"].astype(float).values
    feed_df["exit_sig"] = signals["exit"].astype(float).values

    cerebro = bt.Cerebro()
    cerebro.adddata(SignalData(dataname=feed_df))
    cerebro.addstrategy(SignalStrategy)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=fee)
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="tr", timeframe=bt.TimeFrame.Days)

    strat = cerebro.run()[0]

    tr = strat.analyzers.tr.get_analysis()
    strat_ret = pd.Series(tr).sort_index()
    equity = cash * (1.0 + strat_ret).cumprod()
    n_trades = strat.entries

    m = metrics.compute(equity, strat_ret, n_trades)

    if plot:
        os.makedirs(outdir, exist_ok=True)
        from ._plot import plt
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(equity.index, equity.values, label=f"{label} (backtrader)", lw=1.5)
        ax.set_title(f"{label} — backtrader engine")
        ax.set_ylabel("Equity")
        ax.legend()
        ax.grid(alpha=0.3)
        path = os.path.join(outdir, f"{label}_backtrader.png")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        m["plot"] = path

    return m
