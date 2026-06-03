//+------------------------------------------------------------------+
//|                                          SweepDetector.mqh         |
//|                                          OMNI ICT Production Bot   |
//|                                          v28.0 — Real-time Sweeps  |
//+------------------------------------------------------------------+
#property strict

// Include session tracker for range data
#include "SessionTracker.mqh"

// ── Sweep event struct ──────────────────────────────────────────────
struct SweepEvent {
   string   type;          // "bullish" or "bearish"
   double   level;         // The liquidity level that was swept
   string   level_type;    // "asian_high","asian_low","pdh","pdl","equal_high","equal_low","other"
   datetime time;            // When detected
   double   wick_extreme;  // The furthest wick price reached
   double   body_close;    // Candle body close price
   bool     confirmed;     // True if body closed back inside
   double   volume_ratio;  // Volume vs prior 20-bar average (if available)
   bool     multi_touch;   // Price tested this level 2+ times before breaking
   int      touch_count;   // How many times tested
};

// ── Module state ───────────────────────────────────────────────────
SweepEvent g_recent_sweeps[];       // Rolling history (keep last 10)
datetime   g_sweep_expiry_seconds = 600; // Sweeps valid for 10 minutes

// ── Volume tracking (optional, XAUUSD often has limited volume) ──────
double g_avg_volume_20 = 0;
datetime g_last_volume_update = 0;

// ── Touch tracking for multi-touch detection ───────────────────────
struct LevelTouch {
   double level;
   int    touches;
   datetime first_touch;
   datetime last_touch;
   string level_type;
};
LevelTouch g_level_touches[];

// ── Initialize ────────────────────────────────────────────────────
void SweepDetector_Init() {
   ArrayResize(g_recent_sweeps, 0);
   ArrayResize(g_level_touches, 0);
   g_avg_volume_20 = 0;
   g_last_volume_update = 0;
}

// ── Update moving average volume (if volume data present) ───────────
void SweepDetector_UpdateVolume() {
   if(iVolume(Symbol(), PERIOD_M1, 0) <= 0) return; // No volume data

   datetime now = TimeCurrent();
   if(now - g_last_volume_update < 60) return; // Update once per minute
   g_last_volume_update = now;

   double total = 0;
   int cnt = 0;
   for(int i = 1; i <= 20; i++) {
      double v = (double)iVolume(Symbol(), PERIOD_M1, i);
      if(v > 0) { total += v; cnt++; }
   }
   if(cnt > 0) g_avg_volume_20 = total / cnt;
}

// ── Track potential touches on key levels (H1 bars) ─────────────────
void SweepDetector_TrackTouches() {
   datetime now = TimeCurrent();
   double h = iHigh(Symbol(), PERIOD_H1, 1); // Previous completed H1 bar
   double l = iLow(Symbol(), PERIOD_H1, 1);

   double levels[];
   string level_types[];
   int level_count = 0;

   PriorLiquidity prior = SessionTracker_GetPrior();
   SessionRange asian = SessionTracker_GetAsian();
   SessionRange london = SessionTracker_GetLondon();

   if(asian.high > 0)  { ArrayResize(levels, level_count+1); ArrayResize(level_types, level_count+1); levels[level_count] = asian.high;  level_types[level_count] = "asian_high"; level_count++; }
   if(asian.low > 0)   { ArrayResize(levels, level_count+1); ArrayResize(level_types, level_count+1); levels[level_count] = asian.low;   level_types[level_count] = "asian_low"; level_count++; }
   if(prior.pdh > 0)   { ArrayResize(levels, level_count+1); ArrayResize(level_types, level_count+1); levels[level_count] = prior.pdh;   level_types[level_count] = "pdh"; level_count++; }
   if(prior.pdl > 0)   { ArrayResize(levels, level_count+1); ArrayResize(level_types, level_count+1); levels[level_count] = prior.pdl;   level_types[level_count] = "pdl"; level_count++; }

   for(int i = 0; i < ArraySize(prior.equal_highs); i++) {
      ArrayResize(levels, level_count+1); ArrayResize(level_types, level_count+1);
      levels[level_count] = prior.equal_highs[i]; level_types[level_count] = "equal_high"; level_count++;
   }
   for(int i = 0; i < ArraySize(prior.equal_lows); i++) {
      ArrayResize(levels, level_count+1); ArrayResize(level_types, level_count+1);
      levels[level_count] = prior.equal_lows[i]; level_types[level_count] = "equal_low"; level_count++;
   }

   for(int j = 0; j < level_count; j++) {
      bool touched = false;
      double lvl = levels[j];
      double tol = lvl * 0.001;
      if(MathAbs(h - lvl) < tol || MathAbs(l - lvl) < tol || (h > lvl && l < lvl)) touched = true;

      if(touched) {
         bool found = false;
         for(int k = 0; k < ArraySize(g_level_touches); k++) {
            if(MathAbs(g_level_touches[k].level - lvl) < tol && g_level_touches[k].level_type == level_types[j]) {
               g_level_touches[k].touches++;
               g_level_touches[k].last_touch = now;
               found = true;
               break;
            }
         }
         if(!found) {
            int sz = ArraySize(g_level_touches);
            ArrayResize(g_level_touches, sz + 1);
            g_level_touches[sz].level = lvl;
            g_level_touches[sz].touches = 1;
            g_level_touches[sz].first_touch = now;
            g_level_touches[sz].last_touch = now;
            g_level_touches[sz].level_type = level_types[j];
         }
      }
   }

   int keep = 0;
   for(int k = 0; k < ArraySize(g_level_touches); k++) {
      if(now - g_level_touches[k].last_touch < 14400) {
         if(k != keep) g_level_touches[keep] = g_level_touches[k];
         keep++;
      }
   }
   ArrayResize(g_level_touches, keep);
}

