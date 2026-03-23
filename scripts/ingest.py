import glob
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIRS = [ROOT / "sap-o2c-data", ROOT / "sap-o2c-data 2"]
DB_PATH = ROOT / "data" / "o2c_graph.db"


TABLES = {
    "sales_order_headers": "sales_order_headers",
    "sales_order_items": "sales_order_items",
    "outbound_delivery_headers": "outbound_delivery_headers",
    "outbound_delivery_items": "outbound_delivery_items",
    "billing_document_headers": "billing_document_headers",
    "billing_document_items": "billing_document_items",
    "journal_entry_items_accounts_receivable": "journal_entry_items_accounts_receivable",
    "payments_accounts_receivable": "payments_accounts_receivable",
    "products": "products",
    "product_descriptions": "product_descriptions",
    "business_partners": "business_partners",
    "business_partner_addresses": "business_partner_addresses",
    "customer_sales_area_assignments": "customer_sales_area_assignments",
    "customer_company_assignments": "customer_company_assignments",
    "plants": "plants",
}


def read_jsonl_rows(folder_name: str) -> list[dict]:
    rows: list[dict] = []
    for base in DATA_DIRS:
        folder = base / folder_name
        if not folder.exists():
            continue
        for fp in sorted(glob.glob(str(folder / "*.jsonl"))):
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))
    # Deduplicate exact duplicate records across dataset folders.
    deduped = {}
    for row in rows:
        deduped[json.dumps(row, sort_keys=True)] = row
    return list(deduped.values())


def sanitize_col(c: str) -> str:
    out = []
    for ch in c:
        out.append(ch if ch.isalnum() or ch == "_" else "_")
    col = "".join(out)
    if col and col[0].isdigit():
        col = f"c_{col}"
    return col


def create_table(conn: sqlite3.Connection, table: str, sample: dict) -> list[tuple[str, str]]:
    cols = [(k, sanitize_col(k)) for k in sample.keys()]
    col_defs = ", ".join([f'"{safe}" TEXT' for _, safe in cols])
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
    return cols


def insert_rows(conn: sqlite3.Connection, table: str, cols: list[tuple[str, str]], rows: list[dict]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(cols))
    safe_cols = [safe for _, safe in cols]
    q = f'INSERT INTO "{table}" ({", ".join([f"""\"{c}\"""" for c in safe_cols])}) VALUES ({placeholders})'
    values = []
    for row in rows:
        values.append([str(row.get(orig, "")) for orig, _ in cols])
    conn.executemany(q, values)


