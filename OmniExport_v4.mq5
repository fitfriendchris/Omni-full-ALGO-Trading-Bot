//+------------------------------------------------------------------+
//| OmniExport v4.1 — Multi-TF Export + Auto-Trade Execution        |
//| Exports: D1/H4/H1/M15/M5 bars, key levels, executes commands    |
//| Added:   spread/bid/ask per chart symbol, NDOG/NWOG gaps,       |
//|          full gmt_time with date for Python kill-zone detection  |
//+------------------------------------------------------------------+
#property copyright "OMNI ICT Auto-Trader"
#property version   "4.10"
#property strict

input int    UpdateSeconds = 3;
input string DataFile      = "omni_data.json";
input string CmdFile       = "omni_cmd.txt";
input string ResultFile    = "omni_result.txt";
input int    MagicNumber   = 20250411;
input bool   AutoTradeEnabled = false;   // MUST set true to enable live trading

// Priority symbols for multi-TF export
string primarySymbols[] = {"XAUUSD","XAGUSD","EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD"};

// All watchlist symbols for price scan
string allSymbols[] = {
   "EURUSD","GBPUSD","AUDUSD","NZDUSD","USDCAD","USDCHF","USDJPY",
   "EURGBP","EURJPY","GBPJPY","AUDJPY","CADJPY","CHFJPY","EURAUD",
   "EURCAD","EURCHF","GBPAUD","GBPCAD","GBPCHF","GBPNZD","AUDCAD",
   "AUDCHF","AUDNZD","NZDCAD","NZDCHF","NZDJPY","EURNZD",
   "XAUUSD","XAGUSD",
   "BTCUSD","ETHUSD","LTCUSD","XRPUSD","ADAUSD","SOLUSD",
   "DOGUSD","DOTUSD","LNKUSD","XLMUSD","AVAUSD","BCHUSD",
   ".US30Cash",".USTECHCash",".US500Cash"
};

int  OnInit()              { EventSetTimer(UpdateSeconds); ExportData(); return(INIT_SUCCEEDED); }
void OnDeinit(const int r) { EventKillTimer(); }
void OnTimer()             { ExportData(); if(AutoTradeEnabled) CheckCommands(); }
void OnTick()              {}

//+------------------------------------------------------------------+
// HELPERS
//+------------------------------------------------------------------+
string Q(string s)   { return "\"" + s + "\""; }
string KV(string k, string v, bool last=false)  { return "\""+k+"\":\""+v+"\""+(last?"":","); }
string KVN(string k, string v, bool last=false) { return "\""+k+"\":"+v+(last?"":","); }

string GetSession()
  {
   MqlDateTime t; TimeToStruct(TimeGMT(),t); int h=t.hour;
   if(h>=22||h<7)  return "ASIA";
   if(h>=7&&h<12)  return "LONDON";
   if(h>=12&&h<17) return "NEW_YORK";
   return "OVERLAP";
  }

string GetAMDPhase()
  {
   MqlDateTime t; TimeToStruct(TimeGMT(),t);
   int tot=t.hour*60+t.min;
   if(t.hour>=22||t.hour<7) return "ACCUMULATION";
   if(tot>=420&&tot<570)     return "MANIPULATION";
   if(tot>=720&&tot<900)     return "DISTRIBUTION";
   if(tot>=900&&tot<1020)    return "LONDON_CLOSE";
   return "REBALANCE";
  }

// Export N bars of a timeframe as JSON array
string ExportBars(string sym, ENUM_TIMEFRAMES tf, int count, string tfName)
  {
   string out = "\""+tfName+"\":[";
   MqlRates rates[];
   int copied = CopyRates(sym, tf, 0, count, rates);
   if(copied <= 0) return out + "]";
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   for(int i=copied-1; i>=0; i--)
     {
      string sep = (i>0)?",":"";
      out += "{";
      out += "\"t\":\""+TimeToString(rates[i].time,TIME_DATE|TIME_SECONDS)+"\",";
      out += "\"o\":"+DoubleToString(rates[i].open,digits)+",";
      out += "\"h\":"+DoubleToString(rates[i].high,digits)+",";
      out += "\"l\":"+DoubleToString(rates[i].low,digits)+",";
      out += "\"c\":"+DoubleToString(rates[i].close,digits)+",";
      out += "\"v\":"+IntegerToString(rates[i].tick_volume);
      out += "}"+sep;
     }
   out += "]";
   return out;
  }