// ── Main sweep detection ──────────────────────────────────────────
void SweepDetector_OnTick() {
   SweepDetector_UpdateVolume();
   SweepDetector_TrackTouches();

   datetime now = TimeCurrent();
   double candle_o = iOpen(Symbol(), PERIOD_M1, 1);
   double candle_h = iHigh(Symbol(), PERIOD_M1, 1);
   double candle_l = iLow(Symbol(), PERIOD_M1, 1);
   double candle_c = iClose(Symbol(), PERIOD_M1, 1);
   datetime candle_time = iTime(Symbol(), PERIOD_M1, 1);

   if(candle_time <= g_last_session_boundary) return;

   CheckBearishSweep(candle_o, candle_h, candle_l, candle_c, candle_time);
   CheckBullishSweep(candle_o, candle_h, candle_l, candle_c, candle_time);
}

// ── Check bearish sweep (bull trap: sweep high, close lower) ────────
void CheckBearishSweep(double o, double h, double l, double c, datetime t) {
   PriorLiquidity prior = SessionTracker_GetPrior();
   SessionRange asian = SessionTracker_GetAsian();
   SessionRange london = SessionTracker_GetLondon();

   double levels[]; string level_types[]; int lc = 0;
   if(asian.high > 0)  { ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = asian.high;  level_types[lc] = "asian_high"; lc++; }
   if(prior.pdh > 0)   { ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = prior.pdh;   level_types[lc] = "pdh"; lc++; }
   if(london.high > 0 && SessionTracker_IsSession(SESSION_NY)) {
      ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = london.high; level_types[lc] = "london_high"; lc++;
   }
   for(int i = 0; i < ArraySize(prior.equal_highs); i++) {
      ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = prior.equal_highs[i]; level_types[lc] = "equal_high"; lc++;
   }

   for(int i = 0; i < lc; i++) {
      double lvl = levels[i];
      if(h > lvl && c < lvl) {
         SweepEvent se; se.type = "bearish"; se.level = lvl; se.level_type = level_types[i];
         se.time = t; se.wick_extreme = h; se.body_close = c; se.confirmed = true;

         double vol = (double)iVolume(Symbol(), PERIOD_M1, 1);
         if(g_avg_volume_20 > 0 && vol > 0) se.volume_ratio = vol / g_avg_volume_20; else se.volume_ratio = 1.0;

         se.multi_touch = false; se.touch_count = 1;
         for(int k = 0; k < ArraySize(g_level_touches); k++) {
            if(MathAbs(g_level_touches[k].level - lvl) < lvl * 0.001 && g_level_touches[k].level_type == se.level_type) {
               se.multi_touch = (g_level_touches[k].touches >= 2); se.touch_count = g_level_touches[k].touches; break;
            }
         }
         SweepDetector_RecordSweep(se);
      }
   }
}

