//+------------------------------------------------------------------+
//|                                          SessionTracker.mqh        |
//|                                          OMNI ICT Production Bot   |
//|                                          v28.0 — Real-time Session |
//+------------------------------------------------------------------+
#property strict

// ── Session boundaries (UTC) ────────────────────────────────────────
input group "=== Session UTC Boundaries ==="
input int INP_AsianStart_Hour  = 0;   // Asian start hour UTC
input int INP_AsianStart_Min   = 0;
input int INP_AsianEnd_Hour    = 8;   // Asian end hour UTC
input int INP_AsianEnd_Min     = 0;
input int INP_LondonStart_Hour = 8;   // London start hour UTC
input int INP_LondonStart_Min  = 0;
input int INP_LondonEnd_Hour   = 13;  // London end hour UTC
input int INP_LondonEnd_Min    = 0;
input int INP_NYStart_Hour     = 13;  // NY start hour UTC
input int INP_NYStart_Min      = 0;
input int INP_NYEnd_Hour       = 17;  // NY end hour UTC
input int INP_NYEnd_Min        = 0;
input int INP_LookbackDays     = 5;   // Days for prior high/low

// ── Session identifiers ─────────────────────────────────────────────
enum ENUM_SESSION {
   SESSION_OFF,      // Outside all sessions
   SESSION_ASIAN,    // 00:00-08:00 UTC
   SESSION_LONDON,   // 08:00-13:00 UTC
   SESSION_NY,       // 13:00-17:00 UTC
   SESSION_OFF_POST  // After NY close
};

// ── Session range struct (forming = currently building) ─────────────
struct SessionRange {
   double high;
   double low;
   datetime start_time;
   datetime end_time;
   bool   forming;
   bool   finalized;
   double open;
};

// ── Prior liquidity levels ──────────────────────────────────────────
struct PriorLiquidity {
   double pdh;            // Previous day high
   double pdl;            // Previous day low
   double p2dh;           // Previous 2-day high
   double p2dl;           // Previous 2-day low
   double w_high;         // Weekly high
   double w_low;          // Weekly low
   double m_high;         // Monthly high
   double m_low;          // Monthly low
   double equal_highs[];  // Dynamic equal highs detected
   double equal_lows[];   // Dynamic equal lows detected
};

// ── Module globals ────────────────────────────────────────────────
SessionRange g_asian;
SessionRange g_london;
SessionRange g_ny;
PriorLiquidity g_prior;
ENUM_SESSION g_current_session = SESSION_OFF;
datetime g_last_session_boundary = 0;

// ── Helper: Time to minutes since midnight UTC ──────────────────────
int TimeToMinutesUTC(datetime t) {
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return dt.hour * 60 + dt.min;
}

// ── Helper: Get current UTC time ────────────────────────────────────
datetime NowUTC() {
   return TimeCurrent();
}

// ── Helper: Normalize session boundary datetime ─────────────────────
datetime SessionBoundaryDate(datetime t, int h, int m) {
   MqlDateTime dt;
   TimeToStruct(t, dt);
   dt.hour = h; dt.min = m; dt.sec = 0;
   return StructToTime(dt);
}

// ── Initialize / reset session ranges ───────────────────────────────
void SessionTracker_Init() {
   ZeroMemory(g_asian);
   ZeroMemory(g_london);
   ZeroMemory(g_ny);
   g_current_session = SESSION_OFF;
   g_last_session_boundary = 0;
   SessionTracker_UpdatePriorLiquidity();
}

