import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "o2c_graph.db"
FRONTEND_DIR = ROOT / "frontend"
MAX_CHAT_HISTORY = 30

SYSTEM_GUARDRAIL = (
    "You are an analyst for SAP order-to-cash dataset only. "
    "Reject and refuse unrelated topics."
)
OFF_TOPIC_MSG = "This system is designed to answer questions related to the provided SAP order-to-cash dataset only."

ALLOWED_TABLES = {
    "sales_order_headers",
    "sales_order_items",
    "outbound_delivery_headers",
    "outbound_delivery_items",
    "billing_document_headers",
    "billing_document_items",
    "journal_entry_items_accounts_receivable",
    "payments_accounts_receivable",
    "products",
    "product_descriptions",
    "business_partners",
    "business_partner_addresses",
    "plants",
    "graph_nodes",
    "graph_edges",
}

BLOCKED_QUERY_TERMS = [
    "poem",
    "story",
    "recipe",
    "weather",
    "movie",
    "sports",
    "capital of",
    "general knowledge",
    "joke",
    "politics",
    "fiction",
]

MEMORY: dict[str, list[dict[str, str]]] = {}


class ChatRequest(BaseModel):
    question: str
    history: list[dict[str, str]] = []
    session_id: str = "default"
    stream: bool = False


class SearchRequest(BaseModel):
    query: str
    limit: int = 25


def ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS query_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            session_id TEXT,
            question TEXT,
            mode TEXT,
            sql_text TEXT,
            row_count INTEGER,
            answer TEXT,
            guardrail_blocked INTEGER,
            latency_ms INTEGER,
            error_text TEXT
        )
        """
    )


def write_audit_log(
    session_id: str,
    question: str,
    mode: str,
    sql_text: str | None,
    row_count: int,
    answer: str,
    guardrail_blocked: bool,
    latency_ms: int,
    error_text: str = "",
) -> None:
    with get_conn() as conn:
        ensure_audit_table(conn)
        conn.execute(
            """
            INSERT INTO query_audit_logs(session_id, question, mode, sql_text, row_count, answer, guardrail_blocked, latency_ms, error_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                question,
                mode,
                sql_text or "",
                row_count,
                answer,
                1 if guardrail_blocked else 0,
                latency_ms,
                error_text,
            ),
        )
        conn.commit()


def get_conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="Database not found. Run: python scripts/ingest.py")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().strip())


def is_in_domain(question: str) -> bool:
    q = normalize_text(question)
    if any(term in q for term in BLOCKED_QUERY_TERMS):
        return False
    domain_terms = [
        "sales",
        "order",
        "delivery",
        "billing",
        "invoice",
        "payment",
        "customer",
        "product",
        "journal",
        "document",
        "flow",
        "material",
        "sap",
        "o2c",
        "plant",
        "address",
    ]
    return any(t in q for t in domain_terms) or bool(re.search(r"\b\d{6,}\b", q))


def blocked_sql(sql: str) -> str | None:
    if ";" in sql.strip().rstrip(";"):
        return "Only one SQL statement is allowed."
    s = sql.lower().strip()
    if not s.startswith("select"):
        return "Only SELECT queries are allowed."
    disallowed = ["insert ", "update ", "delete ", "drop ", "alter ", "create ", "pragma ", "attach ", "detach "]
    if any(tok in s for tok in disallowed):
        return "Unsafe SQL operation detected."
    pattern = r"\bfrom\s+([a-zA-Z_][a-zA-Z0-9_]*)|\bjoin\s+([a-zA-Z_][a-zA-Z0-9_]*)"
    for table in re.findall(pattern, s):
        t = (table[0] or table[1]).lower()
        if t and t not in ALLOWED_TABLES:
            return f"Table '{t}' is not allowed."
    return None


