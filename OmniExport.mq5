//+------------------------------------------------------------------+
//|  OmniExport.mq5 — exports account, positions & history to JSON  |
//|  Drop into: MetaTrader 5 / MQL5 / Experts /                     |
//+------------------------------------------------------------------+
#property copyright "OMNI Trading Dashboard"
#property version   "2.00"
#property strict

input int    UpdateSeconds = 3;       // How often to refresh (seconds)
input string FileName      = "omni_data.json";  // Output file name

int    OnInit()   { EventSetTimer(UpdateSeconds); return(INIT_SUCCEEDED); }
void   OnDeinit(const int r) { EventKillTimer(); }
void   OnTimer()  { ExportData(); }
void   OnTick()   {}

//+------------------------------------------------------------------+
void ExportData()
  {
   int fh = FileOpen(FileName, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) { Print("OmniExport: cannot open file"); return; }

   string j = "{\n";

   // ── Timestamp ────────────────────────────────────────────────────
   j += "  \"timestamp\": \"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",\n";

   // ── Session / AMD Phase (derived from GMT hour) ───────────────────
   MqlDateTime tm;
   TimeToStruct(TimeGMT(), tm);
   int gmt_hour = tm.hour;
   string session_name = "CLOSED";
   if(gmt_hour >= 22 || gmt_hour < 7)  session_name = "ASIA";
   else if(gmt_hour >= 7  && gmt_hour < 12) session_name = "LONDON";
   else if(gmt_hour >= 12 && gmt_hour < 17) session_name = "NEW_YORK";
   else if(gmt_hour >= 17)                  session_name = "NY_CLOSE";
   string amd_name = "ACCUMULATION";
   if(gmt_hour >= 7  && gmt_hour < 12) amd_name = "MANIPULATION";
   else if(gmt_hour >= 12)              amd_name = "DISTRIBUTION";
   j += "  \"session\": \""   + session_name + "\",\n";
   j += "  \"amd_phase\": \"" + amd_name    + "\",\n";
   j += "  \"gmt_time\": \""  + TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS) + "\",\n";

   // ── Account ──────────────────────────────────────────────────────
   j += "  \"account\": {\n";
   j += "    \"login\":"    + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))    + ",\n";
   j += "    \"name\":\""   + AccountInfoString(ACCOUNT_NAME)                       + "\",\n";
   j += "    \"server\":\""  + AccountInfoString(ACCOUNT_SERVER)                    + "\",\n";
   j += "    \"currency\":\""+ AccountInfoString(ACCOUNT_CURRENCY)                  + "\",\n";
   j += "    \"balance\":"   + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2) + ",\n";
   j += "    \"equity\":"    + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2)  + ",\n";
   j += "    \"margin\":"    + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2)  + ",\n";
   j += "    \"free_margin\":"+ DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2) + ",\n";
   j += "    \"margin_level\":"+ DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2) + ",\n";
   j += "    \"profit\":"    + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT),2)  + ",\n";
   j += "    \"leverage\":"  + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + "\n";
   j += "  },\n";

   // ── Open Positions ────────────────────────────────────────────────
   j += "  \"positions\": [\n";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string sep = (i < total-1) ? "," : "";
      j += "    {\n";
      j += "      \"ticket\":"       + IntegerToString(ticket)                                           + ",\n";
      j += "      \"symbol\":\""     + PositionGetString(POSITION_SYMBOL)                               + "\",\n";
      j += "      \"type\":\""       + (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL") + "\",\n";
      j += "      \"volume\":"       + DoubleToString(PositionGetDouble(POSITION_VOLUME),2)             + ",\n";
      j += "      \"open_price\":"   + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),5)         + ",\n";
      j += "      \"current_price\":"+ DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5)      + ",\n";
      j += "      \"sl\":"           + DoubleToString(PositionGetDouble(POSITION_SL),5)                 + ",\n";
      j += "      \"tp\":"           + DoubleToString(PositionGetDouble(POSITION_TP),5)                 + ",\n";
      j += "      \"profit\":"       + DoubleToString(PositionGetDouble(POSITION_PROFIT),2)             + ",\n";
      j += "      \"swap\":"         + DoubleToString(PositionGetDouble(POSITION_SWAP),2)               + ",\n";
      j += "      \"magic\":"        + IntegerToString(PositionGetInteger(POSITION_MAGIC))              + ",\n";
      j += "      \"time\":\""       + TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE|TIME_SECONDS) + "\"\n";
      j += "    }" + sep + "\n";
     }
   j += "  ],\n";

   // ── Trade History (last 90 days) ──────────────────────────────────
   j += "  \"history\": [\n";
   datetime from = TimeCurrent() - 90*86400;
   datetime to   = TimeCurrent();
   HistorySelect(from, to);
   int deals = HistoryDealsTotal();
   int written = 0;
   string histLines = "";
   for(int i = 0; i < deals; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      long dtype = HistoryDealGetInteger(ticket, DEAL_TYPE);
      long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL) continue;

      if(written > 0) histLines += ",\n";
      histLines += "    {\n";
      histLines += "      \"ticket\":"      + IntegerToString(ticket)                              + ",\n";
      histLines += "      \"time\":\""      + TimeToString((datetime)HistoryDealGetInteger(ticket,DEAL_TIME),TIME_DATE|TIME_SECONDS) + "\",\n";
      histLines += "      \"symbol\":\""    + HistoryDealGetString(ticket,DEAL_SYMBOL)             + "\",\n";
      histLines += "      \"type\":\""      + (dtype==DEAL_TYPE_BUY?"BUY":"SELL")                 + "\",\n";
      histLines += "      \"entry\":\""     + (entry==DEAL_ENTRY_IN?"IN":(entry==DEAL_ENTRY_OUT?"OUT":"INOUT")) + "\",\n";
      histLines += "      \"volume\":"      + DoubleToString(HistoryDealGetDouble(ticket,DEAL_VOLUME),2)     + ",\n";
      histLines += "      \"price\":"       + DoubleToString(HistoryDealGetDouble(ticket,DEAL_PRICE),5)      + ",\n";
      histLines += "      \"profit\":"      + DoubleToString(HistoryDealGetDouble(ticket,DEAL_PROFIT),2)     + ",\n";
      histLines += "      \"swap\":"        + DoubleToString(HistoryDealGetDouble(ticket,DEAL_SWAP),2)       + ",\n";
      histLines += "      \"commission\":"  + DoubleToString(HistoryDealGetDouble(ticket,DEAL_COMMISSION),2) + "\n";
      histLines += "    }";
      written++;
     }
   j += histLines + "\n";
   j += "  ],\n";

   // ── Watchlist prices ──────────────────────────────────────────────
   string symbols[] = {"EURUSD","GBPUSD","XAUUSD","US30","BTCUSD","NAS100","USDJPY","GBPJPY"};
   j += "  \"prices\": [\n";
   int symCount = ArraySize(symbols);
   for(int i = 0; i < symCount; i++)
     {
      string sym = symbols[i];
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      int spread = (point > 0) ? (int)MathRound((ask-bid)/point) : 0;
      if(bid == 0) continue;
      string sep = (i < symCount-1) ? "," : "";
      j += "    {\"symbol\":\"" + sym + "\",\"bid\":"
           + DoubleToString(bid,digits) + ",\"ask\":"
           + DoubleToString(ask,digits) + ",\"spread\":"
           + IntegerToString(spread) + "}" + sep + "\n";
     }
   j += "  ],\n";

   // ── Charts: OHLC bars + key levels + contract spec per symbol ──────
   string csyms[]  = {"XAUUSD","XAGUSD","EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD"};
   string tfNames[] = {"D1","H4","H1","M15","M5"};
   ENUM_TIMEFRAMES tfEnums[] = {PERIOD_D1, PERIOD_H4, PERIOD_H1, PERIOD_M15, PERIOD_M5};
   int tfBars[] = {20, 30, 48, 48, 24};
   int csymCount = ArraySize(csyms);
   int tfCount   = ArraySize(tfNames);

   j += "  \"charts\": {\n";
   int csWritten = 0;
   for(int si = 0; si < csymCount; si++)
     {
      string csym = csyms[si];
      if(!SymbolSelect(csym, true)) continue;
      double cbid = SymbolInfoDouble(csym, SYMBOL_BID);
      if(cbid == 0) continue;
      int cdig = (int)SymbolInfoInteger(csym, SYMBOL_DIGITS);

      if(csWritten > 0) j += ",\n";
      j += "    \"" + csym + "\": {\n";
      csWritten++;

      // ── OHLC bars per timeframe (newest bar first = index 0) ────────
      for(int ti = 0; ti < tfCount; ti++)
        {
         MqlRates rates[];
         int copied = CopyRates(csym, tfEnums[ti], 1, tfBars[ti], rates);
         j += "      \"" + tfNames[ti] + "\": [\n";
         if(copied > 0)
           {
            for(int bi = 0; bi < copied; bi++)
              {
               string bsep = (bi < copied-1) ? "," : "";
               j += "        {\"t\":\"" + TimeToString(rates[bi].time, TIME_DATE|TIME_SECONDS)
                    + "\",\"o\":" + DoubleToString(rates[bi].open,  cdig)
                    + ",\"h\":"   + DoubleToString(rates[bi].high,  cdig)
                    + ",\"l\":"   + DoubleToString(rates[bi].low,   cdig)
                    + ",\"c\":"   + DoubleToString(rates[bi].close, cdig)
                    + ",\"v\":"   + IntegerToString(rates[bi].tick_volume) + "}" + bsep + "\n";
              }
           }
         j += "      ],\n";  // Comma always present — key levels follow
        }

      // ── Key levels (previous day / week / month H & L) ──────────────
      j += "      \"pdh\":"  + DoubleToString(iHigh(csym, PERIOD_D1,  1), cdig) + ",\n";
      j += "      \"pdl\":"  + DoubleToString(iLow(csym,  PERIOD_D1,  1), cdig) + ",\n";
      j += "      \"pwh\":"  + DoubleToString(iHigh(csym, PERIOD_W1,  1), cdig) + ",\n";
      j += "      \"pwl\":"  + DoubleToString(iLow(csym,  PERIOD_W1,  1), cdig) + ",\n";
      j += "      \"pmh\":"  + DoubleToString(iHigh(csym, PERIOD_MN1, 1), cdig) + ",\n";
      j += "      \"pml\":"  + DoubleToString(iLow(csym,  PERIOD_MN1, 1), cdig) + ",\n";

      // ── Live bid/ask + spread (for spread guard in Python) ──────────────
      double cbid2 = SymbolInfoDouble(csym, SYMBOL_BID);
      double cask2 = SymbolInfoDouble(csym, SYMBOL_ASK);
      double cpt   = SymbolInfoDouble(csym, SYMBOL_POINT);
      int    csprd = (cpt > 0) ? (int)MathRound((cask2-cbid2)/cpt) : 0;
      j += "      \"bid\":"    + DoubleToString(cbid2, cdig) + ",\n";
      j += "      \"ask\":"    + DoubleToString(cask2, cdig) + ",\n";
      j += "      \"spread\":" + IntegerToString(csprd)      + ",\n";

      // ── New Day / New Week Opening Gaps (NDOG / NWOG) ───────────────────
      // NDOG = close of last D1 bar vs open of current D1 bar (gap between sessions)
      // NWOG = close of last W1 bar vs open of current W1 bar
      MqlRates dRates[2], wRates[2];
      double ndog_open  = 0, ndog_close = 0;
      double nwog_open  = 0, nwog_close = 0;
      if(CopyRates(csym, PERIOD_D1, 0, 2, dRates) == 2)
        {
         ndog_close = dRates[1].close;  // yesterday close
         ndog_open  = dRates[0].open;   // today open
        }
      if(CopyRates(csym, PERIOD_W1, 0, 2, wRates) == 2)
        {
         nwog_close = wRates[1].close;  // last week close
         nwog_open  = wRates[0].open;   // this week open
        }
      j += "      \"ndog_close\":" + DoubleToString(ndog_close, cdig) + ",\n";
      j += "      \"ndog_open\":"  + DoubleToString(ndog_open,  cdig) + ",\n";
      j += "      \"nwog_close\":" + DoubleToString(nwog_close, cdig) + ",\n";
      j += "      \"nwog_open\":"  + DoubleToString(nwog_open,  cdig) + ",\n";

      // ── Contract spec (lot sizing in auto_trader) ────────────────────
      j += "      \"tick_size\":"  + DoubleToString(SymbolInfoDouble(csym, SYMBOL_TRADE_TICK_SIZE),  8) + ",\n";
      j += "      \"tick_value\":" + DoubleToString(SymbolInfoDouble(csym, SYMBOL_TRADE_TICK_VALUE), 8) + ",\n";
      j += "      \"point\":"      + DoubleToString(SymbolInfoDouble(csym, SYMBOL_POINT),           8) + ",\n";
      j += "      \"min_lot\":"    + DoubleToString(SymbolInfoDouble(csym, SYMBOL_VOLUME_MIN),      2) + ",\n";
      j += "      \"max_lot\":"    + DoubleToString(SymbolInfoDouble(csym, SYMBOL_VOLUME_MAX),      2) + ",\n";
      j += "      \"lot_step\":"   + DoubleToString(SymbolInfoDouble(csym, SYMBOL_VOLUME_STEP),     2) + "\n";

      j += "    }";
     }
   j += "\n  }\n";
   j += "}\n";

   FileWriteString(fh, j);
   FileClose(fh);
  }
//+------------------------------------------------------------------+
