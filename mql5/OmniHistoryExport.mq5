//+------------------------------------------------------------------+
//|  OmniHistoryExport.mq5                                           |
//|  One-shot: export real broker XAUUSD/XAGUSD history to CSV so the |
//|  Python sequential backtest can run on ACTUAL MidasFX prices      |
//|  (not yfinance futures). Writes to the MT5 Common\Files folder,   |
//|  alongside omni_data.json.                                        |
//|                                                                   |
//|  USE: MetaEditor -> open -> F7 compile -> in MT5 Navigator drag   |
//|  this Script onto ANY XAUUSD chart. It exports and prints a       |
//|  summary to the Experts log. Run once.                            |
//|                                                                   |
//|  Output files (Common\Files):                                     |
//|     hist_XAUUSD_m5.csv, _m15.csv, _h1.csv, _h4.csv, _d1.csv       |
//|     (and XAGUSD if available)                                     |
//|  CSV columns: time,open,high,low,close,volume                     |
//|     time format: YYYY.MM.DD HH:MM:SS (matches Python loader)      |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs

input string  InpSymbols   = "XAUUSD,XAGUSD";  // comma list; first is priority
input int     InpBarsM5    = 30000;            // ~3.5 months of M5
input int     InpBarsM15   = 20000;            // ~7 months of M15
input int     InpBarsH1    = 12000;            // ~2 years of H1
input int     InpBarsH4    = 4000;             // ~2.5 years of H4
input int     InpBarsD1    = 2000;             // ~8 years of D1

//+------------------------------------------------------------------+
int ExportTF(const string sym, ENUM_TIMEFRAMES tf, const string tag, int want)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int got = CopyRates(sym, tf, 0, want, rates);
   if(got <= 0)
   {
      PrintFormat("  %s %s: CopyRates failed (err=%d). Is the symbol in Market Watch / history loaded?",
                  sym, tag, GetLastError());
      return 0;
   }
   string fname = StringFormat("hist_%s_%s.csv", sym, tag);
   int h = FileOpen(fname, FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON, ',');
   if(h == INVALID_HANDLE)
   {
      PrintFormat("  %s %s: FileOpen failed (err=%d)", sym, tag, GetLastError());
      return 0;
   }
   FileWrite(h, "time", "open", "high", "low", "close", "volume");
   // write oldest -> newest
   for(int i = got - 1; i >= 0; i--)
   {
      string t = TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS); // YYYY.MM.DD HH:MM:SS
      FileWrite(h, t,
                DoubleToString(rates[i].open, _Digits),
                DoubleToString(rates[i].high, _Digits),
                DoubleToString(rates[i].low,  _Digits),
                DoubleToString(rates[i].close,_Digits),
                (long)rates[i].tick_volume);
   }
   FileClose(h);
   PrintFormat("  %s %s: wrote %d bars -> Common\\Files\\%s", sym, tag, got, fname);
   return got;
}

//+------------------------------------------------------------------+
void OnStart()
{
   Print("=== OmniHistoryExport: exporting real broker history ===");
   string syms[];
   int n = StringSplit(InpSymbols, ',', syms);
   for(int s = 0; s < n; s++)
   {
      string sym = syms[s];
      StringTrimLeft(sym); StringTrimRight(sym);
      if(sym == "") continue;
      // Make sure the symbol is selected so history is available.
      if(!SymbolSelect(sym, true))
         PrintFormat("  WARNING: could not select %s in Market Watch", sym);
      Print("Symbol: ", sym, " (digits=", (int)SymbolInfoInteger(sym, SYMBOL_DIGITS), ")");
      ExportTF(sym, PERIOD_M5,  "m5",  InpBarsM5);
      ExportTF(sym, PERIOD_M15, "m15", InpBarsM15);
      ExportTF(sym, PERIOD_H1,  "h1",  InpBarsH1);
      ExportTF(sym, PERIOD_H4,  "h4",  InpBarsH4);
      ExportTF(sym, PERIOD_D1,  "d1",  InpBarsD1);
   }
   Print("=== Done. CSVs are in the MT5 Common\\Files folder. ===");
   Print("    macOS path: ~/Library/Application Support/net.metaquotes.wine.metatrader5/");
   Print("    drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files/");
}
//+------------------------------------------------------------------+