// Get previous day/week high-low
void GetKeyLevels(string sym, int digits,
                  double &pdh, double &pdl, double &pwh, double &pwl,
                  double &pmh, double &pml, double &wdh, double &wdl)
  {
   MqlRates d1rates[], wrates[];
   // Previous Day
   if(CopyRates(sym,PERIOD_D1,1,2,d1rates)>=2)
     { pdh=d1rates[0].high; pdl=d1rates[0].low; wdh=d1rates[1].high; wdl=d1rates[1].low; }
   // Previous Week
   if(CopyRates(sym,PERIOD_W1,1,1,wrates)>=1)
     { pwh=wrates[0].high; pwl=wrates[0].low; }
   // Previous Month
   MqlRates mrates[];
   if(CopyRates(sym,PERIOD_MN1,1,1,mrates)>=1)
     { pmh=mrates[0].high; pml=mrates[0].low; }
  }

//+------------------------------------------------------------------+
// MAIN EXPORT
//+------------------------------------------------------------------+
void ExportData()
  {
   int fh = FileOpen(DataFile, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh==INVALID_HANDLE) { Print("Cannot open data file"); return; }

   MqlDateTime gmt; TimeToStruct(TimeGMT(),gmt);
   // Full date+time string so Python can parse kill zones precisely
   string gmtStr = TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS);
   string session = GetSession(); string amd = GetAMDPhase();

   string j = "{\n";
   j += "\"timestamp\":\""+TimeToString(TimeCurrent(),TIME_DATE|TIME_SECONDS)+"\",\n";
   j += "\"session\":\""+session+"\",\n";
   j += "\"amd_phase\":\""+amd+"\",\n";
   j += "\"gmt_time\":\""+gmtStr+"\",\n";
   j += "\"auto_trade_enabled\":"+IntegerToString(AutoTradeEnabled?1:0)+",\n";

   // ── Account ──────────────────────────────────────────────────────
   j += "\"account\":{\n";
   j += "\"login\":"+IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))+",\n";
   j += "\"name\":\""+AccountInfoString(ACCOUNT_NAME)+"\",\n";
   j += "\"server\":\""+AccountInfoString(ACCOUNT_SERVER)+"\",\n";
   j += "\"currency\":\""+AccountInfoString(ACCOUNT_CURRENCY)+"\",\n";
   j += "\"balance\":"+DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2)+",\n";
   j += "\"equity\":"+DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2)+",\n";
   j += "\"margin\":"+DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2)+",\n";
   j += "\"free_margin\":"+DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2)+",\n";
   j += "\"margin_level\":"+DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2)+",\n";
   j += "\"profit\":"+DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT),2)+",\n";
   j += "\"leverage\":"+IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE))+"\n";
   j += "},\n";

   // ── Open Positions ────────────────────────────────────────────────
   j += "\"positions\":[\n";
   int total=PositionsTotal(); string posLines="";
   for(int i=0;i<total;i++)
     {
      ulong ticket=PositionGetTicket(i); if(ticket==0) continue;
      if(posLines!="") posLines+=",\n";
      posLines+="{\"ticket\":"+IntegerToString(ticket);
      posLines+=",\"symbol\":\""+PositionGetString(POSITION_SYMBOL)+"\"";
      posLines+=",\"type\":\""+(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL")+"\"";
      posLines+=",\"volume\":"+DoubleToString(PositionGetDouble(POSITION_VOLUME),2);
      posLines+=",\"open_price\":"+DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),5);
      posLines+=",\"current_price\":"+DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5);
      posLines+=",\"sl\":"+DoubleToString(PositionGetDouble(POSITION_SL),5);
      posLines+=",\"tp\":"+DoubleToString(PositionGetDouble(POSITION_TP),5);
      posLines+=",\"profit\":"+DoubleToString(PositionGetDouble(POSITION_PROFIT),2);
      posLines+=",\"swap\":"+DoubleToString(PositionGetDouble(POSITION_SWAP),2);
      posLines+=",\"magic\":"+IntegerToString(PositionGetInteger(POSITION_MAGIC));
      posLines+=",\"time\":\""+TimeToString((datetime)PositionGetInteger(POSITION_TIME),TIME_DATE|TIME_SECONDS)+"\"}";
     }
   j+=posLines+"\n],\n";

   // ── History ───────────────────────────────────────────────────────
   j += "\"history\":[\n";
   HistorySelect(TimeCurrent()-90*86400,TimeCurrent());
   int deals=HistoryDealsTotal(); string histLines="";
   for(int i=0;i<deals;i++)
     {
      ulong ticket=HistoryDealGetTicket(i); if(ticket==0) continue;
      long dtype=HistoryDealGetInteger(ticket,DEAL_TYPE);
      if(dtype!=DEAL_TYPE_BUY&&dtype!=DEAL_TYPE_SELL) continue;
      long entry=HistoryDealGetInteger(ticket,DEAL_ENTRY);
      if(histLines!="") histLines+=",\n";
      histLines+="{\"ticket\":"+IntegerToString(ticket);
      histLines+=",\"time\":\""+TimeToString((datetime)HistoryDealGetInteger(ticket,DEAL_TIME),TIME_DATE|TIME_SECONDS)+"\"";
      histLines+=",\"symbol\":\""+HistoryDealGetString(ticket,DEAL_SYMBOL)+"\"";
      histLines+=",\"type\":\""+(dtype==DEAL_TYPE_BUY?"BUY":"SELL")+"\"";
      histLines+=",\"entry\":\""+(entry==DEAL_ENTRY_IN?"IN":(entry==DEAL_ENTRY_OUT?"OUT":"INOUT"))+"\"";
      histLines+=",\"volume\":"+DoubleToString(HistoryDealGetDouble(ticket,DEAL_VOLUME),2);
      histLines+=",\"price\":"+DoubleToString(HistoryDealGetDouble(ticket,DEAL_PRICE),5);
      histLines+=",\"profit\":"+DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2);
      histLines+=",\"swap\":"+DoubleToString(HistoryDealGetDouble(ticket,DEAL_SWAP),2);
      histLines+=",\"commission\":"+DoubleToString(HistoryDealGetDouble(ticket,DEAL_COMMISSION),2)+"}";
     }
   j+=histLines+"\n],\n";

   // ── Multi-TF bars for primary symbols ────────────────────────────
   j += "\"charts\":{\n";
   int primCount=ArraySize(primarySymbols);
   for(int i=0;i<primCount;i++)
     {
      string sym=primarySymbols[i];
      double bid=SymbolInfoDouble(sym,SYMBOL_BID);
      if(bid==0.0) continue;
      int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
      double pdh=0,pdl=0,pwh=0,pwl=0,pmh=0,pml=0,wdh=0,wdl=0;
      GetKeyLevels(sym,digits,pdh,pdl,pwh,pwl,pmh,pml,wdh,wdl);
      string sep=(i<primCount-1)?",":"";
      j += "\""+sym+"\":{\n";
      j += ExportBars(sym,PERIOD_D1,20,"D1")+",\n";
      j += ExportBars(sym,PERIOD_H4,20,"H4")+",\n";
      j += ExportBars(sym,PERIOD_H1,20,"H1")+",\n";
      j += ExportBars(sym,PERIOD_M15,15,"M15")+",\n";
      j += ExportBars(sym,PERIOD_M5,10,"M5")+",\n";
      // Live bid/ask/spread — used by Python spread guard
      double cbid = SymbolInfoDouble(sym, SYMBOL_BID);
      double cask = SymbolInfoDouble(sym, SYMBOL_ASK);
      double cpt  = SymbolInfoDouble(sym, SYMBOL_POINT);
      int    cspd = (cpt>0)?(int)MathRound((cask-cbid)/cpt):0;
      j += "\"bid\":"+DoubleToString(cbid,digits)+",\n";
      j += "\"ask\":"+DoubleToString(cask,digits)+",\n";
      j += "\"spread\":"+IntegerToString(cspd)+",\n";

      // NDOG / NWOG opening gaps — used by Python ict_precision.py
      MqlRates dRates[2], wRates[2];
      double ndog_open=0, ndog_close=0, nwog_open=0, nwog_close=0;
      if(CopyRates(sym,PERIOD_D1,0,2,dRates)==2)
        { ndog_close=dRates[1].close; ndog_open=dRates[0].open; }
      if(CopyRates(sym,PERIOD_W1,0,2,wRates)==2)
        { nwog_close=wRates[1].close; nwog_open=wRates[0].open; }
      j += "\"ndog_close\":"+DoubleToString(ndog_close,digits)+",\n";
      j += "\"ndog_open\":"+DoubleToString(ndog_open,digits)+",\n";
      j += "\"nwog_close\":"+DoubleToString(nwog_close,digits)+",\n";
      j += "\"nwog_open\":"+DoubleToString(nwog_open,digits)+",\n";

      // Symbol info for lot sizing
      j += "\"tick_size\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE),8)+",\n";
      j += "\"tick_value\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE),8)+",\n";
      j += "\"contract_size\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_TRADE_CONTRACT_SIZE),2)+",\n";
      j += "\"point\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_POINT),8)+",\n";
      j += "\"min_lot\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN),2)+",\n";
      j += "\"max_lot\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_VOLUME_MAX),2)+",\n";
      j += "\"lot_step\":"+DoubleToString(SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP),2)+",\n";
      // Key levels
      j += "\"pdh\":"+DoubleToString(pdh,digits)+",\n";
      j += "\"pdl\":"+DoubleToString(pdl,digits)+",\n";
      j += "\"pwh\":"+DoubleToString(pwh,digits)+",\n";
      j += "\"pwl\":"+DoubleToString(pwl,digits)+",\n";
      j += "\"pmh\":"+DoubleToString(pmh,digits)+",\n";
      j += "\"pml\":"+DoubleToString(pml,digits)+"\n";
      j += "}"+sep+"\n";
     }
   j += "},\n";

   // ── All symbol prices + ICT signals ──────────────────────────────
   j += "\"prices\":[\n";
   int symCount=ArraySize(allSymbols); string priceLines="";
   for(int i=0;i<symCount;i++)
     {
      string sym=allSymbols[i];
      double bid=SymbolInfoDouble(sym,SYMBOL_BID);
      double ask=SymbolInfoDouble(sym,SYMBOL_ASK);
      double point=SymbolInfoDouble(sym,SYMBOL_POINT);
      int digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
      if(bid==0.0) continue;
      int spread=(point>0)?(int)MathRound((ask-bid)/point):0;
      double rsi=iRSI(sym,PERIOD_H1,14,PRICE_CLOSE);
      double ma20=iMA(sym,PERIOD_H1,20,0,MODE_EMA,PRICE_CLOSE);
      double ma50=iMA(sym,PERIOD_H1,50,0,MODE_EMA,PRICE_CLOSE);
      double ma200=iMA(sym,PERIOD_H1,200,0,MODE_EMA,PRICE_CLOSE);
      string trend="NEUTRAL";
      if(bid>ma20&&ma20>ma50&&ma50>ma200) trend="BULLISH";
      else if(bid<ma20&&ma20<ma50&&ma50<ma200) trend="BEARISH";
      string rsiSig="NEUTRAL";
      if(rsi<30) rsiSig="OVERSOLD"; else if(rsi>70) rsiSig="OVERBOUGHT";
      double h0=iHigh(sym,PERIOD_H1,0),l0=iLow(sym,PERIOD_H1,0);
      double h2=iHigh(sym,PERIOD_H1,2),l2=iLow(sym,PERIOD_H1,2);
      string fvg="NONE"; double fvgH=0,fvgL=0;
      if(l0>h2){fvg="BULLISH";fvgH=l0;fvgL=h2;}
      else if(h0<l2){fvg="BEARISH";fvgH=l2;fvgL=h0;}
      string obType="NONE"; double obH=0,obL=0;
      for(int b=2;b<10;b++)
        {
         double cb=iClose(sym,PERIOD_H1,b),ob_=iOpen(sym,PERIOD_H1,b);
         double cb1=iClose(sym,PERIOD_H1,b-1),ob1=iOpen(sym,PERIOD_H1,b-1);
         if(cb<ob_&&cb1>ob1&&(cb1-ob1)>(ob_-cb)*1.5){obType="BULLISH_OB";obH=iHigh(sym,PERIOD_H1,b);obL=iLow(sym,PERIOD_H1,b);break;}
         if(cb>ob_&&cb1<ob1&&(ob1-cb1)>(cb-ob_)*1.5){obType="BEARISH_OB";obH=iHigh(sym,PERIOD_H1,b);obL=iLow(sym,PERIOD_H1,b);break;}
        }
      double swH=0,swL=999999;
      for(int b=1;b<=20;b++){double hh=iHigh(sym,PERIOD_H1,b),ll=iLow(sym,PERIOD_H1,b);if(hh>swH)swH=hh;if(ll<swL)swL=ll;}
      double asH=0,asL=999999;
      for(int b=0;b<48;b++){datetime bt=iTime(sym,PERIOD_H1,b);MqlDateTime bd;TimeToStruct(bt,bd);if(bd.hour>=22||bd.hour<7){double bh=iHigh(sym,PERIOD_H1,b),bl=iLow(sym,PERIOD_H1,b);if(bh>asH)asH=bh;if(bl<asL)asL=bl;}}
      if(asL==999999)asL=0;
      string structSig="RANGING";
      if(bid>swH)structSig="BOS_BULLISH";
      else if(bid<swL)structSig="BOS_BEARISH";
      else if(trend=="BULLISH"&&rsi<40)structSig="CHOCH_POTENTIAL_BULL";
      else if(trend=="BEARISH"&&rsi>60)structSig="CHOCH_POTENTIAL_BEAR";
      if(priceLines!="") priceLines+=",\n";
      priceLines+="{\"symbol\":\""+sym+"\"";
      priceLines+=",\"bid\":"+DoubleToString(bid,digits);
      priceLines+=",\"ask\":"+DoubleToString(ask,digits);
      priceLines+=",\"spread\":"+IntegerToString(spread);
      priceLines+=",\"rsi\":"+DoubleToString(rsi,2);
      priceLines+=",\"ma20\":"+DoubleToString(ma20,digits);
      priceLines+=",\"ma50\":"+DoubleToString(ma50,digits);
      priceLines+=",\"ma200\":"+DoubleToString(ma200,digits);
      priceLines+=",\"trend\":\""+trend+"\"";
      priceLines+=",\"rsi_signal\":\""+rsiSig+"\"";
      priceLines+=",\"fvg_type\":\""+fvg+"\"";
      priceLines+=",\"fvg_high\":"+DoubleToString(fvgH,digits);
      priceLines+=",\"fvg_low\":"+DoubleToString(fvgL,digits);
      priceLines+=",\"ob_type\":\""+obType+"\"";
      priceLines+=",\"ob_high\":"+DoubleToString(obH,digits);
      priceLines+=",\"ob_low\":"+DoubleToString(obL,digits);
      priceLines+=",\"swing_high\":"+DoubleToString(swH,digits);
      priceLines+=",\"swing_low\":"+DoubleToString(swL,digits);
      priceLines+=",\"asia_high\":"+DoubleToString(asH,digits);
      priceLines+=",\"asia_low\":"+DoubleToString(asL,digits);
      priceLines+=",\"structure\":\""+structSig+"\"}";
     }
   j+=priceLines+"\n]\n}\n";
   FileWriteString(fh,j);
   FileClose(fh);
   Print("OmniExport v4: updated | ",amd," | ",session);
  }

