┌─────────────────────────────────────────────────────────────┐
│  USER SENDS MESSAGE                                         │
│  "hyd to del flight on 23 dec 2025"                        │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 1: Add user message to chat_history                  │
│  chat_history.append({"role": "user", "content": "..."})   │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 2: Build LLM payload with:                           │
│  - System prompt (defines status rules)                     │
│  - Conversation history (last 10 messages)                  │
│  - Current slots state (as context)                         │
│  - User's new message                                       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 3: Call Mistral API                                   │
│  response = call_mistral_with_backoff(payload)              │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 4: LLM returns response with:                        │
│  <conversational_message>                                   │
│    "Got it! You're looking for flights..."                  │
│  </conversational_message>                                  │
│                                                              │
│  <json_data>                                                │
│    {                                                         │
│      "status": "ready_for_search",  ← LLM SETS THIS       │
│      "slots": {...},                                        │
│      "missing_slots": []                                    │
│    }                                                         │
│  </json_data>                                               │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 5: Extract and parse                                 │
│  - conversational_msg = extract_conversational_message()   │
│  - json_data = extract_json_from_response()                │
│  - status = json_data.get("status")  ← EXTRACT STATUS       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 6: Update slots in session state                      │
│  st.session_state.slots = json_data["slots"]                │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  STEP 7: Check status value                                 │
│  status = json_data.get("status")                            │
│                                                              │
│  ┌──────────────────────────────────────────┐              │
│  │  if status == "ready_for_search":        │              │
│  │    → Trigger flight search               │              │
│  │    → Combine message + results           │              │
│  │    → Add to chat_history                 │              │
│  │                                          │              │
│  │  else:                                   │              │
│  │    → Just add conversational message     │              │
│  │    → Wait for more info from user        │              │
│  └──────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────┘