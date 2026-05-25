//+------------------------------------------------------------------+
//|  OmniDeterministicExecutor.mq5                                     |
//|  Executes deterministic ICT signals with LIMIT orders, structural  |

//|  SL, 50% partial close at 1:2 RR, breakeven, and discrete OB-step  |
//|  trailing stops.                                                   |
//|                                                                    |
//|  Signal file:   omni_det_cmd.txt  (Python writes, EA reads)       |
//|  Format:        OPEN|SYMBOL|DIRECTION|ENTRY|SL|TP1|TP2|CONFIDENCE   |
//|  Result file:   omni_det_result.txt (EA writes, Python reads)       |
//+------------------------------------------------------------------+
#property copyright "OMNI Trading Dashboard"
#property version   "2.00"
#property strict

// --- Inputs ---
input int    PollMilliseconds     = 500;        // Poll interval (ms)
input int    MaxSpreadPips        = 50;         // Max spread in points (5.0 pips = 50 pts for gold)
input ulong  MagicNumber          = 20250411;   // Must match deterministic engine
input double PartialCloseFrac     = 0.50;       // Fraction to close at TP1 (0.50 = 50%)
input double BreakevenOffsetPips  = 0.10;       // Move SL to entry + 1 pip after partial close
input int    TrailBufferPips      = 20;         // Pips behind OB for discrete step-trail (2.0 pips)
input string SignalFile            = "omni_det_cmd.txt";
input string ResultFile            = "omni_det_result.txt";
input string LogFileName           = "OmniDeterministicEA.log";

// --- Trade state ---
struct TradeState {
   ulong    ticket;
   string   sym;
   string   dir;       // "BULL" or "BEAR"
   double   entry;
   double   initial_sl;
   double   tp1;
   double   tp2;
   double   initial_vol;
   bool     partial_done;
   bool     be_done;
   datetime open_time;
};

TradeState g_trades[];
int        g_handle_log = INVALID_HANDLE;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetMillisecondTimer(PollMilliseconds);
   g_handle_log = FileOpen(LogFileName, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
   if(g_handle_log == INVALID_HANDLE) Print("[WARN] Cannot open log file ", LogFileName);
   Log("=== OmniDeterministicExecutor v2.0 init | Magic=" + (string)MagicNumber + " ===");
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_handle_log != INVALID_HANDLE) FileClose(g_handle_log);
  }

//+------------------------------------------------------------------+
void OnTick() { }
void OnTimer() { ProcessSignalFile(); ManagePositions(); }

//+------------------------------------------------------------------+
void Log(string msg)
  {
   string line = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + " | " + msg;
   Print(line);
   if(g_handle_log != INVALID_HANDLE) { FileWriteString(g_handle_log, line + "\r\n"); FileFlush(g_handle_log); }
  }

