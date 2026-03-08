# Flight Finder System Flow - Developer Perspective

## Architecture Overview

The system uses a **dual-path architecture** with LangGraph workflow as the preferred method and manual fallback:

```
┌─────────────────────────────────────────────────────────┐
│                    Streamlit UI Layer                    │
│  (main.py - User Interface & Session State Management)   │
└────────────────────┬────────────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         │                       │
    ┌────▼────┐            ┌─────▼─────┐
    │LangGraph│            │   Manual   │
    │ Workflow│            │  Fallback  │
    │(graph.py)│            │(main.py)   │
    └────┬────┘            └─────┬─────┘
         │                        │
         └───────────┬────────────┘
                     │
         ┌───────────▼───────────┐
         │   State Management    │
         │    (state.py)         │
         └───────────────────────┘
```

---

## 1. Initialization Phase (App Startup)

When Streamlit loads `main.py`:

```python
# Lines 575-612: Session state initialization
1. Initialize session state variables:
   - chat_history: [] (conversation messages)
   - conversation_id: UUID (unique session identifier)
   - slots: DEFAULT_SLOTS (flight booking parameters)
   - is_calling_model: False (prevents concurrent requests)
   - last_search_results: None (cached flight results)
   - price_stats: None (price analytics)
   - error_context: None (error recovery data)

2. Try to import LangGraph:
   - If successful: Create flight_graph instance
   - If fails: Set LANGGRAPH_AVAILABLE = False
   - Falls back to manual status handling
```

**Key Files:**
- `main.py` lines 575-612: Session state initialization
- `main.py` lines 607-612: LangGraph initialization

---

## 2. User Input Handling Flow

When a user types a message and hits Enter:

```python
# Line 703: User input detected
if user_input:
    # Line 704-707: Concurrency guard
    if st.session_state.is_calling_model:
        # Prevent multiple simultaneous requests
        st.warning("Processing previous request...")
    else:
        st.session_state.is_calling_model = True
        
        # Line 710: Add user message to history immediately
        st.session_state.chat_history.append({
            "role": "user", 
            "content": user_input
        })
        
        # Line 712-714: Try LangGraph first
        if handle_user_message_with_graph(user_input):
            st.rerun()  # Refresh UI with results
        else:
            # Fall back to manual processing (lines 716-1043)
```

**Key Files:**
- `main.py` lines 703-715: User input handling

---

## 3. Dual-Path Processing

### Path A: LangGraph Workflow (Preferred)

```python
# graph.py - State machine approach
handle_user_message_with_graph(user_input):
    1. Create initial_state from session_state
    2. Invoke flight_graph.invoke(initial_state)
    3. Graph executes nodes based on status:
       
       Entry Point: parse_llm_response
       ├─ Calls Mistral LLM with conversation history
       ├─ Extracts JSON (status, slots, message)
       ├─ Updates state["status"]
       └─ Routes to next node based on status:
          
          Status: "clarification_needed"
          └─> handle_clarification
              └─> Adds message to chat_history
              └─> END
          
          Status: "update"
          └─> handle_update
              └─> Updates slots, adds message
              └─> END
          
          Status: "ready_for_search"
          └─> handle_ready_for_search
              ├─ Validates slots
              ├─ Calls search_flights_api()
              ├─ Calculates price_stats
              ├─ Formats results
              └─> END
          
          Status: "refining_search"
          └─> handle_refining_search
              ├─ Determines refinement type (price/nearby/flexible)
              ├─ Performs refined search
              └─> END
          
          Status: "awaiting_confirmation"
          └─> handle_awaiting_confirmation
              ├─ Resolves airport codes
              ├─ Suggests alternatives
              └─> END
     
    4. Update session_state from final_state
    5. Return True (success)
```

**Key Files:**
- `graph.py`: LangGraph workflow definition
- `main.py` lines 614-656: `handle_user_message_with_graph()` function

### Path B: Manual Fallback (If LangGraph Unavailable)

```python
# main.py lines 716-1043 - Imperative if/elif/else approach
1. Build conversation history (last MAX_HISTORY messages)
2. Call Mistral LLM with:
   - System prompt
   - Conversation history
   - Current slots as context
   - User's new message

3. Parse LLM response:
   - Extract conversational_message (for UI)
   - Extract json_data (status, slots, missing_slots)

4. Route based on status (lines 796-1041):
   
   if status == "ready_for_search":
       ├─ Validate slots
       ├─ If invalid → error message
       ├─ If valid → search_flights_api()
       ├─ Handle results (success/error/empty)
       └─ Format and add to chat_history
   
   elif status == "refining_search":
       ├─ Determine refinement type
       ├─ Execute refinement logic
       └─ Format and add to chat_history
   
   elif status == "awaiting_confirmation":
       ├─ Resolve airport codes
       ├─ Suggest alternatives
       └─ Add to chat_history
   
   else:  # clarification_needed, update, error
       └─ Just add conversational message to chat_history

5. st.rerun() - Refresh UI
```

