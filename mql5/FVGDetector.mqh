//+------------------------------------------------------------------+
//|                                           FVGDetector.mqh          |
//|                                          OMNI ICT Production Bot   |
//|                                          v28.0 — Fair Value Gaps   |
//+------------------------------------------------------------------+
#property strict

// ── FVG struct ─────────────────────────────────────────────────────
struct FVG {
   string   direction;
   double   top;
   double   bottom;
   double   size_pips;
   datetime time;
   int      middle_bar_idx;
   bool     mitigated;
   datetime mitigated_time;
   double   optimal_entry;
};

FVG g_fvgs[];
datetime g_last_fvg_scan = 0;

input group "=== FVG Settings ==="
input int INP_FVG_MaxBarsBack = 10;
input int INP_FVG_MaxAgeMinutes = 60;

void FVGDetector_Init() {
   ArrayResize(g_fvgs, 0);
   g_last_fvg_scan = 0;
}

void FVGDetector_ScanNew() {
   datetime last_completed = iTime(Symbol(), PERIOD_M1, 1);
   if(last_completed <= g_last_fvg_scan) return;
   g_last_fvg_scan = last_completed;
   for(int i = INP_FVG_MaxBarsBack + 2; i >= 3; i--) {
      double h2 = iHigh(Symbol(), PERIOD_M1, i);
      double l2 = iLow(Symbol(), PERIOD_M1, i);
      double h1 = iHigh(Symbol(), PERIOD_M1, i-1);
      double l1 = iLow(Symbol(), PERIOD_M1, i-1);
      // Bullish FVG
      if(l1 > h2) {
         FVG f; f.direction = "bullish";
         f.bottom = h2; f.top = l1;
         f.size_pips = (l1 - h2) / SymbolInfoDouble(Symbol(), SYMBOL_POINT) / 10.0;
         f.time = iTime(Symbol(), PERIOD_M1, i-1);
         f.middle_bar_idx = i-1;
         f.mitigated = false; f.mitigated_time = 0;
         f.optimal_entry = (f.bottom + f.top) / 2.0;
         FVGDetector_AddFVG(f);
      }
      // Bearish FVG
      if(h1 < l2) {
         FVG f; f.direction = "bearish";
         f.top = l2; f.bottom = h1;
         f.size_pips = (l2 - h1) / SymbolInfoDouble(Symbol(), SYMBOL_POINT) / 10.0;
         f.time = iTime(Symbol(), PERIOD_M1, i-1);
         f.middle_bar_idx = i-1;
         f.mitigated = false; f.mitigated_time = 0;
         f.optimal_entry = (f.top + f.bottom) / 2.0;
         FVGDetector_AddFVG(f);
      }
   }
}

void FVGDetector_AddFVG(FVG &f) {
   for(int i = 0; i < ArraySize(g_fvgs); i++) {
      if(g_fvgs[i].time == f.time && g_fvgs[i].direction == f.direction) return;
   }
   int sz = ArraySize(g_fvgs);
   ArrayResize(g_fvgs, sz + 1);
   g_fvgs[sz] = f;
}

void FVGDetector_CheckMitigation() {
   double bid = SymbolInfoDouble(Symbol(), SYMBOL_BID);
   double ask = SymbolInfoDouble(Symbol(), SYMBOL_ASK);
   datetime now = TimeCurrent();
   for(int i = 0; i < ArraySize(g_fvgs); i++) {
      if(g_fvgs[i].mitigated) continue;
      if(now - g_fvgs[i].time > INP_FVG_MaxAgeMinutes * 60) {
         g_fvgs[i].mitigated = true;
         g_fvgs[i].mitigated_time = now;
         continue;
      }
      if(g_fvgs[i].direction == "bullish") {
         if(bid <= g_fvgs[i].top && bid >= g_fvgs[i].bottom) {
            g_fvgs[i].mitigated = true;
            g_fvgs[i].mitigated_time = now;
         }
      } else {
         if(ask >= g_fvgs[i].bottom && ask <= g_fvgs[i].top) {
            g_fvgs[i].mitigated = true;
            g_fvgs[i].mitigated_time = now;
         }
      }
   }
}

void FVGDetector_Prune() {
   datetime now = TimeCurrent();
   int keep = 0;
   for(int i = 0; i < ArraySize(g_fvgs); i++) {
      bool keep_it = !g_fvgs[i].mitigated || (now - g_fvgs[i].mitigated_time < 1800);
      if(keep_it) { if(i != keep) g_fvgs[keep] = g_fvgs[i]; keep++; }
   }
   ArrayResize(g_fvgs, keep);
}

void FVGDetector_OnTick() {
   FVGDetector_ScanNew();
   FVGDetector_CheckMitigation();
   FVGDetector_Prune();
}

string FVGDetector_ExportJSON() {
   string s = "\"fvgs\":";
   s += "[";
   int unmit_count = 0;
   for(int i = 0; i < ArraySize(g_fvgs); i++) {
      if(g_fvgs[i].mitigated) continue;
      if(unmit_count > 0) s += ",";
      FVG f = g_fvgs[i];
      s += "{\"direction\":\"" + f.direction + "\",\"top\":" + DoubleToString(f.top, Digits()) + ",\"bottom\":" + DoubleToString(f.bottom, Digits()) + ",\"size_pips\":" + DoubleToString(f.size_pips, 1) + ",\"time\":\"" + TimeToString(f.time, TIME_DATE|TIME_SECONDS) + "\",\"mitigated\":false,\"optimal_entry\":" + DoubleToString(f.optimal_entry, Digits()) + "}";
      unmit_count++;
   }
   s += "]";
   return s;
}

bool FVGDetector_GetNearestUnmitigated(string dir_filter, double price, double &midpoint, double &top, double &bottom) {
   double best_dist = 999999;
   int best_idx = -1;
   datetime now = TimeCurrent();
   for(int i = 0; i < ArraySize(g_fvgs); i++) {
      if(g_fvgs[i].mitigated) continue;
      if(g_fvgs[i].direction != dir_filter) continue;
      if(now - g_fvgs[i].time > INP_FVG_MaxAgeMinutes * 60) continue;
      double dist = MathAbs(g_fvgs[i].optimal_entry - price);
      if(dist < best_dist) { best_dist = dist; best_idx = i; }
   }
   if(best_idx >= 0) {
      midpoint = g_fvgs[best_idx].optimal_entry;
      top      = g_fvgs[best_idx].top;
      bottom   = g_fvgs[best_idx].bottom;
      return true;
   }
   return false;
}

int FVGDetector_CountUnmitigated(string dir_filter) {
   int c = 0;
   for(int i = 0; i < ArraySize(g_fvgs); i++) {
      if(!g_fvgs[i].mitigated && g_fvgs[i].direction == dir_filter) c++;
   }
   return c;
}