// ── Detect which session we are in based on UTC time ────────────────
ENUM_SESSION GetCurrentSession(datetime t) {
   int mins = TimeToMinutesUTC(t);
   int asian_start  = INP_AsianStart_Hour  * 60 + INP_AsianStart_Min;
   int asian_end    = INP_AsianEnd_Hour    * 60 + INP_AsianEnd_Min;
   int london_start = INP_LondonStart_Hour * 60 + INP_LondonStart_Min;
   int london_end   = INP_LondonEnd_Hour   * 60 + INP_LondonEnd_Min;
   int ny_start     = INP_NYStart_Hour     * 60 + INP_NYStart_Min;
   int ny_end       = INP_NYEnd_Hour       * 60 + INP_NYEnd_Min;

   if(mins >= asian_start && mins < asian_end)       return SESSION_ASIAN;
   if(mins >= london_start && mins < london_end)    return SESSION_LONDON;
   if(mins >= ny_start && mins < ny_end)            return SESSION_NY;
   if(mins >= ny_end)                               return SESSION_OFF_POST;
   return SESSION_OFF;
}

// ── Called on every tick to update forming ranges ─────────────────
void SessionTracker_OnTick() {
   datetime now = NowUTC();
   ENUM_SESSION session = GetCurrentSession(now);

   // Detect session boundary crossing
   if(session != g_current_session) {
      // Finalize outgoing session
      if(g_current_session == SESSION_ASIAN) {
         g_asian.forming = false;
         g_asian.finalized = true;
         g_asian.end_time = now;
      } else if(g_current_session == SESSION_LONDON) {
         g_london.forming = false;
         g_london.finalized = true;
         g_london.end_time = now;
      } else if(g_current_session == SESSION_NY) {
         g_ny.forming = false;
         g_ny.finalized = true;
         g_ny.end_time = now;
      }

      // Initialize incoming session
      if(session == SESSION_ASIAN) {
         ZeroMemory(g_asian);
         g_asian.start_time = now;
         g_asian.forming = true;
         g_asian.high = iHigh(Symbol(), PERIOD_M1, 0);
         g_asian.low  = iLow(Symbol(), PERIOD_M1, 0);
         g_asian.open = iOpen(Symbol(), PERIOD_M1, 0);
      } else if(session == SESSION_LONDON) {
         ZeroMemory(g_london);
         g_london.start_time = now;
         g_london.forming = true;
         g_london.high = iHigh(Symbol(), PERIOD_M1, 0);
         g_london.low  = iLow(Symbol(), PERIOD_M1, 0);
         g_london.open = iOpen(Symbol(), PERIOD_M1, 0);
      } else if(session == SESSION_NY) {
         ZeroMemory(g_ny);
         g_ny.start_time = now;
         g_ny.forming = true;
         g_ny.high = iHigh(Symbol(), PERIOD_M1, 0);
         g_ny.low  = iLow(Symbol(), PERIOD_M1, 0);
         g_ny.open = iOpen(Symbol(), PERIOD_M1, 0);
      }

      g_current_session = session;
      g_last_session_boundary = now;
      SessionTracker_UpdatePriorLiquidity(); // Refresh at day transition
   }

   // Update forming ranges
   double cur_high = iHigh(Symbol(), PERIOD_M1, 0);
   double cur_low  = iLow(Symbol(), PERIOD_M1, 0);

   if(g_asian.forming) {
      if(cur_high > g_asian.high) g_asian.high = cur_high;
      if(cur_low  < g_asian.low)  g_asian.low  = cur_low;
   }
   if(g_london.forming) {
      if(cur_high > g_london.high) g_london.high = cur_high;
      if(cur_low  < g_london.low)  g_london.low  = cur_low;
   }
   if(g_ny.forming) {
      if(cur_high > g_ny.high) g_ny.high = cur_high;
      if(cur_low  < g_ny.low)  g_ny.low  = cur_low;
   }
}

