# Flight Finder (GarudaX)

Conversational flight search: natural-language input, slot extraction via LLM, and real flight search with optional refinement (price, nearby airports, flexible dates). Uses **LangGraph** for the workflow when available, with a manual fallback.

## Structure

```
flightFinder/
├── src/
│   ├── app.py           # Streamlit UI (entry point)
│   ├── config.py        # Env, constants, prompts
│   ├── state.py         # Pydantic/TypedDict state models
│   ├── services/
│   │   └── flight_services.py   # LLM, search, validation, formatting
│   └── graph/
│       └── workflow.py  # LangGraph workflow
├── tests/
│   ├── test_basic.py
│   ├── test_flight_search.py
│   └── graph_usage_example.py
├── scripts/
│   └── amadeus_token.py  # Optional Amadeus API script
├── docs/                 # WORKFLOW_DIAGRAM.md, DEVELOPER_FLOW.md, info.md
├── .env.example
├── requirements.txt
└── README.md
```

## Setup

1. **Clone and create venv**

   ```bash
   cd flightFinder
   python3 -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Environment**

   Copy `.env.example` to `.env` and set at least:

   - `MISTRAL_API_KEY` – required for the conversational LLM
   - `RAPIDAPI_KEY` and `RAPIDAPI_HOST` – for real flight search (e.g. booking-com15)

   Without flight API keys, the app falls back to mock results.

## Run

**Streamlit app (from project root):**

```bash
streamlit run src/app.py
```

**Basic test (no APIs):**

```bash
python -m tests.test_basic
```

**Flight search test (uses .env):**

```bash
python -m tests.test_flight_search
```

## Deploy

- Run from project root so `src` and `.env` resolve correctly.
- For production, set env vars in your host (e.g. Streamlit Cloud, Railway) and do **not** commit `.env`.
- Entry point: `streamlit run src/app.py` (or `python -m src.app` if you add `if __name__ == "__main__": main()` and run as module).

## Conventions

- **Naming:** `snake_case` for modules and functions, `PascalCase` for classes.
- **Config:** All env and constants in `src/config.py`.
- **State:** `src/state.py` for LangGraph and app state models.
- **Services:** `src/services/flight_services.py` has no Streamlit imports so it can be used from the graph and CLI.
- **Tests / examples:** In `tests/`; no production code in `tests/`.
