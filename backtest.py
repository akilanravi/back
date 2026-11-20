"""One 5-minute candle-close setup, mirrored CE/PE, exit ST(7,2) or 15:10 IST.
Research only: trades the underlying, never sends orders or models option premium.
"""
import argparse
import json
import os
import re
import time
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / 'reports'
REPORTS.mkdir(exist_ok=True)
TZ = 'Asia/Kolkata'
MASTER_URL = 'https://images.dhan.co/api-data/api-scrip-master.csv'
API_URL = 'https://api.dhan.co/v2/charts/intraday'


def ema(x, period):
    return x.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi14(close):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100/(1 + rs)
    return out.mask((loss == 0) & (gain > 0), 100).mask((loss == 0) & (gain == 0), 50)


def supertrend(df, period, multiplier):
    """Continuous standard final-band Supertrend; no resetting at midnight."""
    h, l, c = (df[k].to_numpy(dtype=float) for k in ('high', 'low', 'close'))
    prev = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h-l, np.abs(h-prev), np.abs(l-prev)])
    tr[0] = h[0]-l[0]
    atr = pd.Series(tr).ewm(alpha=1/period, adjust=False, min_periods=period).mean().to_numpy()
    n = len(df)
    direction = np.zeros(n, dtype=np.int8)
    line = np.full(n, np.nan)
    upper = lower = np.nan
    trend = 0
    for i in range(n):
        if not np.isfinite(atr[i]):
            continue
        mid = (h[i]+l[i])/2
        bu, bl = mid+multiplier*atr[i], mid-multiplier*atr[i]
        if not np.isfinite(upper):
            upper, lower, trend = bu, bl, (1 if c[i] >= mid else -1)
        else:
            old_upper, old_lower = upper, lower
            upper = bu if bu < old_upper or c[i-1] > old_upper else old_upper
            lower = bl if bl > old_lower or c[i-1] < old_lower else old_lower
            if trend == 1 and c[i] < old_lower:
                trend = -1
            elif trend == -1 and c[i] > old_upper:
                trend = 1
        direction[i] = trend
        line[i] = lower if trend == 1 else upper
    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


