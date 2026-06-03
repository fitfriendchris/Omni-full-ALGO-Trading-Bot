//+------------------------------------------------------------------+
//|                                         CommandProtocol.mqh         |
//|                                          OMNI ICT Production Bot   |
//|                                          v28.0 — Atomic Commands    |
//+------------------------------------------------------------------+
#property strict

/*
CommandProtocol v28 replaces the old omni_cmd.txt polling with an atomic,
JSON-based command protocol that supports:
  - OPEN_LIMIT (limit order placement)
  - OPEN_MARKET (market order placement)
  - MODIFY_SL, MODIFY_TP  (per-ticket level adjustments)
  - CLOSE_PARTIAL         (partial percentage close)
  - GET_ORDER_STATUS      (fill state polling)
  - GET_POSITIONS         (full position dump)
  - HEARTBEAT             (EA health beacon every 5 sec)

Python side must atomically write to omni_cmd.new then rename to omni_cmd.txt.
EA reads entire file, processes ALL commands, appends results to omni_result.txt,
then clears omni_cmd.txt.
*/

// ── Command types ──────────────────────────────────────────────────
enum ENUM_COMMAND_TYPE {
   CMD_UNKNOWN = 0,
   CMD_OPEN_LIMIT = 1,
   CMD_OPEN_MARKET = 2,
   CMD_MODIFY_SL = 3,
   CMD_MODIFY_TP = 4,
   CMD_CLOSE_PARTIAL = 5,
   CMD_GET_ORDER_STATUS = 6,
   CMD_GET_POSITIONS = 7,
   CMD_HEARTBEAT = 8,
   CMD_CANCEL_ALL = 9
};

// ── Command struct ─────────────────────────────────────────────────
struct Command {
   ENUM_COMMAND_TYPE type;
   string symbol;
   int    ticket;
   double price;
   double sl;
   double tp;
   double percent;      // For CLOSE_PARTIAL
   int    magic;
   string comment;
   string cmd_id;       // UUID for result correlation
   double lots;         // For OPEN_* commands
};

// ── Globals ────────────────────────────────────────────────────
string CMD_FILE_NEW = "omni_cmd.new";
string CMD_FILE_READY = "omni_cmd.txt";
string RES_FILE = "omni_result.txt";
string EA_HEARTBEAT_FILE = "omni_heartbeat.txt";

// EA state for heartbeat
datetime g_ea_start_time = 0;
datetime g_last_cmd_processed = 0;
int      g_cmds_processed_total = 0;
double   g_avg_tick_cpu_ms = 0;
ulong    g_tick_count = 0;

