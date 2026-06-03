//+------------------------------------------------------------------+
//|                                       StructureDetector.mqh       |
//|                                          OMNI ICT Production Bot   |
//|                                          v28.0 — CHoCH / BOS       |
//+------------------------------------------------------------------+
#property strict

// ── Swing point struct (fractal high/low) ──────────────────────────
struct SwingPoint {
   double price;
   datetime time;
   int      bar_idx;
   bool     is_high;
};

// ── Structure state ────────────────────────────────────────────────
enum ENUM_TREND { TREND_UP, TREND_DOWN, TREND_RANGE };

struct MarketStructure {
   ENUM_TREND trend;
   double     last_swing_high;
   double     last_swing_low;
   datetime   last_swing_high_time;
   datetime   last_swing_low_time;
   datetime   last_choch_time;
   datetime   last_bos_time;
   string     last_choch_dir;
   string     last_bos_dir;
   double     last_hh;
   double     last_ll;
   bool       valid;
};

SwingPoint g_swings[];
MarketStructure g_structure;

input group "=== Structure Settings ==="
input int INP_FractalWindow = 2;
input int INP_MinSwingBars  = 3;
input int INP_MaxSwingHistory = 50;

void StructureDetector_Init() {
   ArrayResize(g_swings, 0);
   ZeroMemory(g_structure);
   g_structure.trend = TREND_RANGE;
   g_structure.last_choch_dir = "none";
   g_structure.last_bos_dir = "none";
   g_structure.valid = false;
}

void StructureDetector_UpdateSwings() {
   int max_lookback = INP_FractalWindow * 3 + INP_MinSwingBars;
   if(max_lookback < 10) max_lookback = 10;
   for(int i = max_lookback; i >= INP_FractalWindow + 1; i--) {
      int mid = i;
      int left2 = i + INP_FractalWindow;
      int right2 = i - INP_FractalWindow;
      datetime t = iTime(Symbol(), PERIOD_M1, mid);
      bool already = false;
      for(int s = 0; s < ArraySize(g_swings); s++) {
         if(g_swings[s].time == t) { already = true; break; }
      }
      if(already) continue;
      double mid_high = iHigh(Symbol(), PERIOD_M1, mid);
      double mid_low  = iLow(Symbol(), PERIOD_M1, mid);
      bool is_fractal_high = true;
      bool is_fractal_low  = true;
      for(int j = right2; j <= left2; j++) {
         if(j == mid) continue;
         if(iHigh(Symbol(), PERIOD_M1, j) > mid_high) is_fractal_high = false;
         if(iLow(Symbol(),  PERIOD_M1, j) < mid_low)  is_fractal_low  = false;
         if(!is_fractal_high && !is_fractal_low) break;
      }
      if(is_fractal_high) {
         int sz = ArraySize(g_swings);
         ArrayResize(g_swings, sz + 1);
         g_swings[sz].price = mid_high;
         g_swings[sz].time = t;
         g_swings[sz].bar_idx = mid;
         g_swings[sz].is_high = true;
      } else if(is_fractal_low) {
         int sz = ArraySize(g_swings);
         ArrayResize(g_swings, sz + 1);
         g_swings[sz].price = mid_low;
         g_swings[sz].time = t;
         g_swings[sz].bar_idx = mid;
         g_swings[sz].is_high = false;
      }
   }
   int total = ArraySize(g_swings);
   if(total > INP_MaxSwingHistory) {
      int remove = total - INP_MaxSwingHistory;
      for(int i = 0; i < INP_MaxSwingHistory; i++) g_swings[i] = g_swings[i + remove];
      ArrayResize(g_swings, INP_MaxSwingHistory);
   }
}

