//+------------------------------------------------------------------+
//|  OmniExecutor.mq5 — executes trade commands from auto_trader.py |
//|                                                                  |
//|  Command file:  omni_cmd.txt    (Python writes, EA reads)        |
//|  Result file:   omni_result.txt (EA writes, Python reads)        |
//|                                                                  |
//|  Command formats:                                                |
//|    OPEN|SYMBOL|ORDER_TYPE|PRICE|SL|TP|VOLUME|COMMENT            |
//|    CLOSE|TICKET|||||VOLUME|                                      |
//|    MODIFY|TICKET|||SL|TP||                                       |
//|                                                                  |
//|  ORDER_TYPE values: BUY BUY_LIMIT BUY_STOP SELL SELL_LIMIT       |
//|                     SELL_STOP                                    |
//|                                                                  |
//|  Drop on any chart.  Does NOT interfere with OmniExport.mq5.    |
//+------------------------------------------------------------------+
#property copyright "OMNI Trading Dashboard"
#property version   "1.00"
#property strict

input int    PollMilliseconds = 500;   // How often to check for commands (ms)
input ulong  MagicNumber      = 20250411; // Must match OUR_MAGIC in auto_trader.py
input int    SlippagePips     = 3;     // Maximum slippage allowed (pips)

string CMD_FILE    = "omni_cmd.txt";
string RESULT_FILE = "omni_result.txt";

int OnInit()
  {
   EventSetMillisecondTimer(PollMilliseconds);
   Print("OmniExecutor ready | Magic=", MagicNumber, " | Poll=", PollMilliseconds, "ms");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int r) { EventKillTimer(); }
void OnTick() {}

void OnTimer()
  {
   // Check for command file (use FILE_COMMON so it matches Python's path)
   if(!FileIsExist(CMD_FILE, FILE_COMMON)) return;

   int fh = FileOpen(CMD_FILE, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   string cmd = FileReadString(fh);
   FileClose(fh);

   // Delete command file before executing — prevents duplicate execution on crash/retry
   FileDelete(CMD_FILE, FILE_COMMON);

   string trimmed = cmd;
   StringTrimLeft(trimmed);
   StringTrimRight(trimmed);
   if(StringLen(trimmed) == 0) return;

   string result = ProcessCommand(cmd);
   WriteResult(result);
  }

//+------------------------------------------------------------------+
//| Route command to appropriate handler                             |
//+------------------------------------------------------------------+
string ProcessCommand(string cmd)
  {
   string parts[];
   int n = StringSplit(cmd, '|', parts);
   if(n < 1) return "ERROR|empty command";

   string action = parts[0];

   if(action == "OPEN"   && n >= 8) return CmdOpen(parts);
   if(action == "CLOSE"  && n >= 2) return CmdClose(parts);
   if(action == "MODIFY" && n >= 6) return CmdModify(parts);

   return "ERROR|unknown command: " + cmd;
  }

//+------------------------------------------------------------------+
//| OPEN|SYMBOL|TYPE|PRICE|SL|TP|VOLUME|COMMENT                     |
//+------------------------------------------------------------------+
string CmdOpen(string &p[])
  {
   string symbol  = p[1];
   string otype   = p[2];
   double price   = StringToDouble(p[3]);
   double sl      = StringToDouble(p[4]);
   double tp      = StringToDouble(p[5]);
   double volume  = StringToDouble(p[6]);
   string comment = p[7];

   if(!SymbolSelect(symbol, true))
      return "ERROR|symbol not found: " + symbol;
   if(volume <= 0)
      return "ERROR|invalid volume: " + DoubleToString(volume, 2);

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   ZeroMemory(req);
   ZeroMemory(res);

   req.symbol   = symbol;
   req.volume   = volume;
   req.sl       = sl;
   req.tp       = tp;
   req.comment  = comment;
   req.magic    = MagicNumber;
   req.deviation = SlippagePips;

   // Determine action and order type
   if(otype == "BUY")
     {
      req.action = TRADE_ACTION_DEAL;
      req.type   = ORDER_TYPE_BUY;
      req.price  = SymbolInfoDouble(symbol, SYMBOL_ASK);
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "SELL")
     {
      req.action = TRADE_ACTION_DEAL;
      req.type   = ORDER_TYPE_SELL;
      req.price  = SymbolInfoDouble(symbol, SYMBOL_BID);
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "BUY_LIMIT")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_BUY_LIMIT;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "BUY_STOP")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_BUY_STOP;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "SELL_LIMIT")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_SELL_LIMIT;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else if(otype == "SELL_STOP")
     {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_SELL_STOP;
      req.price  = price;
      req.type_filling = SelectFilling(symbol);
     }
   else
      return "ERROR|unknown order type: " + otype;

   if(!OrderSend(req, res))
     {
      int err = GetLastError();
      return "ERROR|OrderSend failed code=" + IntegerToString(err)
             + " retcode=" + IntegerToString(res.retcode)
             + " " + res.comment;
     }

   if(res.retcode == TRADE_RETCODE_DONE || res.retcode == TRADE_RETCODE_PLACED)
      return "OK|" + IntegerToString(res.order);

   return "ERROR|retcode=" + IntegerToString(res.retcode) + " " + res.comment;
  }