def json_from_llm_text(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in LLM response.")
    return json.loads(content[start : end + 1])


def schema_prompt() -> str:
    return """
Tables:
- sales_order_headers(salesOrder, soldToParty, totalNetAmount, creationDate)
- sales_order_items(salesOrder, salesOrderItem, material, netAmount, productionPlant, storageLocation)
- outbound_delivery_headers(deliveryDocument, overallGoodsMovementStatus)
- outbound_delivery_items(deliveryDocument, deliveryDocumentItem, referenceSdDocument, referenceSdDocumentItem, plant, storageLocation)
- billing_document_headers(billingDocument, fiscalYear, accountingDocument, totalNetAmount)
- billing_document_items(billingDocument, billingDocumentItem, material, netAmount, referenceSdDocument, referenceSdDocumentItem)
- journal_entry_items_accounts_receivable(accountingDocument, companyCode, fiscalYear, referenceDocument, amountInTransactionCurrency, transactionCurrency)
- payments_accounts_receivable(accountingDocument, accountingDocumentItem, clearingAccountingDocument, customer, amountInTransactionCurrency, transactionCurrency)
- products(product, productType)
- product_descriptions(product, language, productDescription)
- business_partners(businessPartner, customer, businessPartnerName)
- business_partner_addresses(businessPartner, addressId, cityName, country, streetName, postalCode)
- plants(plant, plantName, addressId)
- graph_nodes(node_id, node_type, label, metadata)
- graph_edges(source_id, target_id, relation, metadata)
"""


def call_llm_for_sql(question: str, history: list[dict[str, str]]) -> dict[str, Any]:
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
    model = os.getenv("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    if not api_key:
        return {"mode": "fallback", "sql": None, "reasoning": "LLM key not configured; using deterministic query templates."}

    examples = """
Examples:
Q: Which products are associated with the highest number of billing documents?
A SQL:
SELECT bi.material AS product, COUNT(DISTINCT bi.billingDocument) AS billing_docs
FROM billing_document_items bi
GROUP BY bi.material
ORDER BY billing_docs DESC
LIMIT 10

Q: Find sales orders delivered but not billed.
A SQL:
SELECT DISTINCT soi.salesOrder
FROM sales_order_items soi
JOIN outbound_delivery_items odi
  ON odi.referenceSdDocument = soi.salesOrder AND odi.referenceSdDocumentItem = soi.salesOrderItem
LEFT JOIN billing_document_items bdi
  ON bdi.referenceSdDocument = odi.deliveryDocument AND bdi.referenceSdDocumentItem = odi.deliveryDocumentItem
WHERE bdi.billingDocument IS NULL
LIMIT 200
"""
    prompt = (
        "Generate a SINGLE SQLite SELECT query for this SAP O2C question.\n"
        "Return strict JSON: {\"mode\":\"sql|reject\",\"sql\":\"...\",\"answer_hint\":\"...\"}\n"
        "If unrelated to SAP O2C data, return mode=reject.\n"
        "Never include markdown.\n"
        f"{schema_prompt()}\n"
        f"{examples}\n"
        f"Recent chat context: {json.dumps(history[-5:])}\n"
        f"Question: {question}"
    )
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM_GUARDRAIL}, {"role": "user", "content": prompt}], "temperature": 0.0}
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        return json_from_llm_text(content)
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError, ValueError):
        return {"mode": "fallback", "sql": None, "reasoning": "LLM call failed, fallback mode."}