**Key Files:**
- `main.py` lines 716-1043: Manual status handling

---

## 4. Status-Based Routing Logic

The LLM returns a `status` field that determines the workflow:

| Status | Meaning | Action |
|--------|---------|--------|
| `clarification_needed` | Missing required info | Ask user for clarification |
| `update` | Slots updated, not ready | Confirm update, continue conversation |
| `ready_for_search` | All required slots filled | Validate → Search API → Display results |
| `refining_search` | User wants to refine | Apply filters (price/nearby/flexible dates) |
| `awaiting_confirmation` | Ambiguous input | Suggest alternatives, resolve conflicts |
| `error` | Parsing/validation failed | Show error, suggest recovery |

**Key Files:**
- `graph.py` lines 23-78: `parse_llm_response()` - extracts status
- `graph.py` lines 320-345: `route_status()` - routes based on status
- `main.py` lines 796-1041: Manual status routing

---

## 5. State Management

### Session State (Streamlit)
- **Persists across reruns** - maintains conversation context
- **Thread-safe per user session** - each browser tab = separate session
- Stores:
  - `chat_history`: List of conversation messages
  - `slots`: Current booking parameters
  - `last_search_results`: Cached flight results
  - `price_stats`: Price analytics
  - `error_context`: Error recovery data

### FlightState (LangGraph)
- **TypedDict** passed between nodes
- **Immutable updates** - nodes return new state
- `chat_history` uses `Annotated[List, operator.add]` for accumulation
- Contains all information needed across the conversation

### State Synchronization
```python
# LangGraph path:
session_state → initial_state → graph processing → final_state → session_state

# Manual path:
session_state → direct updates → session_state
```

**Key Files:**
- `state.py`: FlightState, FlightSlots, FlightResult, PriceStats definitions
- `main.py` lines 575-612: Session state initialization
- `main.py` lines 622-638: State conversion for LangGraph

---

## 6. LLM Interaction Pattern

```python
# System Prompt (not shown in snippet, but referenced)
- Instructs LLM to be conversational
- Requires structured JSON output
- Format: <conversational_message>...</conversational_message>
         <json_data>{"status": "...", "slots": {...}}</json_data>

# Request Structure:
{
  "model": "mistral-medium-latest",
  "messages": [
    {"role": "system", "content": SYSTEM_PROMPT},
    ...conversation_history...,
    {"role": "user", "content": "[Current booking state: {...}]\n\nUser: {user_input}"}
  ],
  "temperature": 0.7,
  "max_tokens": 1500
}

# Response Parsing:
1. Extract conversational_message (for UI display)
2. Extract json_data (for status/slots)
3. Clean XML tags (multiple regex passes)
4. Fallback parsing if structured format fails
```

**Key Files:**
- `main.py` lines 47-79: JSON extraction utilities
- `main.py` lines 81-110: Conversational message extraction
- `main.py` lines 737-752: LLM API call
- `graph.py` lines 23-78: LLM parsing in LangGraph

---

## 7. Error Handling and Recovery

```python
# Multiple layers of error handling:

1. API Call Level (call_mistral_with_backoff):
   - Exponential backoff retry (4 attempts)
   - Handles rate limits, network errors

2. Parsing Level:
   - Try structured XML extraction
   - Fallback to regex JSON extraction
   - Last resort: use raw reply as message

3. Validation Level:
   - validate_slots() checks required fields
   - Returns error_details for specific issues

4. API Search Level:
   - search_flights_api() returns (flights, error, error_details)
   - Handles API failures gracefully
   - Suggests alternatives on failure

5. Exception Level:
   - Try/except around entire processing
   - Logs error, shows user-friendly message
   - Always resets is_calling_model flag
```

**Key Files:**
- `main.py` lines 112-150: `call_mistral_with_backoff()` - retry logic
- `main.py` lines 762-777: Parsing fallback logic
- `main.py` lines 1045-1054: Exception handling

---

## 8. UI Rendering Flow

```python
# Lines 689-695: Display conversation history
for msg in st.session_state.chat_history:
    if msg["role"] == "user":
        st.chat_message("user").write(msg["content"])
    else:
        st.chat_message("assistant").write(msg["content"])

# Line 698: User input widget
user_input = st.chat_input("Type your message here...")

# Rerun triggers:
- After LangGraph processing (line 714)
- After manual processing (line 1043)
- After error handling (line 1052)
- After "New Chat" button (line 675)
```