//+------------------------------------------------------------------+
// COMMAND EXECUTOR
// Command format (pipe-delimited):
// OPEN|SYMBOL|BUY_LIMIT|PRICE|SL|TP|VOLUME|COMMENT
// OPEN|SYMBOL|SELL_LIMIT|PRICE|SL|TP|VOLUME|COMMENT
// OPEN|SYMBOL|BUY|0|SL|TP|VOLUME|COMMENT  (market order)
// CLOSE|TICKET|SYMBOL||||VOLUME|
// MODIFY|TICKET|SYMBOL||SL|TP||
//+------------------------------------------------------------------+
void CheckCommands()
  {
   if(!FileIsExist(CmdFile,FILE_COMMON)) return;
   int fh=FileOpen(CmdFile,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh==INVALID_HANDLE) return;
   string cmd="";
   while(!FileIsEnding(fh)) cmd+=FileReadString(fh);
   FileClose(fh);
   FileDelete(CmdFile,FILE_COMMON);
   if(StringLen(cmd)<5) return;
   Print("OmniExport: received command: ",cmd);
   string result=ProcessCommand(cmd);
   // Write result
   int rf=FileOpen(ResultFile,FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(rf!=INVALID_HANDLE){FileWriteString(rf,result);FileClose(rf);}
  }

string ProcessCommand(string cmd)
  {
   string parts[];
   int n=StringSplit(cmd,'|',parts);
   if(n<2) return "ERROR|bad command format";
   string action=parts[0];

   if(action=="OPEN" && n>=8)
     {
      string sym   =parts[1];
      string type  =parts[2];
      double price =StringToDouble(parts[3]);
      double sl    =StringToDouble(parts[4]);
      double tp    =StringToDouble(parts[5]);
      double vol   =StringToDouble(parts[6]);
      string comment=parts[7];
      ENUM_ORDER_TYPE ot;
      bool isPending=true;
      if(type=="BUY")        {ot=ORDER_TYPE_BUY;      isPending=false;}
      else if(type=="SELL")  {ot=ORDER_TYPE_SELL;     isPending=false;}
      else if(type=="BUY_LIMIT")  ot=ORDER_TYPE_BUY_LIMIT;
      else if(type=="SELL_LIMIT") ot=ORDER_TYPE_SELL_LIMIT;
      else if(type=="BUY_STOP")   ot=ORDER_TYPE_BUY_STOP;
      else if(type=="SELL_STOP")  ot=ORDER_TYPE_SELL_STOP;
      else return "ERROR|unknown order type";
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action   =isPending?TRADE_ACTION_PENDING:TRADE_ACTION_DEAL;
      req.symbol   =sym; req.volume=vol; req.type=ot;
      req.price    =isPending?price:SymbolInfoDouble(sym,ot==ORDER_TYPE_BUY?SYMBOL_ASK:SYMBOL_BID);
      req.sl=sl; req.tp=tp; req.deviation=30;
      req.magic    =MagicNumber; req.comment=comment;
      req.type_filling=ORDER_FILLING_IOC;
      if(OrderSend(req,res))
         return "OK|"+IntegerToString(res.order)+"|"+IntegerToString(res.deal)+"|opened "+type+" "+sym+" vol="+DoubleToString(vol,2)+" price="+DoubleToString(res.price,5);
      else
         return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   if(action=="CLOSE" && n>=3)
     {
      ulong ticket=StringToInteger(parts[1]);
      if(!PositionSelectByTicket(ticket)) return "ERROR|position not found";
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action  =TRADE_ACTION_DEAL;
      req.symbol  =PositionGetString(POSITION_SYMBOL);
      req.volume  =n>=7&&StringLen(parts[6])>0?StringToDouble(parts[6]):PositionGetDouble(POSITION_VOLUME);
      req.type    =PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?ORDER_TYPE_SELL:ORDER_TYPE_BUY;
      req.price   =PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?SymbolInfoDouble(req.symbol,SYMBOL_BID):SymbolInfoDouble(req.symbol,SYMBOL_ASK);
      req.deviation=30; req.magic=MagicNumber; req.comment="OMNI_CLOSE";
      req.type_filling=ORDER_FILLING_IOC;
      if(OrderSend(req,res)) return "OK|closed ticket="+IntegerToString(ticket);
      else return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   if(action=="MODIFY" && n>=6)
     {
      ulong ticket=StringToInteger(parts[1]);
      double newSL=StringToDouble(parts[4]);
      double newTP=StringToDouble(parts[5]);
      if(!PositionSelectByTicket(ticket)) return "ERROR|position not found";
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action=TRADE_ACTION_SLTP; req.symbol=PositionGetString(POSITION_SYMBOL);
      req.sl=newSL; req.tp=newTP; req.position=ticket;
      if(OrderSend(req,res)) return "OK|modified ticket="+IntegerToString(ticket)+" SL="+DoubleToString(newSL,5)+" TP="+DoubleToString(newTP,5);
      else return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   if(action=="CANCEL" && n>=2)
     {
      ulong ticket=StringToInteger(parts[1]);
      MqlTradeRequest req={}; MqlTradeResult res={};
      req.action=TRADE_ACTION_REMOVE; req.order=ticket;
      if(OrderSend(req,res)) return "OK|cancelled order="+IntegerToString(ticket);
      else return "ERROR|"+IntegerToString(res.retcode)+"|"+res.comment;
     }

   return "ERROR|unknown action: "+action;
  }