def deterministic_query(question: str) -> dict[str, Any]:
    q = normalize_text(question)
    if "highest number of billing" in q or "highest billed products" in q or "highest billing documents" in q:
        return {
            "sql": """
SELECT bi.material AS product, COUNT(DISTINCT bi.billingDocument) AS billing_docs
FROM billing_document_items bi
GROUP BY bi.material
ORDER BY billing_docs DESC
LIMIT 10
""".strip(),
            "hint": "Top products by number of billing documents.",
            "highlights_sql": "SELECT DISTINCT 'P:' || bi.material AS node_id FROM billing_document_items bi GROUP BY bi.material ORDER BY COUNT(DISTINCT bi.billingDocument) DESC LIMIT 10",
        }

    if "trace" in q and ("billing document" in q or "billing" in q):
        doc = re.search(r"\b\d{6,}\b", question)
        if not doc:
            return {"sql": "", "hint": "Please provide a billing document number (e.g. 9150187).", "highlights_sql": ""}
        billing_doc = doc.group(0)
        return {
            "sql": f"""
SELECT
  bdi.billingDocument,
  soi.salesOrder,
  odi.deliveryDocument,
  bdh.accountingDocument AS journalReference,
  je.accountingDocument AS journalEntry,
  je.companyCode,
  je.fiscalYear
FROM billing_document_items bdi
LEFT JOIN outbound_delivery_items odi
  ON odi.deliveryDocument = bdi.referenceSdDocument
LEFT JOIN sales_order_items soi
  ON soi.salesOrder = odi.referenceSdDocument AND soi.salesOrderItem = odi.referenceSdDocumentItem
LEFT JOIN billing_document_headers bdh
  ON bdh.billingDocument = bdi.billingDocument
LEFT JOIN journal_entry_items_accounts_receivable je
  ON je.referenceDocument = bdi.billingDocument
WHERE bdi.billingDocument = '{billing_doc}'
LIMIT 100
""".strip(),
            "hint": f"End-to-end trace for billing document {billing_doc}.",
            "highlights_sql": f"""
SELECT 'B:{billing_doc}' AS node_id
UNION
SELECT DISTINCT 'D:' || odi.deliveryDocument FROM outbound_delivery_items odi
JOIN billing_document_items bdi ON bdi.referenceSdDocument = odi.deliveryDocument
WHERE bdi.billingDocument = '{billing_doc}'
UNION
SELECT DISTINCT 'SO:' || soi.salesOrder FROM sales_order_items soi
JOIN outbound_delivery_items odi ON odi.referenceSdDocument = soi.salesOrder AND odi.referenceSdDocumentItem = soi.salesOrderItem
JOIN billing_document_items bdi ON bdi.referenceSdDocument = odi.deliveryDocument
WHERE bdi.billingDocument = '{billing_doc}'
""".strip(),
        }

    if "broken" in q or "incomplete flow" in q or "delivered but not billed" in q or "billed without delivery" in q:
        return {
            "sql": """
SELECT issue_type, document_id
FROM (
  SELECT DISTINCT 'DELIVERED_NOT_BILLED' AS issue_type, odi.deliveryDocument AS document_id
  FROM outbound_delivery_items odi
  LEFT JOIN billing_document_items bdi
    ON bdi.referenceSdDocument = odi.deliveryDocument
  WHERE bdi.billingDocument IS NULL
  UNION ALL
  SELECT DISTINCT 'BILLED_WITHOUT_DELIVERY' AS issue_type, bdi.billingDocument AS document_id
  FROM billing_document_items bdi
  LEFT JOIN outbound_delivery_items odi
    ON odi.deliveryDocument = bdi.referenceSdDocument
  WHERE odi.deliveryDocument IS NULL
)
LIMIT 250
""".strip(),
            "hint": "Detected incomplete order-to-cash flow records.",
            "highlights_sql": "",
        }

    return {
        "sql": """
SELECT soh.salesOrder, soh.soldToParty, soh.totalNetAmount
FROM sales_order_headers soh
ORDER BY CAST(soh.totalNetAmount AS REAL) DESC
LIMIT 10
""".strip(),
        "hint": "Showing top sales orders by net amount.",
        "highlights_sql": "SELECT 'SO:' || salesOrder AS node_id FROM sales_order_headers ORDER BY CAST(totalNetAmount AS REAL) DESC LIMIT 10",
    }


def summarise_answer(question: str, rows: list[dict[str, Any]], hint: str) -> str:
    if not rows:
        return f"{hint} No matching records were found."
    if "highest" in question.lower() and ("billing" in question.lower() or "billed" in question.lower()):
        top = rows[0]
        return f"{hint} Top product is {top.get('product')} with {top.get('billing_docs')} billing documents."
    return f"{hint} Returned {len(rows)} row(s) from dataset."