// ── Update prior-day, prior-week, prior-month liquidity ─────────────
void SessionTracker_UpdatePriorLiquidity() {
   MqlDateTime now_dt;
   TimeToStruct(NowUTC(), now_dt);

   // Previous day high/low (D1 lookback)
   double pdh = iHigh(Symbol(), PERIOD_D1, 1);
   double pdl = iLow(Symbol(), PERIOD_D1, 1);
   if(pdh > 0) g_prior.pdh = pdh;
   if(pdl > 0) g_prior.pdl = pdl;

   // 2-day high/low
   double d2h = iHigh(Symbol(), PERIOD_D1, 2);
   double d2l = iLow(Symbol(), PERIOD_D1, 2);
   if(d2h > 0 && d2h > g_prior.pdh) g_prior.p2dh = d2h; else g_prior.p2dh = g_prior.pdh;
   if(d2l > 0 && d2l < g_prior.pdl) g_prior.p2dl = d2l; else g_prior.p2dl = g_prior.pdl;

   // Weekly high/low
   double wh = iHigh(Symbol(), PERIOD_W1, 1);
   double wl = iLow(Symbol(), PERIOD_W1, 1);
   if(wh > 0) g_prior.w_high = wh;
   if(wl > 0) g_prior.w_low  = wl;

   // Monthly high/low
   double mh = iHigh(Symbol(), PERIOD_MN1, 1);
   double ml = iLow(Symbol(), PERIOD_MN1, 1);
   if(mh > 0) g_prior.m_high = mh;
   if(ml > 0) g_prior.m_low  = ml;

   // Detect equal highs/lows over last lookback days (H1 granularity)
   SessionTracker_DetectEqualLevels(INP_LookbackDays);
}

// ── Detect equal highs/lows within tolerance ──────────────────────
void SessionTracker_DetectEqualDays(int days) {
   // Simple impl: scan last N*24 H1 bars for levels within 0.2%
   ArrayResize(g_prior.equal_highs, 0);
   ArrayResize(g_prior.equal_lows, 0);

   int bars = days * 24;
   double highs[];
   double lows[];
   ArrayResize(highs, bars);
   ArrayResize(lows, bars);

   for(int i = 0; i < bars; i++) {
      highs[i] = iHigh(Symbol(), PERIOD_H1, i + 1);
      lows[i]  = iLow(Symbol(), PERIOD_H1, i + 1);
   }

   double tol_pct = 0.002; // 0.2% tolerance
   for(int i = 0; i < bars; i++) {
      for(int j = i + 1; j < bars; j++) {
         if(highs[i] > 0 && MathAbs(highs[i] - highs[j]) < highs[i] * tol_pct) {
            // Found equal high — add if not already present
            bool exists = false;
            for(int k = 0; k < ArraySize(g_prior.equal_highs); k++) {
               if(MathAbs(g_prior.equal_highs[k] - highs[i]) < highs[i] * tol_pct) { exists = true; break; }
            }
            if(!exists) {
               int sz = ArraySize(g_prior.equal_highs);
               ArrayResize(g_prior.equal_highs, sz + 1);
               g_prior.equal_highs[sz] = (highs[i] + highs[j]) / 2.0;
            }
         }
         if(lows[i] > 0 && MathAbs(lows[i] - lows[j]) < lows[i] * tol_pct) {
            bool exists = false;
            for(int k = 0; k < ArraySize(g_prior.equal_lows); k++) {
               if(MathAbs(g_prior.equal_lows[k] - lows[i]) < lows[i] * tol_pct) { exists = true; break; }
            }
            if(!exists) {
               int sz = ArraySize(g_prior.equal_lows);
               ArrayResize(g_prior.equal_lows, sz + 1);
               g_prior.equal_lows[sz] = (lows[i] + lows[j]) / 2.0;
            }
         }
      }
   }
}