//+------------------------------------------------------------------+
//| Read signal file and place LIMIT orders                            |
//+------------------------------------------------------------------+
void ProcessSignalFile()
  {
   if(!FileIsExist(SignalFile, FILE_COMMON)) return;
   int fh = FileOpen(SignalFile, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) { Log("ERROR: cannot open " + SignalFile); return; }
   string raw = FileReadString(fh); FileClose(fh); FileDelete(SignalFile, FILE_COMMON);

   StringReplace(raw, "\r", ""); StringReplace(raw, "\n", "");
   StringTrimLeft(raw); StringTrimRight(raw);
   if(StringLen(raw) == 0) return;

   string parts[]; int n = StringSplit(raw, '|', parts);
   if(n < 8 || parts[0] != "OPEN") { Log("WARN: malformed signal: " + raw); return; }

   string sym = parts[1]; string dir = parts[2];
   double entry = StringToDouble(parts[3]);
   double sl    = StringToDouble(parts[4]);
   double tp1   = StringToDouble(parts[5]);
   double tp2   = StringToDouble(parts[6]);
   double conf  = StringToDouble(parts[7]);

   if(!SymbolSelect(sym, true)) { Log("SKIP: symbol not found " + sym); return; }
   long spread_pts = SymbolInfoInteger(sym, SYMBOL_SPREAD);
   if(spread_pts > MaxSpreadPips) { Log("SKIP: " + sym + " spread " + (string)spread_pts + " > max"); return; }

   // Default lot 0.01; you may scale by confidence or account balance
   double vol = 0.01;
   int ticket = PlaceLimitOrder(sym, dir, entry, sl, tp1, vol);
   if(ticket <= 0)
     { Log("FAIL: " + dir + " LIMIT " + sym + " @" + DoubleToString(entry, _Digits) + " err=" + (string)GetLastError()); return; }

   Log("PLACED " + dir + " LIMIT " + sym + " ticket=" + (string)ticket + " @" + DoubleToString(entry, _Digits)
       + " SL=" + DoubleToString(sl, _Digits) + " TP1=" + DoubleToString(tp1, _Digits) + " TP2=" + DoubleToString(tp2, _Digits));
   WriteResult("OK|" + (string)ticket + "|" + sym + "|" + dir + "|" + DoubleToString(entry, _Digits));

   int idx = ArraySize(g_trades); ArrayResize(g_trades, idx + 1);
   g_trades[idx].ticket      = ticket;
   g_trades[idx].sym         = sym;
   g_trades[idx].dir         = dir;
   g_trades[idx].entry       = entry;
   g_trades[idx].initial_sl  = sl;
   g_trades[idx].tp1         = tp1;
   g_trades[idx].tp2         = tp2;
   g_trades[idx].initial_vol = vol;
   g_trades[idx].partial_done= false;
   g_trades[idx].be_done     = false;
   g_trades[idx].open_time   = TimeCurrent();
  }

//+------------------------------------------------------------------+
//| Place pending LIMIT order                                          |
//+------------------------------------------------------------------+
int PlaceLimitOrder(string sym, string dir, double entry, double sl, double tp, double vol)
  {
   MqlTradeRequest req = {}; MqlTradeResult res = {};
   req.action       = TRADE_ACTION_PENDING;
   req.symbol       = sym;
   req.volume       = vol;
   req.price        = NormalizeDouble(entry, _Digits);
   req.sl           = NormalizeDouble(sl,    _Digits);
   req.tp           = NormalizeDouble(tp,    _Digits);
   req.magic        = MagicNumber;
   req.deviation    = 10;
   req.comment      = "DET|" + dir;
   if(dir == "BULL")      req.type = ORDER_TYPE_BUY_LIMIT;
   else if(dir == "BEAR") req.type = ORDER_TYPE_SELL_LIMIT;
   else                   return 0;
   if(!OrderSend(req, res)) return 0;
   return (int)res.order;
  }