def call_llm_for_answer(question: str, hint: str, rows: list[dict[str, Any]]) -> str | None:
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
    model = os.getenv("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    if not api_key or not rows:
        return None
    preview = rows[:12]
    prompt = (
        "Summarize dataset-backed SQL result in 2-4 concise sentences.\n"
        "Do not invent facts. Mention key counts and top records when relevant.\n"
        f"Question: {question}\n"
        f"Hint: {hint}\n"
        f"Rows: {json.dumps(preview)}\n"
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_GUARDRAIL}, {"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"].strip()
        return text if text else None
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError):
        return None


def better_fallback_summary(question: str, rows: list[dict[str, Any]], hint: str) -> str:
    q = normalize_text(question)
    if not rows:
        return f"{hint} No matching records were found."
    if "incomplete" in q or "broken" in q:
        delivered_not_billed = sum(1 for r in rows if r.get("issue_type") == "DELIVERED_NOT_BILLED")
        billed_without_delivery = sum(1 for r in rows if r.get("issue_type") == "BILLED_WITHOUT_DELIVERY")
        return (
            f"{hint} Found {len(rows)} problematic documents: "
            f"{delivered_not_billed} delivered-not-billed and {billed_without_delivery} billed-without-delivery."
        )
    if "trace" in q and "billing" in q:
        sample = rows[0]
        return (
            f"{hint} Trace indicates sales order {sample.get('salesOrder') or 'N/A'}, "
            f"delivery {sample.get('deliveryDocument') or 'N/A'}, and journal entry {sample.get('journalEntry') or 'N/A'}."
        )
    return summarise_answer(question, rows, hint)