void StructureDetector_Analyze() {
   int n = ArraySize(g_swings);
   if(n < 4) { g_structure.valid = false; return; }
   double last_hh = 0, prev_hh = 0;
   double last_ll = 0, prev_ll = 0;
   datetime last_hh_t = 0, prev_hh_t = 0;
   datetime last_ll_t = 0, prev_ll_t = 0;
   int hh_count = 0, ll_count = 0;
   for(int i = n - 1; i >= 0; i--) {
      if(g_swings[i].is_high) {
         if(hh_count == 0) { last_hh = g_swings[i].price; last_hh_t = g_swings[i].time; hh_count++; }
         else if(hh_count == 1) { prev_hh = g_swings[i].price; prev_hh_t = g_swings[i].time; hh_count++; break; }
      }
   }
   for(int i = n - 1; i >= 0; i--) {
      if(!g_swings[i].is_high) {
         if(ll_count == 0) { last_ll = g_swings[i].price; last_ll_t = g_swings[i].time; ll_count++; }
         else if(ll_count == 1) { prev_ll = g_swings[i].price; prev_ll_t = g_swings[i].time; ll_count++; break; }
      }
   }
   if(hh_count < 2 || ll_count < 2) { g_structure.valid = false; return; }
   g_structure.last_swing_high = last_hh; g_structure.last_swing_high_time = last_hh_t;
   g_structure.last_swing_low  = last_ll; g_structure.last_swing_low_time  = last_ll_t;
   if(last_hh > prev_hh && last_ll > prev_ll) g_structure.trend = TREND_UP;
   else if(last_hh < prev_hh && last_ll < prev_ll) g_structure.trend = TREND_DOWN;
   else g_structure.trend = TREND_RANGE;
   double recent_close = iClose(Symbol(), PERIOD_M1, 1);
   datetime now = TimeCurrent();
   if(g_structure.trend == TREND_UP && last_hh > prev_hh) {
      g_structure.last_bos_dir = "bullish"; g_structure.last_bos_time = last_hh_t; g_structure.last_hh = last_hh;
   } else if(g_structure.trend == TREND_DOWN && last_ll < prev_ll) {
      g_structure.last_bos_dir = "bearish"; g_structure.last_bos_time = last_ll_t; g_structure.last_ll = last_ll;
   }
   if(g_structure.trend == TREND_UP) {
      double lowest_low = MathMin(prev_ll, last_ll);
      if(recent_close < lowest_low) { g_structure.last_choch_dir = "bearish"; g_structure.last_choch_time = now; g_structure.trend = TREND_DOWN; }
   }
   if(g_structure.trend == TREND_DOWN) {
      double highest_high = MathMax(prev_hh, last_hh);
      if(recent_close > highest_high) { g_structure.last_choch_dir = "bullish"; g_structure.last_choch_time = now; g_structure.trend = TREND_UP; }
   }
   g_structure.valid = true;
}

void StructureDetector_OnTick() {
   StructureDetector_UpdateSwings();
   StructureDetector_Analyze();
}

string StructureDetector_ExportJSON() {
   string trend_str = "ranging";
   if(g_structure.trend == TREND_UP) trend_str = "up";
   if(g_structure.trend == TREND_DOWN) trend_str = "down";
   string s = "\"structure\":";
   s += "{";
     s += "\"trend\":\"" + trend_str + "\",";
     s += "\"last_swing_high\":" + (g_structure.last_swing_high > 0 ? DoubleToString(g_structure.last_swing_high, Digits()) : "null") + ",";
     s += "\"last_swing_low\":"  + (g_structure.last_swing_low  > 0 ? DoubleToString(g_structure.last_swing_low,  Digits()) : "null") + ",";
     s += "\"last_choch_dir\":\"" + g_structure.last_choch_dir + "\",";
     s += "\"last_choch_time\":" + (g_structure.last_choch_time > 0 ? "\"" + TimeToString(g_structure.last_choch_time, TIME_DATE|TIME_SECONDS) + "\"" : "null") + ",";
     s += "\"last_bos_dir\":\"" + g_structure.last_bos_dir + "\",";
     s += "\"last_bos_time\":" + (g_structure.last_bos_time > 0 ? "\"" + TimeToString(g_structure.last_bos_time, TIME_DATE|TIME_SECONDS) + "\"" : "null") + ",";
     s += "\"last_hh\":" + (g_structure.last_hh > 0 ? DoubleToString(g_structure.last_hh, Digits()) : "null") + ",";
     s += "\"last_ll\":" + (g_structure.last_ll > 0 ? DoubleToString(g_structure.last_ll, Digits()) : "null") + ",";
     s += "\"valid\":" + (g_structure.valid ? "true" : "false");
   s += "}";
   return s;
}

bool StructureDetector_IsTrendUp()   { return g_structure.trend == TREND_UP; }
bool StructureDetector_IsTrendDown() { return g_structure.trend == TREND_DOWN; }
bool StructureDetector_IsRange()     { return g_structure.trend == TREND_RANGE; }
bool StructureDetector_HasRecentChoCH(int max_seconds) {
   if(g_structure.last_choch_time <= 0) return false;
   return (TimeCurrent() - g_structure.last_choch_time) <= max_seconds;
}
bool StructureDetector_HasRecentBOS(int max_seconds) {
   if(g_structure.last_bos_time <= 0) return false;
   return (TimeCurrent() - g_structure.last_bos_time) <= max_seconds;
}
MarketStructure StructureDetector_GetState() { return g_structure; }