//+------------------------------------------------------------------+
//| Manage open positions: partial close, breakeven, discrete trail    |
//+------------------------------------------------------------------+
void ManagePositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      string sym = PositionGetString(POSITION_SYMBOL);
      ENUM_POSITION_TYPE ptype = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      double tp    = PositionGetDouble(POSITION_TP);
      double vol   = PositionGetDouble(POSITION_VOLUME);
      double cur   = (ptype == POSITION_TYPE_BUY) ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);

      // Find matching trade state
      int idx = FindTradeIdx(ticket);
      if(idx < 0) continue;
      TradeState &ts = g_trades[idx];
      double pip = SymbolInfoDouble(sym, SYMBOL_POINT) * 10; // rough pip conversion
      if(StringFind(sym, "XAU") >= 0 || StringFind(sym, "GOLD") >= 0) pip = 0.01;
      if(pip <= 0) pip = SymbolInfoDouble(sym, SYMBOL_POINT);

      // --- 1. Partial close at TP1 (1:2 RR achieved) ---
      if(!ts.partial_done)
        {
         bool hit_tp1 = false;
         if(ts.dir == "BULL" && cur >= ts.tp1) hit_tp1 = true;
         if(ts.dir == "BEAR" && cur <= ts.tp1) hit_tp1 = true;
         if(hit_tp1)
           {
            double close_vol = NormalizeDouble(vol * PartialCloseFrac, 2);
            if(close_vol > 0 && close_vol < vol)
              {
               MqlTradeRequest creq = {}; MqlTradeResult cres = {};
               creq.action   = TRADE_ACTION_DEAL;
               creq.position = ticket;
               creq.symbol   = sym;
               creq.volume   = close_vol;
               creq.price    = (ptype == POSITION_TYPE_BUY) ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);
               creq.type     = (ptype == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
               creq.deviation= 10;
               creq.comment  = "DET|PARTIAL|50%";
               if(OrderSend(creq, cres))
                 {
                  ts.partial_done = true;
                  Log("PARTIAL CLOSE ticket=" + (string)ticket + " vol=" + DoubleToString(close_vol, 2));
                 }
               else
                  Log("PARTIAL FAIL ticket=" + (string)ticket + " err=" + (string)GetLastError());
              }
           }
        }

      // --- 2. Move remaining to breakeven +1 pip after partial ---
      if(ts.partial_done && !ts.be_done)
        {
         double be_sl = ts.entry + (ts.dir == "BULL" ? BreakevenOffsetPips * pip : -BreakevenOffsetPips * pip);
         be_sl = NormalizeDouble(be_sl, _Digits);
         if((ts.dir == "BULL" && be_sl > sl && MathAbs(be_sl - sl) >= pip)
            || (ts.dir == "BEAR" && be_sl < sl && MathAbs(be_sl - sl) >= pip))
           {
            MqlTradeRequest mreq = {}; MqlTradeResult mres = {};
            mreq.action = TRADE_ACTION_SLTP;
            mreq.position = ticket;
            mreq.symbol   = sym;
            mreq.sl       = be_sl;
            mreq.tp       = ts.tp2;
            if(OrderSend(mreq, mres))
              {
               ts.be_done = true;
               Log("BREAKEVEN ticket=" + (string)ticket + " SL→" + DoubleToString(be_sl, _Digits));
              }
            else
               Log("BREAKEVEN FAIL ticket=" + (string)ticket + " err=" + (string)GetLastError());
           }
        }

      // --- 3. Discrete structural step-trail after breakeven ---
      // Only on M1 new bar to avoid excessive scanning
      if(ts.be_done && i % 3 == 0) // throttle: every 3rd tick check
        {
         double new_sl = DiscreteOBStepTrail(sym, ts.dir, ts.entry, sl, pip);
         if(new_sl > 0 &&
            ((ts.dir == "BULL" && new_sl > sl) || (ts.dir == "BEAR" && new_sl < sl))
            && MathAbs(new_sl - sl) >= pip)
           {
            MqlTradeRequest mreq = {}; MqlTradeResult mres = {};
            mreq.action   = TRADE_ACTION_SLTP;
            mreq.position = ticket;
            mreq.symbol   = sym;
            mreq.sl       = NormalizeDouble(new_sl, _Digits);
            mreq.tp       = ts.tp2;
            if(OrderSend(mreq, mres))
               Log("STEP-TRAIL ticket=" + (string)ticket + " SL→" + DoubleToString(new_sl, _Digits));
            else
               Log("STEP-TRAIL FAIL ticket=" + (string)ticket + " err=" + (string)GetLastError());
           }
        }

      // --- 4. Close remaining on opposing BOS/CHoCH ---
      if(ts.be_done && OpposingStructureBreak(sym, ts.dir))
        {
         MqlTradeRequest creq = {}; MqlTradeResult cres = {};
         creq.action   = TRADE_ACTION_DEAL;
         creq.position = ticket;
         creq.symbol   = sym;
         creq.volume   = vol;
         creq.price    = (ptype == POSITION_TYPE_BUY) ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);
         creq.type     = (ptype == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
         creq.deviation= 10;
         creq.comment  = "DET|OPPOSING_BOS";
         if(OrderSend(creq, cres))
            Log("CLOSE OPPOSING_BOS ticket=" + (string)ticket);
         else
            Log("CLOSE OPPOSING_BOS FAIL ticket=" + (string)ticket + " err=" + (string)GetLastError());
         // Remove from tracking
         ArrayRemove(g_trades, idx, 1);
        }
     }
  }

