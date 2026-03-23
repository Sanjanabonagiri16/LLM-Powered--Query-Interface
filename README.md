# O2C Graph-Based Business Data Explorer

This project builds a dataset-grounded order-to-cash graph explorer with:

- Data integration + graph data model over SAP O2C tables
- Interactive graph visualization UI (node expansion + relationship browsing + metadata)
- Natural language chat interface mapped to SQL query execution and streaming responses
- Guardrails for out-of-domain questions

## Tech Stack

- Backend: FastAPI + SQLite
- Data ingestion: Python script over JSONL dataset
- Frontend: plain HTML/JS + Cytoscape.js
- LLM: OpenRouter-compatible API (optional; fallback templates included)

## Architecture and Tradeoffs

### Why a hybrid relational + graph design

- **Relational (SQLite) for query reliability:** business questions like top billed products and broken flows are easier to answer with SQL joins and aggregates.
- **Graph projection for exploration UX:** interactive node/edge traversal is more intuitive for business flow investigation than only tabular output.
- **Single-source persistence:** graph nodes/edges are materialized into SQLite tables (`graph_nodes`, `graph_edges`) to avoid running multiple databases in this assignment.

### Database choice rationale

- **Chosen:** SQLite
- **Why:** zero-infra setup, deterministic local runtime, great for assignment/demo reproducibility, and strong SQL support for NL->SQL execution.
- **Tradeoff:** SQLite is not horizontally scalable for high-concurrency production workloads; a production path could move to Postgres + graph extension or a dedicated graph store.

### LLM prompting strategy

- System instruction enforces strict SAP O2C scope.
- Prompt provides concise schema map and query examples.
- Model output is constrained to strict JSON (`mode`, `sql`, `answer_hint`).
- LLM SQL is validated by guardrails; deterministic templates are used when LLM is unavailable or SQL fails safety checks.

### Response grounding and anti-hallucination

- Every final answer is based on rows returned from executed SQL.
- SQL guardrail blocks non-SELECT and disallowed tables.
- Off-topic questions are rejected with a fixed domain message.
- Fallback summaries are deterministic; optional LLM answer synthesis only summarizes returned rows.

### Error handling and fallback behavior

- Missing/invalid DB -> API returns actionable error (`run scripts/ingest.py`).
- LLM/API failure -> deterministic SQL path.
- Unsafe SQL from model -> rejected and optionally replaced by deterministic query.
- Trace/doc not found -> clear no-path message.

## Project Structure

- `scripts/ingest.py` - loads dataset and builds graph tables
- `backend/app.py` - API for graph, chat, streaming, entity search, memory
- `frontend/index.html` - graph explorer and chat UI
- `data/o2c_graph.db` - generated SQLite database

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Optional: configure LLM for NL->SQL generation:

   ```bash
   cp .env.example .env
   # fill LLM_API_KEY
   ```

4. Build integrated database + graph:

   ```bash
   python scripts/ingest.py
   ```

5. Run API + UI:

   ```bash
   uvicorn backend.app:app --reload
   ```

6. Open `http://127.0.0.1:8000`

### One-command local launch

```bash
./scripts/run_local.sh
```

This script creates/uses `.venv`, installs dependencies, rebuilds the integrated dataset DB, and starts the FastAPI app.

## Deployment (Render - Recommended)

1. Push this project to a public GitHub repository.
2. Go to [Render](https://render.com/) and create a new **Web Service** from that repository.
3. Render will detect `render.yaml` automatically. If prompted manually, use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `./scripts/start_server.sh`
4. Deploy and copy the generated public URL.

Notes:

- On startup, `scripts/start_server.sh` rebuilds the integrated SQLite DB from both dataset folders.
- No authentication is required by default.

## What is Modeled

Node types:

- Customer
- SalesOrder
- SalesOrderItem
- Product
- Delivery
- DeliveryItem
- BillingDocument
- BillingItem
- JournalEntry
- Payment
- Plant
- Address

Edge types:

- `PLACED_ORDER`
- `HAS_ITEM`
- `ITEM_FOR_PRODUCT`
- `FULFILLED_BY_DELIVERY`
- `BILLED_AS`
- `POSTED_TO_JOURNAL`
- `CLEARED_BY_PAYMENT`
- `DELIVERED_FROM_PLANT`
- `HAS_ADDRESS`
- `LOCATED_AT_ADDRESS`

## Additional Capabilities

- Streaming endpoint: `POST /api/chat/stream`
- Conversation memory per session: `session_id` + reset endpoint
- Hybrid entity search: `POST /api/search/entities`
- Result-linked node highlighting in graph UI
- Auto-expand and focus on highlighted flow nodes in graph UI
- Trace path mode by billing document + one-click trace from selected billing node
- Graph relation browser endpoint: `GET /api/graph/relations`
- Dataset per-table/source stats endpoint: `GET /api/dataset/stats`
- Query audit logs with export:
  - `GET /api/audit/recent`
  - `GET /api/audit/export`

## Guardrails

- Rejects non-domain prompts (weather, creative writing, etc.)
- SQL safety filter blocks non-SELECT and disallowed tables
- Every answer is generated from query results on SQLite data

## Example Questions

- Which products are associated with the highest number of billing documents?
- Trace the full flow of billing document `9000001234`.
- Identify sales orders delivered but not billed.

## Interface Test Questions

Use these prompts directly in the chat panel to validate interface behavior and data-grounded responses.

### Required core prompts

- `Which products are associated with the highest number of billing documents?`
- `Trace the full flow for billing document 91150187`
- `Identify sales orders that have broken or incomplete flows`

### Additional business prompts

- `Show me top 10 sales orders by total net amount`
- `Show outbound deliveries`
- `Show journal entries linked to billing document 91150187`
- `Show payments linked to billing document 91150187`

### Guardrail prompts (should be rejected)

- `Write a poem about space`
- `Tell me a joke`
- `Who won the cricket world cup?`

### UI interaction checks

- Use `Billing Doc` + `Trace Path` with `91150187`
- Select a billing node and click `Trace Selected`
- Click `Exit` to restore full graph
- Click `View Stats` and verify per-table/source counts
- Click `Export Stats` and `Export Logs` to verify downloads

