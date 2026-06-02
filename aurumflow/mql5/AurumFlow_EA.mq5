//+------------------------------------------------------------------+
//|                                            AurumFlow_EA.mq5      |
//|                     ZeroMQ Bridge for AurumFlow Trading Bot       |
//|                                                                  |
//|  Connects to the Python AurumFlow bot via ZeroMQ sockets.        |
//|  PUBLISHER: sends ticks, account info, positions (PUSH)          |
//|  SUBSCRIBER: receives trading commands from Python (PULL)        |
//|                                                                  |
//|  Compilation: Open in MetaEditor, press F7 -> AurumFlow_EA.ex5  |
//+------------------------------------------------------------------+
#property copyright "AurumFlow Team"
#property link      "https://github.com/aurumflow"
#property version   "1.00"
#property strict

// ---- Include ZeroMQ library ----
// Download zmq.dll / libzmq from https://zeromq.org/download/
// Place in MT5/Libraries/ folder
#include <Zmq.mqh>

// ---- Input parameters ----
input string Inp_ZMQ_PublishAddr    = "tcp://*:5555";   // Publish address (ticks -> Python)
input string Inp_ZMQ_SubscribeAddr  = "tcp://*:5556";   // Subscribe address (commands from Python)
input string Inp_Symbol             = "XAUUSD";         // Trading symbol
input int    Inp_MagicNumber        = 202405;           // EA magic number
input bool   Inp_TradeAllowed       = true;             // Allow EA to trade

// ---- Global ZMQ objects ----
Context     context;
Socket      pubSocket(context, ZMQ_PUSH);    // Send data to Python
Socket      subSocket(context, ZMQ_PULL);    // Receive commands from Python

// ---- Global state ----
string   g_symbol;
int      g_magic;
ulong    g_last_tick_time = 0;
int      g_heartbeat_sec  = 5;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol = Inp_Symbol;
   g_magic  = Inp_MagicNumber;

   Print("============================================");
   Print("AurumFlow EA initializing...");
   Print("Symbol: ", g_symbol);
   Print("Publish: ", Inp_ZMQ_PublishAddr);
   Print("Subscribe: ", Inp_ZMQ_SubscribeAddr);

   // Bind ZMQ sockets
   if (!pubSocket.bind(Inp_ZMQ_PublishAddr))
   {
      Print("ERROR: Failed to bind PUSH socket to ", Inp_ZMQ_PublishAddr);
      return INIT_FAILED;
   }
   Print("PUSH socket bound to ", Inp_ZMQ_PublishAddr);

   if (!subSocket.bind(Inp_ZMQ_SubscribeAddr))
   {
      Print("ERROR: Failed to bind PULL socket to ", Inp_ZMQ_SubscribeAddr);
      return INIT_FAILED;
   }
   Print("PULL socket bound to ", Inp_ZMQ_SubscribeAddr);

   // Enable symbol in MarketWatch
   SymbolSelect(g_symbol, true);

   // Send initial heartbeat
   SendAccountInfo();

   Print("AurumFlow EA initialized successfully");
   Print("============================================");
   EventSetTimer(g_heartbeat_sec);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   pubSocket.disconnect(Inp_ZMQ_PublishAddr);
   subSocket.disconnect(Inp_ZMQ_SubscribeAddr);
   Print("AurumFlow EA shutdown (reason: ", reason, ")");
}

//+------------------------------------------------------------------+
//| Timer function - periodic heartbeat                              |
//+------------------------------------------------------------------+
void OnTimer()
{
   SendAccountInfo();
   SendPositions();
}

//+------------------------------------------------------------------+
//| Tick function - called on every tick                             |
//+------------------------------------------------------------------+
void OnTick()
{
   // 1. Send tick data to Python
   SendTick();

   // 2. Check for incoming commands from Python (non-blocking)
   CheckCommands();

   // 3. Check trailing stops on open positions
   CheckTrailingStops();
}

//+------------------------------------------------------------------+
//| Trade function - called on trade events                          |
//+------------------------------------------------------------------+
void OnTrade()
{
   // Send updated positions after any trade event
   SendPositions();
   SendAccountInfo();
}

//+------------------------------------------------------------------+
//| Send tick data to Python                                         |
//+------------------------------------------------------------------+
void SendTick()
{
   MqlTick last_tick;
   if (!SymbolInfoTick(g_symbol, last_tick))
      return;

   // Only send if time changed
   if (last_tick.time == g_last_tick_time)
      return;
   g_last_tick_time = last_tick.time;

   string json = StringFormat(
      "{"
      "\"type\":\"tick\","
      "\"symbol\":\"%s\","
      "\"bid\":%.5f,"
      "\"ask\":%.5f,"
      "\"last\":%.5f,"
      "\"volume\":%d,"
      "\"time\":%lld"
      "}",
      g_symbol,
      last_tick.bid,
      last_tick.ask,
      last_tick.last,
      last_tick.volume,
      last_tick.time
   );

   if (!pubSocket.send(json))
      Print("WARNING: Failed to send tick via ZMQ");
}