**Key Files:**
- `main.py` lines 661-683: UI header and booking details
- `main.py` lines 686-698: Chat interface rendering

---

## 9. Design Patterns Used

1. **State Machine Pattern**: LangGraph workflow with status-based routing
2. **Fallback Pattern**: LangGraph → Manual handling
3. **Guard Pattern**: `is_calling_model` prevents concurrent requests
4. **Accumulator Pattern**: `chat_history` accumulates messages
5. **Strategy Pattern**: Different workflows for different statuses
6. **Retry Pattern**: Exponential backoff for API calls

---

## 10. Data Flow Summary

```
User Input
    ↓
Add to chat_history
    ↓
┌─────────────────┐
│  LangGraph?     │
└───┬─────────┬───┘
    │ YES     │ NO
    ↓         ↓
LangGraph   Manual
Workflow    Processing
    ↓         ↓
Parse LLM   Parse LLM
Response    Response
    ↓         ↓
Route by    Route by
Status      Status
    ↓         ↓
Execute     Execute
Workflow    Workflow
    ↓         ↓
Update      Update
Session     Session
State       State
    ↓         ↓
Rerun UI    Rerun UI
```

---

## 11. Key Components Breakdown

### main.py
- **Lines 1-29**: Imports and configuration
- **Lines 31-38**: Constants (MODEL_NAME, MAX_HISTORY, etc.)
- **Lines 40-42**: Mistral client initialization
- **Lines 45-150**: Utility functions (JSON parsing, API calls, backoff)
- **Lines 152-537**: Helper functions (validation, search, formatting)
- **Lines 573-612**: Session state initialization
- **Lines 614-656**: LangGraph handler
- **Lines 658-698**: UI rendering
- **Lines 700-1054**: User input processing (dual-path)

### graph.py
- **Lines 23-78**: `parse_llm_response()` - Entry point, calls LLM
- **Lines 80-84**: `handle_clarification()` - Clarification workflow
- **Lines 86-90**: `handle_update()` - Update workflow
- **Lines 92-170**: `handle_ready_for_search()` - Search workflow
- **Lines 172-284**: `handle_refining_search()` - Refinement workflow
- **Lines 286-318**: `handle_awaiting_confirmation()` - Confirmation workflow
- **Lines 320-345**: `route_status()` - Status-based routing
- **Lines 347-385**: `create_flight_finder_graph()` - Graph construction

### state.py
- **Lines 6-22**: `FlightSlots` - Pydantic model for booking slots
- **Lines 24-31**: `FlightResult` - Pydantic model for flight results
- **Lines 33-37**: `PriceStats` - Pydantic model for price statistics
- **Lines 39-72**: `FlightState` - TypedDict for LangGraph state

---

## 12. Workflow Execution Details

### Workflow 1: Ready for Search
```python
1. Validate slots (origin, destination, date required)
2. If invalid → error message with details
3. If valid:
   a. Call search_flights_api()
   b. Store results in session_state
   c. Calculate price_stats (min/max/avg)
   d. Format results for display
   e. Handle edge cases (empty results, API errors)
```

### Workflow 2: Refining Search
```python
1. Determine refinement type:
   - Price filter (cheaper/budget)
   - Nearby airports
   - Flexible dates
2. Execute refinement:
   - Price: Filter existing + re-search with threshold
   - Nearby: Search multiple airports
   - Flexible: Search date range
3. Format and display refined results
```

### Workflow 3: Awaiting Confirmation
```python
1. Check for ambiguous city names
2. Resolve airport codes
3. Suggest alternatives if needed
4. Handle error context
5. Display confirmation message
```

---

## 13. Key Takeaways for Developers

1. **Dual-Path Architecture**: LangGraph preferred, manual fallback ensures reliability
2. **Status-Driven Routing**: LLM determines workflow via status field
3. **State Persistence**: Session state maintains conversation context
4. **Error Resilience**: Multiple error handling layers at different levels
5. **User Experience**: Conversational UI with structured data extraction
6. **Scalability**: LangGraph enables complex workflows, manual path is simpler

---

## 14. Debugging Tips

1. **Check LangGraph availability**: Look for "Warning: LangGraph not available" in console
2. **Inspect session state**: Use `st.json(st.session_state.slots)` in expander
3. **Monitor status transitions**: Check `status` field in LLM responses
4. **Check error context**: `st.session_state.error_context` contains validation errors
5. **View raw LLM response**: Check console logs for parsing issues

---

## 15. Future Enhancements

- [ ] Add logging framework for better debugging
- [ ] Implement conversation persistence (database)
- [ ] Add unit tests for workflow nodes
- [ ] Implement caching for API responses
- [ ] Add metrics/analytics tracking
- [ ] Support multi-language conversations
- [ ] Add voice input/output support