def fetch_highlights(conn: sqlite3.Connection, highlights_sql: str, rows: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    if highlights_sql:
        try:
            results = conn.execute(highlights_sql).fetchall()
            ids.extend([r[0] for r in results if r and r[0]])
        except sqlite3.Error:
            pass
    for row in rows[:15]:
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            if key.lower().endswith("salesorder"):
                ids.append(f"SO:{value}")
            if key.lower().endswith("deliverydocument"):
                ids.append(f"D:{value}")
            if key.lower().endswith("billingdocument"):
                ids.append(f"B:{value}")
            if key.lower() == "product" or key.lower() == "material":
                ids.append(f"P:{value}")
    # preserve order unique
    seen = set()
    out = []
    for node_id in ids:
        if node_id not in seen:
            seen.add(node_id)
            out.append(node_id)
    return out[:60]


def run_chat_logic(req: ChatRequest) -> dict[str, Any]:
    t0 = time.time()
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    history = MEMORY.get(req.session_id, [])
    merged_history = (history + req.history)[-MAX_CHAT_HISTORY:]
    if not is_in_domain(question):
        payload = {"answer": OFF_TOPIC_MSG, "mode": "guardrail", "sql": None, "rows": [], "highlight_node_ids": []}
        MEMORY[req.session_id] = (merged_history + [{"role": "user", "content": question}, {"role": "assistant", "content": OFF_TOPIC_MSG}])[-MAX_CHAT_HISTORY:]
        write_audit_log(
            session_id=req.session_id,
            question=question,
            mode="guardrail",
            sql_text=None,
            row_count=0,
            answer=OFF_TOPIC_MSG,
            guardrail_blocked=True,
            latency_ms=int((time.time() - t0) * 1000),
        )
        return payload

    det = deterministic_query(question)
    llm_sql_plan = call_llm_for_sql(question, merged_history)
    if llm_sql_plan.get("mode") == "reject":
        write_audit_log(
            session_id=req.session_id,
            question=question,
            mode="guardrail",
            sql_text=None,
            row_count=0,
            answer=OFF_TOPIC_MSG,
            guardrail_blocked=True,
            latency_ms=int((time.time() - t0) * 1000),
        )
        return {"answer": OFF_TOPIC_MSG, "mode": "guardrail", "sql": None, "rows": [], "highlight_node_ids": []}

    if llm_sql_plan.get("mode") == "sql" and llm_sql_plan.get("sql"):
        sql = str(llm_sql_plan["sql"]).strip()
        hint = str(llm_sql_plan.get("answer_hint", "Query results from dataset."))
        highlights_sql = ""
        mode = "llm-sql"
    else:
        sql = det["sql"]
        hint = det["hint"]
        highlights_sql = det.get("highlights_sql", "")
        mode = "deterministic-sql"

    if not sql:
        write_audit_log(
            session_id=req.session_id,
            question=question,
            mode=mode,
            sql_text=None,
            row_count=0,
            answer=hint,
            guardrail_blocked=False,
            latency_ms=int((time.time() - t0) * 1000),
        )
        return {"answer": hint, "mode": mode, "sql": None, "rows": [], "highlight_node_ids": []}

    reason = blocked_sql(sql)
    if reason:
        # fallback to deterministic if model SQL is blocked
        if mode == "llm-sql":
            sql = det["sql"]
            hint = det["hint"]
            highlights_sql = det.get("highlights_sql", "")
            mode = "fallback-after-guardrail"
            reason = blocked_sql(sql)
        if reason:
            write_audit_log(
                session_id=req.session_id,
                question=question,
                mode="sql-guardrail-error",
                sql_text=sql,
                row_count=0,
                answer="SQL rejected by guardrail.",
                guardrail_blocked=True,
                latency_ms=int((time.time() - t0) * 1000),
                error_text=reason,
            )
            raise HTTPException(status_code=400, detail=f"SQL rejected by guardrail: {reason}")

    with get_conn() as conn:
        rows = conn.execute(sql).fetchmany(250)
        data = rows_to_dicts(rows)
        highlight_node_ids = fetch_highlights(conn, highlights_sql, data)
    answer = call_llm_for_answer(question, hint, data) or better_fallback_summary(question, data, hint)
    MEMORY[req.session_id] = (merged_history + [{"role": "user", "content": question}, {"role": "assistant", "content": answer}])[-MAX_CHAT_HISTORY:]
    write_audit_log(
        session_id=req.session_id,
        question=question,
        mode=mode,
        sql_text=sql,
        row_count=len(data),
        answer=answer,
        guardrail_blocked=False,
        latency_ms=int((time.time() - t0) * 1000),
    )
    return {"answer": answer, "mode": mode, "sql": sql, "rows": data, "highlight_node_ids": highlight_node_ids}


app = FastAPI(title="O2C Graph Explorer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/graph/seed")
def graph_seed(limit: int = 150) -> dict[str, Any]:
    with get_conn() as conn:
        nodes = conn.execute("SELECT node_id, node_type, label, metadata FROM graph_nodes LIMIT ?", (limit,)).fetchall()
        edges = conn.execute("SELECT source_id, target_id, relation, metadata FROM graph_edges LIMIT ?", (limit * 2,)).fetchall()
    return {"nodes": rows_to_dicts(nodes), "edges": rows_to_dicts(edges)}


@app.get("/api/graph/expand/{node_id}")
def graph_expand(node_id: str, limit: int = 100) -> dict[str, Any]:
    with get_conn() as conn:
        node = conn.execute("SELECT node_id, node_type, label, metadata FROM graph_nodes WHERE node_id = ?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
        edges = conn.execute(
            "SELECT source_id, target_id, relation, metadata FROM graph_edges WHERE source_id = ? OR target_id = ? LIMIT ?",
            (node_id, node_id, limit),
        ).fetchall()
        neighbor_ids = list({eid for e in edges for eid in (e["source_id"], e["target_id"])})
        neighbors = []
        if neighbor_ids:
            placeholders = ",".join("?" for _ in neighbor_ids)
            neighbors = conn.execute(
                f"SELECT node_id, node_type, label, metadata FROM graph_nodes WHERE node_id IN ({placeholders})",
                tuple(neighbor_ids),
            ).fetchall()
    return {"center": dict(node), "nodes": rows_to_dicts(neighbors), "edges": rows_to_dicts(edges)}


@app.get("/api/graph/trace/{billing_document}")
def graph_trace_path(billing_document: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{6,}", billing_document):
        raise HTTPException(status_code=400, detail="Billing document must be numeric.")

    with get_conn() as conn:
        trace_rows = conn.execute(
            """
            SELECT
              bdi.billingDocument,
              odi.deliveryDocument,
              soi.salesOrder,
              je.accountingDocument AS journalEntry,
              je.fiscalYear,
              je.companyCode,
              par.accountingDocument AS paymentDocument,
              par.accountingDocumentItem
            FROM billing_document_items bdi
            LEFT JOIN outbound_delivery_items odi
              ON odi.deliveryDocument = bdi.referenceSdDocument
            LEFT JOIN sales_order_items soi
              ON soi.salesOrder = odi.referenceSdDocument
             AND soi.salesOrderItem = odi.referenceSdDocumentItem
            LEFT JOIN journal_entry_items_accounts_receivable je
              ON je.referenceDocument = bdi.billingDocument
            LEFT JOIN payments_accounts_receivable par
              ON par.clearingAccountingDocument = je.accountingDocument
             AND par.clearingDocFiscalYear = je.fiscalYear
             AND par.companyCode = je.companyCode
            WHERE bdi.billingDocument = ?
            LIMIT 100
            """,
            (billing_document,),
        ).fetchall()

        if not trace_rows:
            return {
                "billing_document": billing_document,
                "message": "No trace path found for this billing document.",
                "nodes": [],
                "edges": [],
                "highlight_node_ids": [],
                "ordered_path": [],
            }

        node_ids: set[str] = {f"B:{billing_document}"}
        ordered_path = [f"B:{billing_document}"]
        for r in trace_rows:
            d = r["deliveryDocument"]
            so = r["salesOrder"]
            je = r["journalEntry"]
            pay = r["paymentDocument"]
            fy = r["fiscalYear"]
            cc = r["companyCode"]
            item = r["accountingDocumentItem"]
            if so:
                node_ids.add(f"SO:{so}")
                ordered_path.insert(0, f"SO:{so}")
            if d:
                node_ids.add(f"D:{d}")
                if f"D:{d}" not in ordered_path:
                    ordered_path.insert(ordered_path.index(f"B:{billing_document}"), f"D:{d}")
            if je and fy and cc:
                j_id = f"J:{je}:{fy}:{cc}"
                node_ids.add(j_id)
                if j_id not in ordered_path:
                    ordered_path.append(j_id)
            if pay and item and fy and cc:
                p_id = f"PAY:{pay}:{item}:{fy}:{cc}"
                node_ids.add(p_id)
                if p_id not in ordered_path:
                    ordered_path.append(p_id)

        placeholders = ",".join("?" for _ in node_ids)
        nodes = conn.execute(
            f"SELECT node_id, node_type, label, metadata FROM graph_nodes WHERE node_id IN ({placeholders})",
            tuple(node_ids),
        ).fetchall()
        edges = conn.execute(
            f"""
            SELECT source_id, target_id, relation, metadata
            FROM graph_edges
            WHERE source_id IN ({placeholders}) AND target_id IN ({placeholders})
            """,
            tuple(node_ids) + tuple(node_ids),
        ).fetchall()

    # Unique preserve order
    seen = set()
    compact_path = []
    for node_id in ordered_path:
        if node_id not in seen:
            seen.add(node_id)
            compact_path.append(node_id)

    return {
        "billing_document": billing_document,
        "nodes": rows_to_dicts(nodes),
        "edges": rows_to_dicts(edges),
        "highlight_node_ids": compact_path,
        "ordered_path": compact_path,
    }


@app.post("/api/search/entities")
def search_entities(req: SearchRequest) -> dict[str, Any]:
    query = normalize_text(req.query)
    if not query:
        return {"nodes": []}
    like = f"%{query}%"
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT node_id, node_type, label, metadata
            FROM graph_nodes
            WHERE lower(label) LIKE ? OR lower(metadata) LIKE ? OR lower(node_type) LIKE ?
            ORDER BY
              CASE WHEN lower(label) = ? THEN 0
                   WHEN lower(label) LIKE ? THEN 1
                   ELSE 2 END,
              node_type, label
            LIMIT ?
            """,
            (like, like, like, query, like, req.limit),
        ).fetchall()
    return {"nodes": rows_to_dicts(rows)}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    return run_chat_logic(req)


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    payload = run_chat_logic(req)
    answer = payload["answer"]

    def gen():
        yield json.dumps({"type": "meta", "mode": payload["mode"], "sql": payload["sql"], "highlight_node_ids": payload["highlight_node_ids"]}) + "\n"
        for token in answer.split(" "):
            yield json.dumps({"type": "token", "value": token + " "}) + "\n"
        yield json.dumps({"type": "rows", "rows": payload["rows"]}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/chat/reset/{session_id}")
def reset_memory(session_id: str) -> dict[str, str]:
    MEMORY.pop(session_id, None)
    return {"status": "ok"}


@app.get("/api/audit/recent")
def audit_recent(limit: int = 50) -> dict[str, Any]:
    with get_conn() as conn:
        ensure_audit_table(conn)
        rows = conn.execute(
            """
            SELECT id, created_at, session_id, question, mode, row_count, guardrail_blocked, latency_ms, error_text
            FROM query_audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"logs": rows_to_dicts(rows)}


@app.get("/api/audit/export")
def audit_export(limit: int = 5000) -> dict[str, Any]:
    with get_conn() as conn:
        ensure_audit_table(conn)
        rows = conn.execute(
            """
            SELECT id, created_at, session_id, question, mode, sql_text, row_count, answer, guardrail_blocked, latency_ms, error_text
            FROM query_audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"logs": rows_to_dicts(rows)}


@app.get("/api/graph/analytics")
def graph_analytics(limit: int = 10) -> dict[str, Any]:
    with get_conn() as conn:
        node_type_counts = conn.execute(
            "SELECT node_type, COUNT(*) AS count FROM graph_nodes GROUP BY node_type ORDER BY count DESC"
        ).fetchall()
        top_degree = conn.execute(
            """
            SELECT n.node_id, n.node_type, n.label, COUNT(*) AS degree
            FROM graph_nodes n
            JOIN graph_edges e ON e.source_id = n.node_id OR e.target_id = n.node_id
            GROUP BY n.node_id, n.node_type, n.label
            ORDER BY degree DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        relation_counts = conn.execute(
            "SELECT relation, COUNT(*) AS count FROM graph_edges GROUP BY relation ORDER BY count DESC"
        ).fetchall()
    return {
        "node_type_counts": rows_to_dicts(node_type_counts),
        "top_degree_nodes": rows_to_dicts(top_degree),
        "relation_counts": rows_to_dicts(relation_counts),
    }


@app.get("/api/graph/relations")
def graph_relations() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT relation, COUNT(*) AS edge_count
            FROM graph_edges
            GROUP BY relation
            ORDER BY edge_count DESC, relation ASC
            """
        ).fetchall()
    return {"relations": rows_to_dicts(rows)}


@app.get("/api/dataset/stats")
def dataset_stats() -> dict[str, Any]:
    base_dirs = [ROOT / "sap-o2c-data", ROOT / "sap-o2c-data 2"]
    per_table: dict[str, dict[str, int]] = {}

    for base in base_dirs:
        source_name = base.name
        if not base.exists():
            continue
        for table_dir in sorted([p for p in base.iterdir() if p.is_dir()]):
            table = table_dir.name
            count = 0
            for fp in sorted(table_dir.glob("*.jsonl")):
                with fp.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            count += 1
            if table not in per_table:
                per_table[table] = {}
            per_table[table][source_name] = count

    with get_conn() as conn:
        integrated_counts = {}
        for table in sorted(ALLOWED_TABLES):
            try:
                value = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()
                integrated_counts[table] = int(value["c"]) if value else 0
            except sqlite3.Error:
                continue

    table_rows = []
    for table in sorted(per_table.keys()):
        src_a = per_table[table].get("sap-o2c-data", 0)
        src_b = per_table[table].get("sap-o2c-data 2", 0)
        table_rows.append(
            {
                "table": table,
                "sap_o2c_data_rows": src_a,
                "sap_o2c_data_2_rows": src_b,
                "raw_sum": src_a + src_b,
                "integrated_table_rows": integrated_counts.get(table, 0),
            }
        )

    return {
        "sources": [d.name for d in base_dirs if d.exists()],
        "tables": table_rows,
        "integrated_counts": integrated_counts,
    }