//+------------------------------------------------------------------+
//| Send account info to Python                                      |
//+------------------------------------------------------------------+
void SendAccountInfo()
{
   double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
   double margin     = AccountInfoDouble(ACCOUNT_MARGIN);
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double marginLevel = 0;
   if (margin > 0)
      marginLevel = (equity / margin) * 100.0;
   long   login      = AccountInfoInteger(ACCOUNT_LOGIN);
   string server     = AccountInfoString(ACCOUNT_SERVER);
   string currency   = AccountInfoString(ACCOUNT_CURRENCY);
   int    leverage   = (int)AccountInfoInteger(ACCOUNT_LEVERAGE);

   string json = StringFormat(
      "{"
      "\"type\":\"account\","
      "\"login\":%lld,"
      "\"server\":\"%s\","
      "\"balance\":%.2f,"
      "\"equity\":%.2f,"
      "\"margin\":%.2f,"
      "\"margin_free\":%.2f,"
      "\"margin_level\":%.2f,"
      "\"currency\":\"%s\","
      "\"leverage\":%d"
      "}",
      login, server, balance, equity, margin, freeMargin,
      marginLevel, currency, leverage
   );

   pubSocket.send(json, false); // Non-blocking send
}

//+------------------------------------------------------------------+
//| Send current positions to Python                                 |
//+------------------------------------------------------------------+
void SendPositions()
{
   string json = "{\"type\":\"positions\",\"data\":[";

   int total = PositionsTotal();
   bool first = true;

   for (int i = 0; i < total; i++)
   {
      if (!PositionSelectByTicket(PositionGetTicket(i)))
         continue;

      if (PositionGetString(POSITION_SYMBOL) != g_symbol &&
          PositionGetString(POSITION_SYMBOL) != "")
         continue;

      if (!first) json += ",";
      first = false;

      string pos_json = StringFormat(
         "{"
         "\"ticket\":%lld,"
         "\"symbol\":\"%s\","
         "\"type\":\"%s\","
         "\"volume\":%.2f,"
         "\"price_open\":%.5f,"
         "\"sl\":%.5f,"
         "\"tp\":%.5f,"
         "\"profit\":%.2f,"
         "\"swap\":%.2f,"
         "\"commission\":%.2f,"
         "\"time\":%lld,"
         "\"magic\":%lld"
         "}",
         PositionGetTicket(i),
         PositionGetString(POSITION_SYMBOL),
         (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell"),
         PositionGetDouble(POSITION_VOLUME),
         PositionGetDouble(POSITION_PRICE_OPEN),
         PositionGetDouble(POSITION_SL),
         PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT),
         PositionGetDouble(POSITION_SWAP),
         PositionGetDouble(POSITION_COMMISSION),
         (long)PositionGetInteger(POSITION_TIME),
         (long)PositionGetInteger(POSITION_MAGIC)
      );
      json += pos_json;
   }

   json += "]}";
   pubSocket.send(json, false);
}

//+------------------------------------------------------------------+
//| Check for incoming commands from Python                          |
//+------------------------------------------------------------------+
void CheckCommands()
{
   // Non-blocking receive
   string cmd = subSocket.receive(0);

   if (cmd == "")
      return;

   Print("Received command: ", cmd);

   // Parse and execute
   if (StringFind(cmd, "\"BUY\"") > 0 || StringFind(cmd, "\"SELL\"") > 0)
   {
      ExecuteMarketOrder(cmd);
   }
   else if (StringFind(cmd, "\"CLOSE\"") > 0)
   {
      ExecuteClose(cmd);
   }
   else if (StringFind(cmd, "\"MODIFY\"") > 0)
   {
      ExecuteModify(cmd);
   }
   else if (StringFind(cmd, "\"CLOSE_ALL\"") > 0)
   {
      CloseAllPositions();
   }
}

//+------------------------------------------------------------------+
//| Execute a market order from JSON command                         |
//+------------------------------------------------------------------+
void ExecuteMarketOrder(string cmd)
{
   if (!Inp_TradeAllowed)
   {
      Print("Trading disabled by config");
      return;
   }

   // Extract fields from JSON (simple parser)
   string action  = ExtractJsonString(cmd, "action");
   string symbol  = ExtractJsonString(cmd, "symbol");
   double volume  = ExtractJsonDouble(cmd, "volume");
   double sl      = ExtractJsonDouble(cmd, "sl");
   double tp      = ExtractJsonDouble(cmd, "tp");

   if (symbol == "") symbol = g_symbol;
   if (volume <= 0)  volume = 0.01;

   // Determine order type
   int order_type = (action == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   // Get current price
   double price = SymbolInfoDouble(symbol, (order_type == ORDER_TYPE_BUY) ? SYMBOL_ASK : SYMBOL_BID);

   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};

   request.action    = TRADE_ACTION_DEAL;
   request.symbol    = symbol;
   request.volume    = volume;
   request.type      = order_type;
   request.price     = price;
   request.deviation = 10;
   request.magic     = g_magic;
   request.comment   = "AurumFlow";
   request.type_time = ORDER_TIME_GTC;
   request.type_filling = ORDER_FILLING_IOC;

   if (sl > 0) request.sl = NormalizeDouble(sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
   if (tp > 0) request.tp = NormalizeDouble(tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));

   if (OrderSend(request, result))
   {
      Print("Order executed: ticket=", result.order, " price=", result.price);
      SendPositions();
      SendAccountInfo();
   }
   else
   {
      Print("Order failed: ", result.retcode, " - ", GetLastError());
   }
}