// ── Check bullish sweep (bear trap: sweep low, close higher) ────────
void CheckBullishSweep(double o, double h, double l, double c, datetime t) {
   PriorLiquidity prior = SessionTracker_GetPrior();
   SessionRange asian = SessionTracker_GetAsian();
   SessionRange london = SessionTracker_GetLondon();

   double levels[]; string level_types[]; int lc = 0;
   if(asian.low > 0)   { ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = asian.low;   level_types[lc] = "asian_low"; lc++; }
   if(prior.pdl > 0)   { ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = prior.pdl;   level_types[lc] = "pdl"; lc++; }
   if(london.low > 0 && SessionTracker_IsSession(SESSION_NY)) {
      ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = london.low; level_types[lc] = "london_low"; lc++;
   }
   for(int i = 0; i < ArraySize(prior.equal_lows); i++) {
      ArrayResize(levels, lc+1); ArrayResize(level_types, lc+1); levels[lc] = prior.equal_lows[i]; level_types[lc] = "equal_low"; lc++;
   }

   for(int i = 0; i < lc; i++) {
      double lvl = levels[i];
      if(l < lvl && c > lvl) {
         SweepEvent se; se.type = "bullish"; se.level = lvl; se.level_type = level_types[i];
         se.time = t; se.wick_extreme = l; se.body_close = c; se.confirmed = true;

         double vol = (double)iVolume(Symbol(), PERIOD_M1, 1);
         if(g_avg_volume_20 > 0 && vol > 0) se.volume_ratio = vol / g_avg_volume_20; else se.volume_ratio = 1.0;

         se.multi_touch = false; se.touch_count = 1;
         for(int k = 0; k < ArraySize(g_level_touches); k++) {
            if(MathAbs(g_level_touches[k].level - lvl) < lvl * 0.001 && g_level_touches[k].level_type == se.level_type) {
               se.multi_touch = (g_level_touches[k].touches >= 2); se.touch_count = g_level_touches[k].touches; break;
            }
         }
         SweepDetector_RecordSweep(se);
      }
   }
}

// ── Record a sweep event ──────────────────────────────────────────
void SweepDetector_RecordSweep(SweepEvent &se) {
   bool updated = false;
   for(int i = 0; i < ArraySize(g_recent_sweeps); i++) {
      if(g_recent_sweeps[i].level_type == se.level_type && MathAbs(g_recent_sweeps[i].level - se.level) < se.level * 0.001) {
         if(se.time - g_recent_sweeps[i].time < 300) { g_recent_sweeps[i] = se; updated = true; break; }
      }
   }
   if(!updated) {
      int sz = ArraySize(g_recent_sweeps); ArrayResize(g_recent_sweeps, sz + 1); g_recent_sweeps[sz] = se;
   }
   datetime now = TimeCurrent(); int keep = 0;
   for(int i = 0; i < ArraySize(g_recent_sweeps); i++) {
      if(now - g_recent_sweeps[i].time < g_sweep_expiry_seconds) { if(i != keep) g_recent_sweeps[keep] = g_recent_sweeps[i]; keep++; }
   }
   ArrayResize(g_recent_sweeps, keep);
}

// ── Export sweeps to JSON ─────────────────────────────────────────
string SweepDetector_ExportJSON() {
   string s = "\"sweeps\":"; s += "[";
   for(int i = 0; i < ArraySize(g_recent_sweeps); i++) {
      if(i > 0) s += ",";
      SweepEvent e = g_recent_sweeps[i];
      s += "{\"type\":\"" + e.type + "\",\"level\":" + DoubleToString(e.level, Digits()) + ",\"level_type\":\"" + e.level_type + "\",\"time\":\"" + TimeToString(e.time, TIME_DATE|TIME_SECONDS) + "\",\"wick_extreme\":" + DoubleToString(e.wick_extreme, Digits()) + ",\"body_close\":" + DoubleToString(e.body_close, Digits()) + ",\"confirmed\":" + (e.confirmed ? "true":"false") + ",\"volume_ratio\":" + DoubleToString(e.volume_ratio, 2) + ",\"multi_touch\":" + (e.multi_touch ? "true":"false") + ",\"touch_count\":" + IntegerToString(e.touch_count) + "}";
   }
   s += "]"; return s;
}

// ── Accessors for other modules ───────────────────────────────────
bool SweepDetector_HasRecentSweep(string type_filter, string level_type_filter, int max_seconds) {
   datetime now = TimeCurrent();
   for(int i = 0; i < ArraySize(g_recent_sweeps); i++) {
      if(g_recent_sweeps[i].type == type_filter && g_recent_sweeps[i].level_type == level_type_filter) {
         if(now - g_recent_sweeps[i].time <= max_seconds) return true;
      }
   }
   return false;
}
bool SweepDetector_GetMostRecent(string type_filter, SweepEvent &out) {
   datetime newest = 0; int idx = -1;
   for(int i = 0; i < ArraySize(g_recent_sweeps); i++) {
      if(g_recent_sweeps[i].type == type_filter && g_recent_sweeps[i].time > newest) { newest = g_recent_sweeps[i].time; idx = i; }
   }
   if(idx >= 0) { out = g_recent_sweeps[idx]; return true; }
   return false;
}