//+------------------------------------------------------------------+
//| CLOSE|TICKET|||||VOLUME|                                         |
//+------------------------------------------------------------------+
string CmdClose(string &p[])
  {
   ulong  ticket = (ulong)StringToInteger(p[1]);
   double vol    = (ArraySize(p) >= 7 && StringLen(p[6]) > 0)
                   ? StringToDouble(p[6]) : 0;

   if(!PositionSelectByTicket(ticket))
      return "ERROR|position not found: " + IntegerToString(ticket);

   string symbol  = PositionGetString(POSITION_SYMBOL);
   double pos_vol = PositionGetDouble(POSITION_VOLUME);
   long   pos_type = PositionGetInteger(POSITION_TYPE);

   double close_vol = (vol > 0 && vol < pos_vol) ? vol : pos_vol;

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   ZeroMemory(req);
   ZeroMemory(res);

   req.action   = TRADE_ACTION_DEAL;
   req.position = ticket;
   req.symbol   = symbol;
   req.volume   = close_vol;
   req.type     = (pos_type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price    = (pos_type == POSITION_TYPE_BUY)
                  ? SymbolInfoDouble(symbol, SYMBOL_BID)
                  : SymbolInfoDouble(symbol, SYMBOL_ASK);
   req.deviation = SlippagePips;
   req.magic    = MagicNumber;
   req.comment  = "OMNI_CLOSE";
   req.type_filling = SelectFilling(symbol);

   if(!OrderSend(req, res))
     {
      int err = GetLastError();
      return "ERROR|Close failed code=" + IntegerToString(err)
             + " retcode=" + IntegerToString(res.retcode)
             + " " + res.comment;
     }

   if(res.retcode == TRADE_RETCODE_DONE)
      return "OK|" + IntegerToString(ticket);

   return "ERROR|retcode=" + IntegerToString(res.retcode) + " " + res.comment;
  }

//+------------------------------------------------------------------+
//| MODIFY|TICKET|||SL|TP||                                          |
//+------------------------------------------------------------------+
string CmdModify(string &p[])
  {
   ulong  ticket = (ulong)StringToInteger(p[1]);
   double sl     = (ArraySize(p) >= 5 && StringLen(p[4]) > 0) ? StringToDouble(p[4]) : 0;
   double tp     = (ArraySize(p) >= 6 && StringLen(p[5]) > 0) ? StringToDouble(p[5]) : 0;

   // Try position first, then pending order
   bool is_position = PositionSelectByTicket(ticket);
   bool is_order    = !is_position && OrderSelect(ticket);

   if(!is_position && !is_order)
      return "ERROR|ticket not found: " + IntegerToString(ticket);

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   ZeroMemory(req);
   ZeroMemory(res);

   if(is_position)
     {
      req.action   = TRADE_ACTION_SLTP;
      req.position = ticket;
      req.symbol   = PositionGetString(POSITION_SYMBOL);
      req.sl       = sl;
      req.tp       = tp;
     }
   else
     {
      req.action = TRADE_ACTION_MODIFY;
      req.order  = ticket;
      req.price  = OrderGetDouble(ORDER_PRICE_OPEN);
      req.sl     = sl;
      req.tp     = tp;
     }

   if(!OrderSend(req, res))
     {
      int err = GetLastError();
      return "ERROR|Modify failed code=" + IntegerToString(err)
             + " retcode=" + IntegerToString(res.retcode)
             + " " + res.comment;
     }

   if(res.retcode == TRADE_RETCODE_DONE)
      return "OK|" + IntegerToString(ticket);

   return "ERROR|retcode=" + IntegerToString(res.retcode) + " " + res.comment;
  }

//+------------------------------------------------------------------+
//| Write result file for Python to read                             |
//+------------------------------------------------------------------+
void WriteResult(string result)
  {
   int fh = FileOpen(RESULT_FILE, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE)
     {
      Print("OmniExecutor: cannot write result file");
      return;
     }
   FileWriteString(fh, result);
   FileClose(fh);
   Print("OmniExecutor result: ", result);
  }

//+------------------------------------------------------------------+
//| Select the best filling mode for a symbol                        |
//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING SelectFilling(string symbol)
  {
   // SYMBOL_FILLING_MODE bitmask: bit0=FOK(1), bit1=IOC(2).
   // When both bits are 0 the broker uses RETURN mode (MetaQuotes demo default).
   long filling = (long)SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) != 0)
      return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) != 0)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
  }
//+------------------------------------------------------------------+