def prepare(df):
    df = df.copy().sort_index()
    for col in ('open','high','low','close','volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['open','high','low','close','volume'])
    c, v = df.close, df.volume
    session = pd.Series(df.index.date, index=df.index)
    typical = (df.high + df.low + c)/3
    vwap = (typical*v).groupby(session).cumsum()/v.groupby(session).cumsum().replace(0,np.nan)
    st10, trend10 = supertrend(df,10,2)
    st7, trend7 = supertrend(df,7,2)
    volume_ma = v.shift(1).rolling(20, min_periods=20).mean()  # prior 20 completed candles
    common = (v > 1.5*volume_ma) & volume_ma.gt(0)
    ce = ((c>vwap)&(ema(c,9)>ema(c,21))&(c>st10)&(rsi14(c)>55)&
          (c>df.high.shift(1))&common&(c>df.open)&(trend10==1))
    pe = ((c<vwap)&(ema(c,9)<ema(c,21))&(c<st10)&(rsi14(c)<45)&
          (c<df.low.shift(1))&common&(c<df.open)&(trend10==-1))
    df['ce'] = ce.fillna(False)
    df['pe'] = pe.fillna(False)
    df['st7_dir'] = trend7
    df['st10'] = st10
    df['vwap'] = vwap
    return df


def backtest(df, symbol, cfg):
    """Signals at bar close; entry/exit at next bar open. 15:10 is hard cutoff.
    No overnight positions. Exit on first confirmed ST7 reversal or 15:10 open.
    """
    d = prepare(df)
    rows=[]
    max_daily = int(cfg.get('max_trades_per_symbol_day', 99))
    qty_cap = float(cfg.get('per_trade_capital', 25000))
    charge_bps = float(cfg.get('round_trip_cost_bps', 12))
    for session_date, day in d.groupby(d.index.date, sort=True):
        # Treat timestamp as candle OPEN (Dhan 5m convention).
        day = day.between_time('09:15','15:25')
        if len(day)<5: continue
        ix=day.index; o=day.open.to_numpy(); cl=day.close.to_numpy()
        st=day.st7_dir.to_numpy(); ce=day.ce.to_numpy(); pe=day.pe.to_numpy()
        count=0; i=0
        while i < len(day)-1 and count<max_daily:
            # Signal candle must be completed AFTER 09:30 (09:30 candle closes 09:35).
            if ix[i].strftime('%H:%M') < cfg.get('first_signal_bar','09:30'):
                i+=1; continue
            next_time=ix[i+1].strftime('%H:%M')
            if next_time > cfg.get('last_entry_time','14:55') or next_time >= '15:10':
                i+=1; continue
            side = 1 if ce[i] else (-1 if pe[i] else 0)
            if not side:
                i+=1; continue
            j=i+1
            entry=float(o[j])
            if not np.isfinite(entry) or entry<=0:
                i+=1; continue
            qty=int(qty_cap//entry)
            if qty<1:
                i+=1; continue
            # Exit at 15:10 open (or first later available open; flag missing 15:10).
            cutoff = next((k for k in range(j+1,len(day)) if ix[k].strftime('%H:%M')>='15:10'), None)
            if cutoff is None:
                # Missing cutoff candle: last available close, explicitly flagged.
                exit_idx=len(day)-1; exit_price=float(cl[exit_idx]); reason='MISSING_1510_LAST_CLOSE'
            else:
                exit_idx=cutoff; exit_price=float(o[cutoff]); reason='EOD_1510' if ix[cutoff].strftime('%H:%M')=='15:10' else 'LATE_MISSING_1510'
            # Reversal determined by completed candle, executable next candle open.
            for k in range(j, (cutoff if cutoff is not None else len(day)-1)):
                if st[k] == -side and k+1<len(day):
                    exit_idx=k+1; exit_price=float(o[exit_idx]); reason='ST7_2_REVERSAL'; break
            gross=(exit_price-entry)*qty*side
            cost=(entry+exit_price)*qty*charge_bps/10000
            rows.append(dict(symbol=symbol,side='CE' if side==1 else 'PE',signal_time=str(ix[i]),
                entry_time=str(ix[j]),exit_time=str(ix[exit_idx]),entry=round(entry,4),
                exit=round(exit_price,4),qty=qty,gross=round(gross,2),
                estimated_cost=round(cost,2),net=round(gross-cost,2),exit_reason=reason))
            count+=1
            i=exit_idx+1  # No overlapping position in this symbol; no entry on exit bar.
    return rows


def master_universe(master, indices):
    required={'SEM_EXM_EXCH_ID','SEM_SEGMENT','SEM_SMST_SECURITY_ID','SEM_TRADING_SYMBOL','SEM_INSTRUMENT_NAME'}
    if not required.issubset(master.columns):
        raise ValueError('Dhan master columns changed: '+str(sorted(required-set(master.columns))))
    master=master.fillna('')
    nse=master[master.SEM_EXM_EXCH_ID.eq('NSE')]
    eq=nse[nse.SEM_SEGMENT.eq('E')].copy()
    eq['sym']=eq.SEM_TRADING_SYMBOL.str.strip().str.upper()
    eq=eq[eq.sym.ne('')].drop_duplicates('sym').set_index('sym')
    # Derive stock underlyings from FUTSTK contracts in CURRENT instrument master.
    derivatives=nse[nse.SEM_INSTRUMENT_NAME.str.upper().str.contains('FUTSTK',regex=False)]
    candidates=set()
    for _,r in derivatives.iterrows():
        fields=[r.get('SEM_CUSTOM_SYMBOL',''),r.get('SEM_TRADING_SYMBOL','')]
        for field in fields:
            s=str(field).strip().upper()
            for candidate in (s.split(' ')[0], s.split('-')[0], re.split(r'\d{1,2}[A-Z]{3}\d{2}',s)[0].rstrip('- ')):
                if candidate in eq.index:
                    candidates.add(candidate)
    if not candidates:
        raise RuntimeError('No F&O underlyings resolved from Dhan FUTSTK contracts. Check instrument master fields; do not silently use all NSE equities.')
    universe={s:dict(security_id=str(eq.loc[s,'SEM_SMST_SECURITY_ID']),exchange_segment='NSE_EQ',instrument='EQUITY') for s in sorted(candidates)}
    # Index data: match IDX records from Dhan master, never assume IDs.
    index_rows=master[master.SEM_SEGMENT.str.upper().str.contains('IDX|INDEX',regex=True) |
                      master.SEM_INSTRUMENT_NAME.str.upper().eq('INDEX')]
    alias={'NIFTY':'NIFTY 50','BANKNIFTY':'NIFTY BANK','FINNIFTY':'NIFTY FIN SERVICE',
           'MIDCPNIFTY':'NIFTY MID SELECT','NIFTYNXT50':'NIFTY NEXT 50','SENSEX':'SENSEX'}
    for name in indices:
        if name not in alias: continue
        possible=index_rows[index_rows.SEM_TRADING_SYMBOL.str.upper().str.strip().isin([name,alias[name]])]
        if possible.empty and 'SEM_CUSTOM_SYMBOL' in index_rows:
            possible=index_rows[index_rows.SEM_CUSTOM_SYMBOL.str.upper().str.strip().isin([name,alias[name]])]
        if possible.empty:
            print(f'WARNING index {name}: not found in Dhan master; skipped. Supply index_override.json if necessary.',flush=True)
            continue
        r=possible.iloc[0]
        universe[name]=dict(security_id=str(r.SEM_SMST_SECURITY_ID),exchange_segment='IDX_I',instrument='INDEX')
    override=ROOT/'index_override.json'
    if override.exists():
        for name,details in json.loads(override.read_text()).items():
            if name in indices and all(details.get(k) for k in ('security_id','exchange_segment','instrument')):
                universe[name]=details
    return universe


def fetch_candles(symbol, info, start, end, token, retries=4):
    pieces=[]
    start=pd.Timestamp(start); end=pd.Timestamp(end)+pd.Timedelta(days=1)
    session=requests.Session()
    for a in pd.date_range(start,end,freq='85D'):
        b=min(a+pd.Timedelta(days=85),end)
        if a>=b: continue
        payload=dict(securityId=str(info['security_id']),exchangeSegment=info['exchange_segment'],
            instrument=info['instrument'],interval='5',oi=False,
            fromDate=a.strftime('%Y-%m-%d %H:%M:%S'),toDate=b.strftime('%Y-%m-%d %H:%M:%S'))
        for attempt in range(retries):
            try:
                response=session.post(API_URL,headers={'access-token':token,'Content-Type':'application/json','Accept':'application/json'},json=payload,timeout=55)
                if response.status_code in (429,500,502,503,504):
                    time.sleep(min(2**attempt*2,25)); continue
                response.raise_for_status()
                data=response.json()
                if 'timestamp' not in data: raise ValueError(f'Unexpected response {str(data)[:150]}')
                if data['timestamp']:
                    part=pd.DataFrame({k:data[k] for k in ('timestamp','open','high','low','close','volume')})
                    part['datetime']=pd.to_datetime(part.pop('timestamp'),unit='s',utc=True).dt.tz_convert(TZ)
                    pieces.append(part.set_index('datetime'))
                break
            except requests.RequestException:
                if attempt==retries-1: raise
                time.sleep(min(2**attempt*2,25))
        else:
            raise RuntimeError(f'Dhan API exhausted retries for {symbol}: {a.date()}')
        time.sleep(float(os.environ.get('DHAN_DELAY_SECONDS','0.45')))
    if not pieces: return pd.DataFrame()
    return pd.concat(pieces).loc[lambda x:~x.index.duplicated(keep='last')].sort_index()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--symbols',default='',help='Comma-separated subset for testing')
    ap.add_argument('--max-symbols',type=int,default=0,help='Optional test cap; 0 means all')
    ap.add_argument('--start',default='')
    ap.add_argument('--end',default='')
    args=ap.parse_args()
    cfg=json.loads((ROOT/'config.json').read_text())
    start=args.start or cfg['start']; end=args.end or cfg['end']
    token=os.environ.get('DHAN_ACCESS_TOKEN','').strip()
    if not token: raise RuntimeError('Set DHAN_ACCESS_TOKEN in GitHub Actions secrets')
    master=pd.read_csv(MASTER_URL,dtype=str,low_memory=False)
    universe=master_universe(master,cfg.get('indices',[]))
    if args.symbols:
        wanted={s.strip().upper() for s in args.symbols.split(',') if s.strip()}
        unknown=wanted-set(universe)
        if unknown: print('WARNING: unresolved requested symbols:',sorted(unknown),flush=True)
        universe={k:v for k,v in universe.items() if k in wanted}
    if args.max_symbols>0: universe=dict(list(universe.items())[:args.max_symbols])
    print(f'UNIVERSE {len(universe)} instruments; {start} through {end}',flush=True)
    if not universe: raise RuntimeError('No instruments mapped')
    pd.DataFrame([dict(symbol=s,**v) for s,v in universe.items()]).to_csv(REPORTS/'universe.csv',index=False)
    trades=[]; coverage=[]
    for i,(symbol,info) in enumerate(universe.items(),1):
        try:
            candles=fetch_candles(symbol,info,start,end,token)
            if candles.empty: raise ValueError('No 5-minute candles returned')
            coverage.append(dict(symbol=symbol,status='OK',first=str(candles.index.min()),last=str(candles.index.max()),bars=len(candles)))
            trades.extend(backtest(candles,symbol,cfg))
            print(f'[{i}/{len(universe)}] {symbol}: {len(candles)} bars',flush=True)
        except Exception as e:
            coverage.append(dict(symbol=symbol,status='ERROR',message=str(e)[:300]))
            print(f'[{i}/{len(universe)}] {symbol}: ERROR {e}',flush=True)
        # Save partial reports in case GitHub times out during large-universe runs.
        if i%10==0:
            pd.DataFrame(coverage).to_csv(REPORTS/'coverage.csv',index=False)
            if trades: pd.DataFrame(trades).to_csv(REPORTS/'trades.csv',index=False)
    pd.DataFrame(coverage).to_csv(REPORTS/'coverage.csv',index=False)
    if not trades: raise RuntimeError('No trades; inspect coverage.csv')
    t=pd.DataFrame(trades); t.to_csv(REPORTS/'trades.csv',index=False)
    rows=[]
    for side,g in t.groupby('side'):
        win=g.loc[g.net>0,'net'].sum(); loss=-g.loc[g.net<0,'net'].sum()
        rows.append(dict(side=side,trades=len(g),win_rate_pct=round(100*(g.net>0).mean(),2),gross=round(g.gross.sum(),2),
                         estimated_cost=round(g.estimated_cost.sum(),2),net=round(g.net.sum(),2),
                         avg_net=round(g.net.mean(),2),profit_factor=round(win/loss,3) if loss else None))
    pd.DataFrame(rows).to_csv(REPORTS/'ranking.csv',index=False)
    t.assign(month=t.entry_time.str[:7]).groupby(['side','month'],as_index=False).agg(trades=('net','size'),net=('net','sum')).to_csv(REPORTS/'monthly.csv',index=False)
    t.groupby(['symbol','side'],as_index=False).agg(trades=('net','size'),net=('net','sum'),gross=('gross','sum')).to_csv(REPORTS/'symbol_ranking.csv',index=False)
    print('Completed. Underlying-only research. No live orders or historical options P&L.',flush=True)

if __name__=='__main__': main()