//+------------------------------------------------------------------+
//| Discrete OB-step trail using last completed bars on current TF     |
//+------------------------------------------------------------------+
double DiscreteOBStepTrail(string sym, string dir, double entry, double current_sl, double pip)
  {
   // Use M15 for structural stepping; scan last 20 bars for a fresh OB
   datetime arrTime[]; double arrO[], arrH[], arrL[], arrC[];
   int copied = CopyRates(sym, PERIOD_M15, 0, 20, arrTime, arrO, arrH, arrL, arrC);
   if(copied < 5) return 0;

   int buf_pts = TrailBufferPips; // points (not pips)
   double buf  = buf_pts * SymbolInfoDouble(sym, SYMBOL_POINT);

   if(dir == "BULL")
     {
      for(int i = copied - 2; i >= 1; i--)
        {
         // Bullish OB = bearish candle (c < o) followed by displacement up
         if(arrC[i-1] < arrO[i-1] && arrC[i] > arrO[i-1])
           {
            double sl_candidate = arrC[i-1] - buf; // 2 pts below bearish body_bottom
            if(sl_candidate > current_sl) return sl_candidate;
           }
        }
     }
   else
     {
      for(int i = copied - 2; i >= 1; i--)
        {
         // Bearish OB = bullish candle (c > o) followed by displacement down
         if(arrC[i-1] > arrO[i-1] && arrC[i] < arrO[i-1])
           {
            double sl_candidate = arrO[i-1] + buf; // 2 pts above bullish body_top
            if(sl_candidate < current_sl) return sl_candidate;
           }
        }
     }
   return 0;
  }

//+------------------------------------------------------------------+
//| Check for opposing BOS / CHoCH on M15                             |
//+------------------------------------------------------------------+
bool OpposingStructureBreak(string sym, string dir)
  {
   double arrO[], arrH[], arrL[], arrC[]; datetime arrT[];
   int copied = CopyRates(sym, PERIOD_M15, 0, 15, arrT, arrO, arrH, arrL, arrC);
   if(copied < 5) return false;

   if(dir == "BULL")
     {
      // Look for a new swing low being broken to the downside (bearish BOS)
      double recent_low = arrL[copied - 3];
      if(arrC[copied - 1] < recent_low && arrC[copied - 2] < recent_low)
         return true;
     }
   else
     {
      double recent_high = arrH[copied - 3];
      if(arrC[copied - 1] > recent_high && arrC[copied - 2] > recent_high)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Helpers                                                            |
//+------------------------------------------------------------------+
int FindTradeIdx(ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_trades); i++)
      if(g_trades[i].ticket == ticket) return i;
   return -1;
  }

void WriteResult(string msg)
  {
   int fh = FileOpen(ResultFile, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   FileWriteString(fh, msg + "\r\n");
   FileClose(fh);
  }

//+------------------------------------------------------------------+
//| ArrayRemove helper for MQL5 TradeState[]                           |
//+------------------------------------------------------------------+
void ArrayRemove(TradeState &arr[], int idx, int count=1)
  {
   int len = ArraySize(arr);
   if(len <= 0 || idx < 0 || idx >= len) return;
   if(idx + count > len) count = len - idx;
   for(int i = idx; i < len - count; i++)
      arr[i] = arr[i + count];
   ArrayResize(arr, len - count);
  }