def build_graph(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS graph_nodes")
    conn.execute("DROP TABLE IF EXISTS graph_edges")
    conn.execute(
        """
        CREATE TABLE graph_nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT,
            label TEXT,
            metadata TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE graph_edges (
            source_id TEXT,
            target_id TEXT,
            relation TEXT,
            metadata TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(node_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_relation ON graph_edges(relation)")

    # sales orders
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'SO:' || salesOrder, 'SalesOrder', salesOrder,
               json_object('salesOrder', salesOrder, 'soldToParty', soldToParty, 'totalNetAmount', totalNetAmount)
        FROM sales_order_headers
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'SOI:' || salesOrder || ':' || salesOrderItem, 'SalesOrderItem', salesOrder || '-' || salesOrderItem,
               json_object('salesOrder', salesOrder, 'salesOrderItem', salesOrderItem, 'material', material, 'netAmount', netAmount)
        FROM sales_order_items
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT 'SO:' || salesOrder, 'SOI:' || salesOrder || ':' || salesOrderItem, 'HAS_ITEM', '{}'
        FROM sales_order_items
        """
    )

    # products
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'P:' || product, 'Product', product, json_object('product', product, 'productType', productType)
        FROM products
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT 'SOI:' || salesOrder || ':' || salesOrderItem, 'P:' || material, 'ITEM_FOR_PRODUCT', '{}'
        FROM sales_order_items
        WHERE material IS NOT NULL AND material != ''
        """
    )

    # deliveries
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'D:' || deliveryDocument, 'Delivery', deliveryDocument,
               json_object('deliveryDocument', deliveryDocument, 'overallGoodsMovementStatus', overallGoodsMovementStatus)
        FROM outbound_delivery_headers
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'DI:' || deliveryDocument || ':' || deliveryDocumentItem, 'DeliveryItem', deliveryDocument || '-' || deliveryDocumentItem,
               json_object('deliveryDocument', deliveryDocument, 'deliveryDocumentItem', deliveryDocumentItem, 'plant', plant)
        FROM outbound_delivery_items
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT 'D:' || deliveryDocument, 'DI:' || deliveryDocument || ':' || deliveryDocumentItem, 'HAS_ITEM', '{}'
        FROM outbound_delivery_items
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT 'SO:' || referenceSdDocument, 'D:' || deliveryDocument, 'FULFILLED_BY_DELIVERY',
               json_object('referenceSdDocumentItem', referenceSdDocumentItem, 'plant', plant, 'storageLocation', storageLocation)
        FROM outbound_delivery_items
        WHERE referenceSdDocument IS NOT NULL AND referenceSdDocument != ''
        """
    )

    # billing
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'B:' || billingDocument, 'BillingDocument', billingDocument,
               json_object('billingDocument', billingDocument, 'fiscalYear', fiscalYear, 'accountingDocument', accountingDocument)
        FROM billing_document_headers
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT 'BI:' || billingDocument || ':' || billingDocumentItem, 'BillingItem', billingDocument || '-' || billingDocumentItem,
               json_object('billingDocument', billingDocument, 'billingDocumentItem', billingDocumentItem, 'material', material)
        FROM billing_document_items
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT 'B:' || billingDocument, 'BI:' || billingDocument || ':' || billingDocumentItem, 'HAS_ITEM', '{}'
        FROM billing_document_items
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT 'D:' || referenceSdDocument, 'B:' || billingDocument, 'BILLED_AS',
               json_object('referenceSdDocumentItem', referenceSdDocumentItem)
        FROM billing_document_items
        WHERE referenceSdDocument IS NOT NULL AND referenceSdDocument != ''
        """
    )

    # journal entries and payments
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT DISTINCT 'J:' || accountingDocument || ':' || fiscalYear || ':' || companyCode,
               'JournalEntry', accountingDocument,
               json_object('accountingDocument', accountingDocument, 'fiscalYear', fiscalYear, 'companyCode', companyCode)
        FROM journal_entry_items_accounts_receivable
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT DISTINCT 'B:' || referenceDocument,
               'J:' || accountingDocument || ':' || fiscalYear || ':' || companyCode,
               'POSTED_TO_JOURNAL',
               json_object('amountInTransactionCurrency', amountInTransactionCurrency, 'transactionCurrency', transactionCurrency)
        FROM journal_entry_items_accounts_receivable
        WHERE referenceDocument IS NOT NULL AND referenceDocument != ''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT DISTINCT 'PAY:' || accountingDocument || ':' || accountingDocumentItem || ':' || fiscalYear || ':' || companyCode,
               'Payment', accountingDocument,
               json_object('accountingDocument', accountingDocument, 'accountingDocumentItem', accountingDocumentItem, 'customer', customer)
        FROM payments_accounts_receivable
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT DISTINCT 'J:' || clearingAccountingDocument || ':' || clearingDocFiscalYear || ':' || companyCode,
               'PAY:' || accountingDocument || ':' || accountingDocumentItem || ':' || fiscalYear || ':' || companyCode,
               'CLEARED_BY_PAYMENT',
               json_object('amountInTransactionCurrency', amountInTransactionCurrency, 'transactionCurrency', transactionCurrency)
        FROM payments_accounts_receivable
        WHERE clearingAccountingDocument IS NOT NULL AND clearingAccountingDocument != ''
        """
    )

    # customer nodes from business partner map
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT DISTINCT 'C:' || customer, 'Customer', customer,
               json_object('customer', customer, 'businessPartnerName', businessPartnerName)
        FROM business_partners
        WHERE customer IS NOT NULL AND customer != ''
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT DISTINCT 'C:' || soh.soldToParty, 'SO:' || soh.salesOrder, 'PLACED_ORDER', '{}'
        FROM sales_order_headers soh
        WHERE soh.soldToParty IS NOT NULL AND soh.soldToParty != ''
        """
    )

    # plant nodes and links
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT DISTINCT 'PL:' || plant, 'Plant', plant,
               json_object('plant', plant, 'plantName', plantName, 'addressId', addressId)
        FROM plants
        WHERE plant IS NOT NULL AND plant != ''
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT DISTINCT 'DI:' || deliveryDocument || ':' || deliveryDocumentItem, 'PL:' || plant, 'DELIVERED_FROM_PLANT', '{}'
        FROM outbound_delivery_items
        WHERE plant IS NOT NULL AND plant != ''
        """
    )

    # address nodes and links
    conn.execute(
        """
        INSERT OR IGNORE INTO graph_nodes(node_id, node_type, label, metadata)
        SELECT DISTINCT 'ADDR:' || addressId, 'Address', COALESCE(NULLIF(cityName, ''), addressId),
               json_object('addressId', addressId, 'cityName', cityName, 'country', country, 'streetName', streetName, 'postalCode', postalCode)
        FROM business_partner_addresses
        WHERE addressId IS NOT NULL AND addressId != ''
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT DISTINCT 'C:' || bp.customer, 'ADDR:' || bpa.addressId, 'HAS_ADDRESS', '{}'
        FROM business_partners bp
        JOIN business_partner_addresses bpa ON bpa.businessPartner = bp.businessPartner
        WHERE bp.customer IS NOT NULL AND bp.customer != '' AND bpa.addressId IS NOT NULL AND bpa.addressId != ''
        """
    )
    conn.execute(
        """
        INSERT INTO graph_edges(source_id, target_id, relation, metadata)
        SELECT DISTINCT 'PL:' || p.plant, 'ADDR:' || p.addressId, 'LOCATED_AT_ADDRESS', '{}'
        FROM plants p
        WHERE p.plant IS NOT NULL AND p.plant != '' AND p.addressId IS NOT NULL AND p.addressId != ''
        """
    )


def main() -> None:
    if not any(d.exists() for d in DATA_DIRS):
        raise SystemExit("Dataset folders not found. Expected sap-o2c-data and/or sap-o2c-data 2")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        for folder_name, table_name in TABLES.items():
            rows = read_jsonl_rows(folder_name)
            if not rows:
                continue
            cols = create_table(conn, table_name, rows[0])
            insert_rows(conn, table_name, cols, rows)
            print(f"Loaded {len(rows)} rows into {table_name}")
        build_graph(conn)
        conn.commit()
        print(f"Graph database created at {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