//+------------------------------------------------------------------+
//| Close a position by ticket number                                |
//+------------------------------------------------------------------+
void ExecuteClose(string cmd)
{
   long ticket = (long)ExtractJsonDouble(cmd, "ticket");

   if (ticket <= 0)
   {
      // Close all
      CloseAllPositions();
      return;
   }

   if (!PositionSelectByTicket(ticket))
   {
      Print("Position not found: ", ticket);
      return;
   }

   string symbol = PositionGetString(POSITION_SYMBOL);
   int type = (int)PositionGetInteger(POSITION_TYPE);
   double volume = PositionGetDouble(POSITION_VOLUME);

   int close_type = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   double price = SymbolInfoDouble(symbol, (close_type == ORDER_TYPE_SELL) ? SYMBOL_BID : SYMBOL_ASK);

   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};

   request.action    = TRADE_ACTION_DEAL;
   request.symbol    = symbol;
   request.volume    = volume;
   request.type      = close_type;
   request.position  = ticket;
   request.price     = price;
   request.deviation = 10;
   request.magic     = g_magic;
   request.comment   = "AurumFlow";
   request.type_time = ORDER_TIME_GTC;
   request.type_filling = ORDER_FILLING_IOC;

   if (OrderSend(request, result))
   {
      Print("Position closed: ticket=", ticket);
      SendPositions();
      SendAccountInfo();
   }
   else
   {
      Print("Close failed: ticket=", ticket, " retcode=", result.retcode);
   }
}

//+------------------------------------------------------------------+
//| Modify SL/TP on a position                                       |
//+------------------------------------------------------------------+
void ExecuteModify(string cmd)
{
   long ticket = (long)ExtractJsonDouble(cmd, "ticket");
   double sl   = ExtractJsonDouble(cmd, "sl");
   double tp   = ExtractJsonDouble(cmd, "tp");

   if (ticket <= 0 || !PositionSelectByTicket(ticket))
   {
      Print("Position not found for modify: ", ticket);
      return;
   }

   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlTradeRequest request = {};
   MqlTradeResult  result  = {};

   request.action   = TRADE_ACTION_SLTP;
   request.position = ticket;
   request.symbol   = symbol;
   request.sl       = (sl > 0) ? NormalizeDouble(sl, digits) : PositionGetDouble(POSITION_SL);
   request.tp       = (tp > 0) ? NormalizeDouble(tp, digits) : PositionGetDouble(POSITION_TP);
   request.magic    = g_magic;

   if (OrderSend(request, result))
   {
      Print("Position modified: ticket=", ticket, " sl=", request.sl, " tp=", request.tp);
   }
   else
   {
      Print("Modify failed: ticket=", ticket, " retcode=", result.retcode);
   }
}

//+------------------------------------------------------------------+
//| Close all open positions                                         |
//+------------------------------------------------------------------+
void CloseAllPositions()
{
   int total = PositionsTotal();
   for (int i = total - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (ticket > 0)
      {
         string cmd = StringFormat("{\"action\":\"CLOSE\",\"ticket\":%lld}", ticket);
         ExecuteClose(cmd);
      }
   }
}

//+------------------------------------------------------------------+
//| Check and update trailing stops                                  |
//+------------------------------------------------------------------+
void CheckTrailingStops()
{
   // Trailing stop logic is handled by Python side.
   // This EA simply executes MODIFY commands when received.
}

//+------------------------------------------------------------------+
//| Simple JSON field extractors                                     |
//+------------------------------------------------------------------+
string ExtractJsonString(string json, string key)
{
   string search = "\"" + key + "\":\"";
   int pos = StringFind(json, search);
   if (pos < 0) return "";

   int start = pos + StringLen(search);
   int end = StringFind(json, "\"", start);
   if (end < 0) return "";

   return StringSubstr(json, start, end - start);
}

double ExtractJsonDouble(string json, string key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if (pos < 0) return -1;

   int start = pos + StringLen(search);

   // Skip optional quotes
   if (StringSubstr(json, start, 1) == "\"") start++;

   // Find end (comma, }, or quote)
   int end = start;
   while (end < StringLen(json))
   {
      ushort c = StringGetCharacter(json, end);
      if (c == ',' || c == '}' || c == '\"') break;
      end++;
   }

   string val_str = StringSubstr(json, start, end - start);
   return StringToDouble(val_str);
}
//+------------------------------------------------------------------+