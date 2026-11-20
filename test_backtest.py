import unittest
import numpy as np
import pandas as pd
from backtest import prepare, backtest, supertrend

class StrategyTests(unittest.TestCase):
    def candles(self, n=180):
        ix=pd.date_range('2026-11-19 09:15',periods=n,freq='5min',tz='Asia/Kolkata')
        ix=ix[ix.strftime('%H:%M')<='15:25']
        price=100+np.linspace(0,4,len(ix))
        return pd.DataFrame({'open':price-0.03,'high':price+0.15,'low':price-0.15,'close':price,'volume':1000.},index=ix)

    def test_indicator_shapes(self):
        d=self.candles()
        x=prepare(d)
        self.assertEqual(len(x),len(d))
        self.assertTrue({'ce','pe','st7_dir','vwap'}.issubset(x.columns))
        self.assertFalse(x.ce.iloc[:20].any())

    def test_exit_1510_when_no_reversal(self):
        d=self.candles()
        # Force a single valid CE event after 09:30, and hold ST positive.
        import backtest as mod
        original=mod.prepare
        def fake(df):
            z=df.copy(); z['ce']=False; z['pe']=False
            z.loc[z.index[5],'ce']=True
            z['st7_dir']=1
            return z
        mod.prepare=fake
        try:
            t=backtest(d,'TEST',{'per_trade_capital':25000,'round_trip_cost_bps':12})
            self.assertEqual(len(t),1)
            self.assertEqual(t[0]['exit_reason'],'EOD_1510')
            self.assertEqual(t[0]['exit_time'][11:16],'15:10')
            self.assertEqual(t[0]['entry_time'][11:16],'09:45')
        finally:
            mod.prepare=original

    def test_st_reversal_at_next_open(self):
        d=self.candles()
        import backtest as mod
        original=mod.prepare
        def fake(df):
            z=df.copy(); z['ce']=False; z['pe']=False
            z.loc[z.index[5],'pe']=True
            z['st7_dir']=-1
            z.loc[z.index[8]:,'st7_dir']=1
            return z
        mod.prepare=fake
        try:
            t=backtest(d,'TEST',{'per_trade_capital':25000,'round_trip_cost_bps':12})
            self.assertEqual(t[0]['exit_reason'],'ST7_2_REVERSAL')
            self.assertEqual(t[0]['exit_time'],str(d.index[9]))
        finally:
            mod.prepare=original

if __name__=='__main__': unittest.main()