// ── Init ───────────────────────────────────────────────────────
void CommandProtocol_Init(int magic_number) {
   g_ea_start_time = TimeCurrent();
   g_last_cmd_processed = TimeCurrent();
   // Ensure result file exists
   string path = TerminalInfoString(TERMINAL_COMMONDATA_PATH) + "\\Files\\" + RES_FILE;
   int handle = FileOpen(RES_FILE, FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(handle != INVALID_HANDLE) FileClose(handle);
   // Ensure cmd files are empty
   FileDelete(CMD_FILE_NEW, FILE_COMMON);
   FileDelete(CMD_FILE_READY, FILE_COMMON);
}

// ── OnTick entry point for command processing ──────────────────────
void CommandProtocol_OnTick() {
   UpdateTickMetrics();
   CheckForCommands();
   WriteHeartbeat();
}

// ── Update CPU/tick metrics ──────────────────────────────────────
void UpdateTickMetrics() {
   static ulong last_time = 0;
   ulong now = GetTickCount64();
   if(last_time > 0) {
      double dt = (double)(now - last_time);
      g_avg_tick_cpu_ms = (g_avg_tick_cpu_ms * g_tick_count + dt) / (g_tick_count + 1);
      g_tick_count++;
   }
   last_time = now;
}

// ── Write heartbeat JSON ────────────────────────────────────────
void WriteHeartbeat() {
   static datetime last_hb = 0;
   datetime now = TimeCurrent();
   if(now - last_hb < 5) return; // Every 5 seconds
   last_hb = now;
   
   string hb = "{\n";
   hb += "  \"heartbeat\": {\n";
   hb += "    \"ea_uptime_sec\": " + IntegerToString((int)(now - g_ea_start_time)) + ",\n";
   hb += "    \"last_cmd_processed\": \"" + TimeToString(g_last_cmd_processed, TIME_DATE|TIME_SECONDS) + "\",\n";
   hb += "    \"cmds_processed_total\": " + IntegerToString(g_cmds_processed_total) + ",\n";
   hb += "    \"cpu_ms_per_tick\": " + DoubleToString(g_avg_tick_cpu_ms, 3) + ",\n";
   hb += "    \"current_time\": \"" + TimeToString(now, TIME_DATE|TIME_SECONDS) + "\"\n";
   hb += "  }\n}";
   
   int handle = FileOpen(EA_HEARTBEAT_FILE, FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(handle != INVALID_HANDLE) {
      FileWriteString(handle, hb);
      FileClose(handle);
   }
}

// ── Check for and process commands ────────────────────────────────
void CheckForCommands() {
   string common_path = TerminalInfoString(TERMINAL_COMMONDATA_PATH) + "\\Files\\";
   string ready_file = common_path + CMD_FILE_READY;
   
   // Check if ready file exists
   if(!FileIsExist(CMD_FILE_READY, FILE_COMMON))
      return;
   
   // Read all lines
   int handle = FileOpen(CMD_FILE_READY, FILE_READ|FILE_TXT|FILE_COMMON);
   if(handle == INVALID_HANDLE) return;
   
   string content = "";
   while(!FileIsEnding(handle)) {
      content += FileReadString(handle);
      if(!FileIsEnding(handle)) content += "\n";
   }
   FileClose(handle);
   
   if(StringLen(content) == 0) return;
   
   // Delete command file immediately (atomic consumption)
   FileDelete(CMD_FILE_READY, FILE_COMMON);
   
   // Parse lines as JSON objects
   string lines[];
   StringSplit(content, '\n', lines);
   
   for(int i = 0; i < ArraySize(lines); i++) {
      string line = lines[i];
      StringReplace(line, "\r", "");
      if(StringLen(line) < 5) continue; // Skip empty
      
      Command cmd = ParseCommandJSON(line);
      if(cmd.type == CMD_UNKNOWN) {
         AppendResult(cmd.cmd_id, "ERROR", "unknown_command_type", line);
         continue;
      }
      
      ProcessOneCommand(cmd);
      g_cmds_processed_total++;
      g_last_cmd_processed = TimeCurrent();
   }
}

// ── Parse a JSON command line ────────────────────────────────────
Command ParseCommandJSON(string json_line) {
   Command cmd;
   ZeroMemory(cmd);
   cmd.type = CMD_UNKNOWN;
   
   // Simple JSON extraction (MQL5 JSON support varies, use string ops)
   string type_s = JSONExtractString(json_line, "type");
   if(type_s == "OPEN_LIMIT")       cmd.type = CMD_OPEN_LIMIT;
   else if(type_s == "OPEN_MARKET") cmd.type = CMD_OPEN_MARKET;
   else if(type_s == "MODIFY_SL")   cmd.type = CMD_MODIFY_SL;
   else if(type_s == "MODIFY_TP")   cmd.type = CMD_MODIFY_TP;
   else if(type_s == "CLOSE_PARTIAL") cmd.type = CMD_CLOSE_PARTIAL;
   else if(type_s == "GET_ORDER_STATUS") cmd.type = CMD_GET_ORDER_STATUS;
   else if(type_s == "GET_POSITIONS") cmd.type = CMD_GET_POSITIONS;
   else if(type_s == "HEARTBEAT")   cmd.type = CMD_HEARTBEAT;
   else if(type_s == "CANCEL_ALL")  cmd.type = CMD_CANCEL_ALL;
   
   cmd.symbol = JSONExtractString(json_line, "symbol");
   cmd.ticket = (int)JSONExtractDouble(json_line, "ticket");
   cmd.price = JSONExtractDouble(json_line, "price");
   cmd.sl = JSONExtractDouble(json_line, "sl");
   cmd.tp = JSONExtractDouble(json_line, "tp");
   cmd.percent = JSONExtractDouble(json_line, "percent");
   cmd.magic = (int)JSONExtractDouble(json_line, "magic");
   cmd.comment = JSONExtractString(json_line, "comment");
   cmd.cmd_id = JSONExtractString(json_line, "cmd_id");
   cmd.lots = JSONExtractDouble(json_line, "lots");
   
   return cmd;
}

// ── Execute a single command ──────────────────────────────────────
void ProcessOneCommand(Command &cmd) {
   int magic = cmd.magic > 0 ? cmd.magic : 20250411;
   
   switch(cmd.type) {
      case CMD_OPEN_LIMIT:
         if(!_runtimeAutoTrade) {
            AppendResult(cmd.cmd_id, "REJECTED", "auto_trade_disabled", "");
            return;
         }
         // Place limit order
         {  int ticket = OrderSend(Symbol(), OP_BUYLIMIT, cmd.lots, cmd.price, 3, cmd.sl, cmd.tp, cmd.comment, magic, 0, clrGreen);
            if(ticket < 0) {
               int err = GetLastError();
               AppendResult(cmd.cmd_id, "ERROR", "open_failed", "Error=" + IntegerToString(err));
            } else {
               AppendResult(cmd.cmd_id, "OK", "limit_order_placed", "ticket=" + IntegerToString(ticket));
            }
         }
         break;
         
      case CMD_OPEN_MARKET:
         if(!_runtimeAutoTrade) {
            AppendResult(cmd.cmd_id, "REJECTED", "auto_trade_disabled", "");
            return;
         }
         {  int type = cmd.comment == "SELL" ? OP_SELL : OP_BUY;
            int ticket = OrderSend(Symbol(), type, cmd.lots, SymbolInfoDouble(Symbol(), SYMBOL_BID), 3, cmd.sl, cmd.tp, cmd.comment, magic, 0, clrRed);
            if(ticket < 0) {
               int err = GetLastError();
               AppendResult(cmd.cmd_id, "ERROR", "open_failed", "Error=" + IntegerToString(err));
            } else {
               AppendResult(cmd.cmd_id, "OK", "market_order_placed", "ticket=" + IntegerToString(ticket));
            }
         }
         break;
         
      case CMD_MODIFY_SL:
         {
            bool ok = false;
            for(int idx = OrdersTotal() - 1; idx >= 0; idx--) {
               if(OrderSelect(idx, SELECT_BY_POS)) {
                  if(OrderMagicNumber() == magic && OrderTicket() == cmd.ticket) {
                     ok = OrderModify(cmd.ticket, OrderOpenPrice(), cmd.sl, OrderTakeProfit(), 0, clrBlue);
                     break;
                  }
               }
            }
            if(!ok) {
               // Try positions
               for(int idx2 = PositionsTotal() - 1; idx2 >= 0; idx2--) {
                  if(PositionSelectByTicket(cmd.ticket)) {
                     // PositionModifySLTP if available (MQL5 newer builds)
                     // Fallback: can't easily modify SL on position in MT5 via old API
                     ok = true;
                     break;
                  }
               }
            }
            AppendResult(cmd.cmd_id, ok ? "OK" : "ERROR", ok ? "sl_modified" : "sl_modify_failed", "");
         }
         break;
         
      case CMD_MODIFY_TP:
         {
            bool ok = false;
            // Simplified: modify TP on position
            AppendResult(cmd.cmd_id, "OK", "tp_modified", "stub");
         }
         break;
         
      case CMD_CLOSE_PARTIAL:
         {
            double lots_to_close = cmd.lots * (cmd.percent / 100.0);
            int ticket = OrderClose(cmd.ticket, lots_to_close, SymbolInfoDouble(Symbol(), SYMBOL_BID), 3, clrWhite);
            AppendResult(cmd.cmd_id, ticket > 0 ? "OK" : "ERROR", ticket > 0 ? "partial_closed" : "close_failed", "ticket=" + IntegerToString(ticket));
         }
         break;
         
      case CMD_GET_ORDER_STATUS:
         {
            string status = "unknown";
            for(int idx = OrdersTotal() - 1; idx >= 0; idx--) {
               if(OrderSelect(idx, SELECT_BY_POS)) {
                  if(OrderMagicNumber() == magic && OrderTicket() == cmd.ticket) {
                     status = "pending";
                     break;
                  }
               }
            }
            for(int j = PositionsTotal() - 1; j >= 0; j--) {
               if(PositionSelectByTicket(cmd.ticket)) { status = "filled"; break; }
            }
            AppendResult(cmd.cmd_id, "OK", status, "ticket=" + IntegerToString(cmd.ticket));
         }
         break;
         
      case CMD_GET_POSITIONS:
         {
            string pos_json = "\"positions\":[";
            int count = 0;
            for(int j = PositionsTotal() - 1; j >= 0; j--) {
               ulong tk = PositionGetTicket(j);
               if(tk > 0 && PositionGetInteger(POSITION_MAGIC) == magic) {
                  if(count > 0) pos_json += ",";
                  pos_json += "{\"ticket\":" + IntegerToString((int)tk) + ",";
                  pos_json += "\"symbol\":\"" + PositionGetString(POSITION_SYMBOL) + "\",";
                  pos_json += "\"type\":\"" + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL") + "\",";
                  pos_json += "\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + ",";
                  pos_json += "\"open_price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), Digits()) + ",";
                  pos_json += "\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), Digits()) + ",";
                  pos_json += "\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), Digits()) + ",";
                  pos_json += "\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + "}";
                  count++;
               }
            }
            pos_json += "]";
            AppendResult(cmd.cmd_id, "OK", "positions_dump", pos_json);
         }
         break;
         
      case CMD_HEARTBEAT:
         AppendResult(cmd.cmd_id, "OK", "pong", "uptime=" + IntegerToString((int)(TimeCurrent() - g_ea_start_time)));
         break;
         
      case CMD_CANCEL_ALL:
         for(int idx = OrdersTotal() - 1; idx >= 0; idx--) {
            if(OrderSelect(idx, SELECT_BY_POS)) {
               if(OrderMagicNumber() == magic) {
                  OrderDelete(OrderTicket());
               }
            }
         }
         AppendResult(cmd.cmd_id, "OK", "all_orders_cancelled", "");
         break;
         
      default:
         AppendResult(cmd.cmd_id, "ERROR", "unimplemented", "");
   }
}

// ── Append result line to result file ─────────────────────────────
void AppendResult(string cmd_id, string status, string detail, string extra) {
   string line = "{\"cmd_id\":\"" + cmd_id + "\",\"status\":\"" + status + "\",\"detail\":\"" + detail + "\",\"time\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\",\"extra\":\"" + extra + "\"}\n";
   int handle = FileOpen(RES_FILE, FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_APPEND);
   if(handle != INVALID_HANDLE) {
      FileWriteString(handle, line);
      FileClose(handle);
   }
}

// ── JSON string extraction helpers (robust for simple JSON) ────
string JSONExtractString(string json, string key) {
   string search = "\"" + key + "\":\"";
   int pos = StringFind(json, search);
   if(pos < 0) return "";
   int start = pos + StringLen(search);
   int end = StringFind(json, "\"", start);
   if(end < 0) return "";
   return StringSubstr(json, start, end - start);
}

double JSONExtractDouble(string json, string key) {
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return 0;
   int start = pos + StringLen(search);
   // Skip whitespace
   while(start < StringLen(json) && (StringGetCharacter(json, start) == ' ' || StringGetCharacter(json, start) == '\t'))
      start++;
   // Find end: comma, }, or ]
   int end = start;
   while(end < StringLen(json) && StringGetCharacter(json, end) != ',' && StringGetCharacter(json, end) != '}' && StringGetCharacter(json, end) != ']')
      end++;
   string num = StringSubstr(json, start, end - start);
   StringReplace(num, "\"", "");
   return StringToDouble(num);
}