// ── Export session ranges to JSON string ────────────────────────────
string SessionTracker_ExportJSON() {
   string s = "\"session_ranges\":";
   s += "{";
     s += "\"current_session\":\"" + SessionName(g_current_session) + "\",";
     s += "\"asian\":{";
       s += "\"high\":" + (g_asian.high > 0 ? DoubleToString(g_asian.high, Digits()) : "null") + ",";
       s += "\"low\":"  + (g_asian.low  > 0 ? DoubleToString(g_asian.low,  Digits()) : "null") + ",";
       s += "\"forming\":" + (g_asian.forming ? "true" : "false") + ",";
       s += "\"finalized\":" + (g_asian.finalized ? "true" : "false");
     s += "},";
     s += "\"london\":{";
       s += "\"high\":" + (g_london.high > 0 ? DoubleToString(g_london.high, Digits()) : "null") + ",";
       s += "\"low\":"  + (g_london.low  > 0 ? DoubleToString(g_london.low,  Digits()) : "null") + ",";
       s += "\"forming\":" + (g_london.forming ? "true" : "false") + ",";
       s += "\"finalized\":" + (g_london.finalized ? "true" : "false");
     s += "},";
     s += "\"ny\":{";
       s += "\"high\":" + (g_ny.high > 0 ? DoubleToString(g_ny.high, Digits()) : "null") + ",";
       s += "\"low\":"  + (g_ny.low  > 0 ? DoubleToString(g_ny.low,  Digits()) : "null") + ",";
       s += "\"forming\":" + (g_ny.forming ? "true" : "false") + ",";
       s += "\"finalized\":" + (g_ny.finalized ? "true" : "false");
     s += "},";
     s += "\"pdh\":" + (g_prior.pdh > 0 ? DoubleToString(g_prior.pdh, Digits()) : "null") + ",";
     s += "\"pdl\":" + (g_prior.pdl > 0 ? DoubleToString(g_prior.pdl, Digits()) : "null") + ",";
     s += "\"p2dh\":" + (g_prior.p2dh > 0 ? DoubleToString(g_prior.p2dh, Digits()) : "null") + ",";
     s += "\"p2dl\":" + (g_prior.p2dl > 0 ? DoubleToString(g_prior.p2dl, Digits()) : "null") + ",";
     s += "\"weekly_high\":" + (g_prior.w_high > 0 ? DoubleToString(g_prior.w_high, Digits()) : "null") + ",";
     s += "\"weekly_low\":" + (g_prior.w_low > 0 ? DoubleToString(g_prior.w_low, Digits()) : "null") + ",";
     s += "\"monthly_high\":" + (g_prior.m_high > 0 ? DoubleToString(g_prior.m_high, Digits()) : "null") + ",";
     s += "\"monthly_low\":" + (g_prior.m_low > 0 ? DoubleToString(g_prior.m_low, Digits()) : "null") + ",";
     s += "\"equal_highs\":" + ArrayToJSONArray(g_prior.equal_highs) + ",";
     s += "\"equal_lows\":"  + ArrayToJSONArray(g_prior.equal_lows);
   s += "}";
   return s;
}

// ── Helpers for JSON export ────────────────────────────────────────
string SessionName(ENUM_SESSION s) {
   switch(s) {
      case SESSION_ASIAN:    return "asian";
      case SESSION_LONDON:   return "london";
      case SESSION_NY:       return "ny";
      case SESSION_OFF_POST: return "off_post";
      default:               return "off";
   }
}
string ArrayToJSONArray(double &arr[]) {
   string j = "[";
   for(int i = 0; i < ArraySize(arr); i++) {
      if(i > 0) j += ",";
      j += DoubleToString(arr[i], Digits());
   }
   j += "]";
   return j;
}

// ── Accessors for other modules ─────────────────────────────────────
bool SessionTracker_IsSession(ENUM_SESSION s) { return g_current_session == s; }
SessionRange SessionTracker_GetAsian()       { return g_asian; }
SessionRange SessionTracker_GetLondon()      { return g_london; }
SessionRange SessionTracker_GetNY()          { return g_ny; }
PriorLiquidity SessionTracker_GetPrior()     { return g_prior; }
double SessionTracker_AsianRange() {
   if(g_asian.high > 0 && g_asian.low > 0) return g_asian.high - g_asian.low;
   return 0;
}
double SessionTracker_LondonRange() {
   if(g_london.high > 0 && g_london.low > 0) return g_london.high - g_london.low;
   return 0;
}
bool SessionTracker_AsianSweptHigh() {
   return (g_london.forming || g_london.finalized) && g_london.high > g_asian.high && g_asian.high > 0;
}
bool SessionTracker_AsianSweptLow() {
   return (g_london.forming || g_london.finalized) && g_london.low < g_asian.low && g_asian.low > 0;
}
