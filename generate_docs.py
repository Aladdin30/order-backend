"""Generate comprehensive Word document (API_Testing_Manual_Phase1_Phase2.docx).

Covers:
- Master Endpoints Reference Checklist across Phase 1 and Phase 2 (24 endpoints)
- Quick Setup & Getting Ready
- Staff Catalog CRUD REST Endpoints (/api/v1/staff/menu)
- Physical Table QR Generation
- Customer Dining Experience (Guest Flow)
- Kitchen & Staff Operations (Order transitions, Table sync to EATING, Service requests)
- Bill Settlement & Clean Teardown (Cash verification, Table reset to AVAILABLE, 401 Session lockout)
"""

import os
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn


def set_cell_background(cell, fill_hex):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tcPr.append(shd)


def set_cell_margins(cell, top=100, bottom=100, left=150, right=150):
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = OxmlElement('w:tcMar')
    for m, val in [('w:top', top), ('w:bottom', bottom), ('w:left', left), ('w:right', right)]:
        node = OxmlElement(m)
        node.set(qn('w:w'), str(val))
        node.set(qn('w:type'), 'dxa')
        tcMar.append(node)
    tcPr.append(tcMar)


def add_callout(doc, title, text, bg_hex="EDF2F7", border_hex="2B6CB0"):
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    cell = table.cell(0, 0)
    cell.width = Inches(6.5)
    set_cell_background(cell, bg_hex)
    set_cell_margins(cell, top=120, bottom=120, left=200, right=200)

    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(4)
    run_title = p.add_run(f"📌 {title}\n")
    run_title.bold = True
    run_title.font.size = Pt(10)
    run_title.font.color.rgb = RGBColor(0x1A, 0x36, 0x5D)

    run_text = p.add_run(text)
    run_text.font.size = Pt(9.5)
    run_text.font.color.rgb = RGBColor(0x2D, 0x37, 0x48)
    doc.add_paragraph()


def add_code_block(doc, code_text):
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    cell = table.cell(0, 0)
    cell.width = Inches(6.5)
    set_cell_background(cell, "F7FAFC")
    set_cell_margins(cell, top=80, bottom=80, left=140, right=140)

    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(code_text)
    run.font.name = "Consolas"
    run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(0x1A, 0x20, 0x2C)
    doc.add_paragraph()


def format_table_header(row, col_widths, bg_hex="1A365D"):
    for i, cell in enumerate(row.cells):
        cell.width = col_widths[i]
        set_cell_background(cell, bg_hex)
        set_cell_margins(cell, top=100, bottom=100, left=120, right=120)
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            for r in p.runs:
                r.bold = True
                r.font.size = Pt(9)
                r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)


def format_table_row(row, col_widths, is_even=False):
    bg_hex = "F7FAFC" if is_even else "FFFFFF"
    for i, cell in enumerate(row.cells):
        cell.width = col_widths[i]
        set_cell_background(cell, bg_hex)
        set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
        for p in cell.paragraphs:
            for r in p.runs:
                r.font.size = Pt(8.5)
                r.font.color.rgb = RGBColor(0x2D, 0x37, 0x48)


def generate_documentation(output_path: str):
    doc = Document()

    # Document Margins
    for section in doc.sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    # -------------------------------------------------------------------------
    # COVER / HEADER
    # -------------------------------------------------------------------------
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_title = p_title.add_run("SMART RESTAURANT PLATFORM\n")
    r_title.bold = True
    r_title.font.size = Pt(22)
    r_title.font.color.rgb = RGBColor(0x1A, 0x36, 0x5D)

    r_sub = p_title.add_run("Interactive Testing Manual & Complete Developer Runbook\n")
    r_sub.bold = True
    r_sub.font.size = Pt(14)
    r_sub.font.color.rgb = RGBColor(0x2B, 0x6C, 0xB0)

    r_desc = p_title.add_run("Phase 1 (Restaurant Setup & Staff CRUD) & Phase 2 (Guest Dining Experience & Operations)")
    r_desc.font.size = Pt(11)
    r_desc.font.italic = True
    r_desc.font.color.rgb = RGBColor(0x71, 0x80, 0x96)

    doc.add_paragraph()

    add_callout(
        doc,
        "System Architecture & Testing Objectives",
        "This manual enables developers and QA engineers to execute deterministic, step-by-step verification "
        "of the entire backend dining and ordering pipeline via Swagger UI (http://localhost:8000/docs), "
        "Postman, or cURL. It covers multi-tenant isolation, cryptographic HMAC QR tables, dynamic staff catalog CRUD, "
        "Item 86 availability toggles, ACID checkout, kitchen FSM transitions, and clean table teardown.",
        "EBF8FF",
        "3182CE",
    )

    # -------------------------------------------------------------------------
    # PART 1: MASTER ENDPOINTS REFERENCE CHECKLIST
    # -------------------------------------------------------------------------
    h1 = doc.add_heading("1. Master Endpoints Reference Checklist", level=1)
    h1.paragraph_format.space_before = Pt(16)
    h1.paragraph_format.space_after = Pt(8)

    p_chk = doc.add_paragraph("Comprehensive inventory of all 24 production endpoints across Phase 1 and Phase 2:")

    table = doc.add_table(rows=1, cols=6)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    headers = ["Category", "Persona", "Endpoint", "Method", "Key Headers", "Purpose / Key Return"]
    widths = [Inches(1.0), Inches(0.8), Inches(2.2), Inches(0.7), Inches(1.4), Inches(2.3)]

    hdr_row = table.rows[0]
    for i, h_text in enumerate(headers):
        hdr_row.cells[i].paragraphs[0].text = h_text
    format_table_header(hdr_row, widths)

    master_endpoints = [
        ("System", "Any", "/health/ready", "GET", "None", "DB & Redis connectivity readiness check"),
        ("Auth", "Staff", "/api/v1/auth/token", "POST", "Content-Type: urlencoded", "Issue Staff JWT (Argon2id auth)"),
        ("Auth", "Staff", "/api/v1/auth/me", "GET", "Bearer <staff>", "Inspect identity & assigned branch IDs"),
        ("QR Sticker", "Staff", "/api/v1/qr/generate-token", "POST", "Bearer <staff>, X-Branch-ID", "Produce binary-signed HMAC table QR token"),
        ("Staff Menu", "Staff", "/api/v1/staff/menu/categories", "POST", "Bearer <staff>, X-Branch-ID", "Create menu category with kitchen station"),
        ("Staff Menu", "Staff", "/api/v1/staff/menu/items", "POST", "Bearer <staff>, X-Branch-ID", "Create menu item with bilingual names & price"),
        ("Staff Menu", "Staff", "/api/v1/staff/menu/items/{id}/availability", "PATCH", "Bearer <staff>, X-Branch-ID", "Instant Item 86 toggle (out-of-stock switch)"),
        ("Session", "Guest", "/api/v1/sessions/verify-presence", "POST", "None", "Verify QR & GPS, issue Guest JWT (BROWSING)"),
        ("Menu", "Guest", "/api/v1/menu/tree", "GET", "Accept-Language: ar/en", "Hierarchical catalog tree with 86'd items"),
        ("Pricing", "Guest", "/api/v1/menu/validate-item-selection", "POST", "Bearer <guest>", "Server-side modifier validation & line pricing"),
        ("Checkout", "Guest", "/api/v1/orders/checkout", "POST", "Bearer <guest>", "ACID table checkout; subsequent calls append"),
        ("Tracking", "Guest", "/api/v1/orders/active", "GET", "Bearer <guest>", "Live active table order & preparation status"),
        ("Cancel", "Guest/Staff", "/api/v1/orders/{id}/cancel", "POST", "Bearer <token>", "Guarded cancellation for draft/pending tickets"),
        ("FSM Bump", "Staff", "/api/v1/orders/{id}/transition", "POST", "Bearer <staff>, X-Branch-ID", "Advance cooking lifecycle (DELIVERED -> EATING)"),
        ("Table Call", "Guest", "/api/v1/service-requests", "POST", "Bearer <guest>", "Request table assistance (WATER, OTHER with note)"),
        ("Guest Queue", "Guest", "/api/v1/service-requests/table/active", "GET", "Bearer <guest>", "Customer view of pending/active requests"),
        ("Staff Queue", "Staff", "/api/v1/service-requests/active", "GET", "Bearer <staff>, X-Branch-ID", "Waiter FIFO queue of pending branch requests"),
        ("Resolve Call", "Staff", "/api/v1/service-requests/{id}/status", "PATCH", "Bearer <staff>, X-Branch-ID", "Transition request to ACKNOWLEDGED / COMPLETED"),
        ("Online Pay", "Guest", "/api/v1/payments/online/initiate", "POST", "Bearer <guest>", "Initialize Stripe / digital wallet session"),
        ("Webhook", "Gateway", "/api/v1/payments/webhooks/{provider}", "POST", "Stripe-Signature / X-Signature", "Cryptographic gateway webhook & auto-settlement"),
        ("Cash Call", "Guest", "/api/v1/payments/offline/request", "POST", "Bearer <guest>", "Request physical cash settlement (BILL_REQUESTED)"),
        ("Cash Queue", "Cashier", "/api/v1/payments/branch/pending", "GET", "Bearer <staff>, X-Branch-ID", "List pending cash payments awaiting register"),
        ("Cash Verify", "Cashier", "/api/v1/payments/offline/{id}/verify", "POST", "Bearer <staff>, X-Branch-ID", "Confirm cash: Order CLOSED, Table AVAILABLE"),
        ("Teardown", "Guest", "/api/v1/orders/checkout", "POST", "Bearer <old_guest>", "Lockout guard: 401 SESSION_TERMINATED_TABLE_AVAILABLE"),
    ]

    for idx, ep in enumerate(master_endpoints):
        row = table.add_row()
        for c_idx, val in enumerate(ep):
            row.cells[c_idx].paragraphs[0].text = val
        format_table_row(row, widths, is_even=(idx % 2 == 1))

    doc.add_paragraph()

    # -------------------------------------------------------------------------
    # PART 2: QUICK SETUP & GETTING READY
    # -------------------------------------------------------------------------
    h2 = doc.add_heading("2. Quick Setup & Getting Ready", level=1)
    h2.paragraph_format.space_before = Pt(16)

    doc.add_heading("2.1 Starting the Application Server", level=2)
    doc.add_paragraph("Launch the FastAPI backend with uvicorn and hot-reload enabled:")
    add_code_block(doc, "uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
    doc.add_paragraph("Verify system readiness check:")
    add_code_block(doc, "curl -X GET http://localhost:8000/health/ready\n# Returns: {\"status\": \"ready\", \"database\": \"connected\", \"service\": \"Smart Restaurant Platform\"}")

    doc.add_heading("2.2 Generating Demo Seed Data", level=2)
    doc.add_paragraph("Run the interactive database seeder to populate Tenant, Branch, Tables, and Staff credentials:")
    add_code_block(doc, "python scripts/seed_data.py")
    doc.add_paragraph("Pre-seeded Staff Accounts (Password for all: Admin123!):")
    doc.add_paragraph("• Super Admin: admin@gourmet.com (Role: SUPER_ADMIN)\n"
                      "• Branch Admin: branchadmin@gourmet.com (Role: BRANCH_ADMIN, Downtown Branch)\n"
                      "• Cashier: cashier@gourmet.com (Role: CASHIER, Downtown Branch)")

    doc.add_heading("2.3 Swagger UI Authorization (Staff vs Guest Tokens)", level=2)
    doc.add_paragraph("Navigate to http://localhost:8000/docs and click the green 'Authorize 🔓' button:")
    doc.add_paragraph("1. For Staff: Use the OAuth2 password dialog. Enter branchadmin@gourmet.com and Admin123!. Swagger UI calls /api/v1/auth/token and sets the Bearer header automatically.\n"
                      "2. For Guest: After scanning QR via /sessions/verify-presence, paste the returned session_token into the BearerAuth dialog.")

    # -------------------------------------------------------------------------
    # PART 3: PHASE 1 — STAFF RESTAURANT SETUP & CATALOG CRUD
    # -------------------------------------------------------------------------
    h3 = doc.add_heading("3. Phase 1 — Restaurant Management & Staff Catalog CRUD", level=1)
    h3.paragraph_format.space_before = Pt(16)

    doc.add_heading("3.1 Staff Login & JWT Issuance", level=2)
    doc.add_paragraph("Endpoint: POST /api/v1/auth/token\nHeader: Content-Type: application/x-www-form-urlencoded")
    add_code_block(doc, "curl -X POST http://localhost:8000/api/v1/auth/token \\\n"
                        "  -H 'Content-Type: application/x-www-form-urlencoded' \\\n"
                        "  -d 'username=branchadmin@gourmet.com&password=Admin123!'")

    doc.add_heading("3.2 Dynamic Category Management (/api/v1/staff/menu/categories)", level=2)
    doc.add_paragraph("Create a category assigned to HOT_KITCHEN:")
    add_code_block(doc, "POST /api/v1/staff/menu/categories\n"
                        "Headers: Authorization: Bearer <STAFF_TOKEN>, X-Branch-ID: <BRANCH_ID>\n"
                        "Payload:\n"
                        "{\n"
                        '  "name": {"en": "Burgers & Sandwiches", "ar": "البرغر والسندويشات"},\n'
                        '  "display_order": 1,\n'
                        '  "station": "HOT_KITCHEN",\n'
                        '  "is_active": true\n'
                        "}\n"
                        "Returns: 201 Created with category UUID.")

    doc.add_paragraph("Partially update category name or sequence:")
    add_code_block(doc, "PATCH /api/v1/staff/menu/categories/<CATEGORY_ID>\n"
                        "Payload:\n"
                        "{\n"
                        '  "name": {"en": "Artisan Burgers", "ar": "برغر مميز"},\n'
                        '  "display_order": 2\n'
                        "}")

    doc.add_heading("3.3 Item Management & Item 86 Kill-Switch (/api/v1/staff/menu/items)", level=2)
    doc.add_paragraph("Create a bilingual catalog item:")
    add_code_block(doc, "POST /api/v1/staff/menu/items\n"
                        "Payload:\n"
                        "{\n"
                        '  "category_id": "<CATEGORY_ID>",\n'
                        '  "name": {"en": "Double Angus Smash", "ar": "دبل أنغوس سماش"},\n'
                        '  "description": {"en": "Two smashed Angus patties with cheddar", "ar": "شريحتان لحم أنغوس مع جبنة"},\n'
                        '  "base_price": 42.00,\n'
                        '  "station": "HOT_KITCHEN",\n'
                        '  "is_available": true,\n'
                        '  "allergens": ["gluten", "dairy"],\n'
                        '  "dietary_badges": ["halal"]\n'
                        "}")

    doc.add_paragraph("Toggle Item 86 Kill-Switch (Instant Out-of-Stock):")
    add_code_block(doc, "PATCH /api/v1/staff/menu/items/<ITEM_ID>/availability\n"
                        "Payload:\n"
                        "{\n"
                        '  "is_available": false\n'
                        "}\n"
                        "Returns: 200 OK. Item is immediately grayed out in menu tree.")

    doc.add_heading("3.4 Modifier Groups & Options CRUD", level=2)
    doc.add_paragraph("Add modifier group (e.g. Size: min=1, max=1):")
    add_code_block(doc, "POST /api/v1/staff/menu/items/<ITEM_ID>/modifier-groups\n"
                        "Payload:\n"
                        "{\n"
                        '  "name": {"en": "Patty Size", "ar": "حجم الشريحة"},\n'
                        '  "min_choices": 1,\n'
                        '  "max_choices": 1,\n'
                        '  "is_required": true\n'
                        "}")

    doc.add_paragraph("Add modifier option with price adjustment:")
    add_code_block(doc, "POST /api/v1/staff/menu/modifier-groups/<GROUP_ID>/options\n"
                        "Payload:\n"
                        "{\n"
                        '  "name": {"en": "Triple Patty (+12 SAR)", "ar": "ثلاث شرائح (+12 ر.س)"},\n'
                        '  "price_delta": 12.00,\n'
                        '  "is_available": true\n'
                        "}")

    doc.add_heading("3.5 Physical Table QR Token Issuance", level=2)
    doc.add_paragraph("Generate tamper-proof HMAC-SHA256 signed table token for Table 1:")
    add_code_block(doc, "POST /api/v1/qr/generate-token\n"
                        "Headers: Authorization: Bearer <STAFF_TOKEN>, X-Branch-ID: <BRANCH_ID>\n"
                        "Payload: {\"table_id\": \"<TABLE_1_ID>\"}\n"
                        "Returns: Base64 signed token encoding version + tenant + branch + table + HMAC signature.")

    # -------------------------------------------------------------------------
    # PART 4: PHASE 2 — GUEST DINING FLOW
    # -------------------------------------------------------------------------
    h4 = doc.add_heading("4. Phase 2 — The Customer Dining Experience (Guest Flow)", level=1)
    h4.paragraph_format.space_before = Pt(16)

    doc.add_heading("4.1 Scan QR & Verify Presence", level=2)
    doc.add_paragraph("Guest scans table sticker. Client sends QR token and GPS coordinates inside branch geofence (150m):")
    add_code_block(doc, "POST /api/v1/sessions/verify-presence\n"
                        "Payload:\n"
                        "{\n"
                        '  "qr_token": "<TABLE_QR_TOKEN>",\n'
                        '  "client_latitude": 24.7135517,\n'
                        '  "client_longitude": 46.6752957\n'
                        "}\n"
                        "Returns: 200 OK with session_token (Guest JWT) and table_status = 'BROWSING'.")

    doc.add_heading("4.2 Browse Localized Menu & Item 86 Rendering", level=2)
    doc.add_paragraph("Call GET /api/v1/menu/tree with Accept-Language: ar to receive Arabic names, or en for English. Out-of-stock items have is_available: false for grayed-out UI rendering.")

    doc.add_heading("4.3 Modifier Configuration & Line Pricing", level=2)
    doc.add_paragraph("Validate choices against min/max rules and calculate exact line totals before checkout:")
    add_code_block(doc, "POST /api/v1/menu/validate-item-selection\n"
                        "Headers: Authorization: Bearer <GUEST_TOKEN>\n"
                        "Payload:\n"
                        "{\n"
                        '  "item_id": "<ITEM_ID>",\n'
                        '  "quantity": 2,\n'
                        '  "selected_groups": [\n'
                        '    {"group_id": "<GROUP_ID>", "option_ids": ["<OPTION_ID>"]}\n'
                        '  ]\n'
                        "}\n"
                        "Returns: Calculated base_price, unit_price (base + deltas), and authoritative subtotal.")

    doc.add_heading("4.4 Place Order & Shared Bill Append", level=2)
    doc.add_paragraph("Atomically place order using ACID row lock on table. Subsequent checkouts at the same table append items to the shared bill with 15% VAT calculation:")
    add_code_block(doc, "POST /api/v1/orders/checkout\n"
                        "Headers: Authorization: Bearer <GUEST_TOKEN>\n"
                        "Payload:\n"
                        "{\n"
                        '  "items": [\n'
                        '    {"item_id": "<ITEM_ID>", "quantity": 1, "selected_groups": [{"group_id": "<GROUP_ID>", "option_ids": ["<OPTION_ID>"]}]}\n'
                        '  ],\n'
                        '  "customer_notes": "Allergic to dairy"\n'
                        "}\n"
                        "Returns: 201 Created with order UUID, 15% tax_total, and total_amount. Table status -> 'AWAITING_FOOD'.")

    # -------------------------------------------------------------------------
    # PART 5: KITCHEN OPERATIONS & SERVICE REQUESTS
    # -------------------------------------------------------------------------
    h5 = doc.add_heading("5. Kitchen Operations & Table Service Requests", level=1)
    h5.paragraph_format.space_before = Pt(16)

    doc.add_heading("5.1 Order FSM Transitions & Table Auto-Sync", level=2)
    doc.add_paragraph("Staff moves order lifecycle: SUBMITTED -> PREPARING -> READY -> DELIVERED:")
    add_code_block(doc, "POST /api/v1/orders/<ORDER_ID>/transition\n"
                        "Headers: Authorization: Bearer <STAFF_TOKEN>, X-Branch-ID: <BRANCH_ID>\n"
                        "Payload: {\"target_status\": \"DELIVERED\", \"reason\": \"Delivered by runner\"}\n"
                        "Key Validation: Table status automatically synchronizes to 'EATING'.")

    doc.add_heading("5.2 Table Service Requests & Anti-Spam Duplicate Guard", level=2)
    doc.add_paragraph("Guest calls for WATER or OTHER assistance. Duplicate active requests return 409 Conflict:")
    add_code_block(doc, "POST /api/v1/service-requests\n"
                        "Payload: {\"request_type\": \"WATER\"}\n"
                        "Duplicate Test: Immediately repeating this call returns 409 Conflict ('ACTIVE_REQUEST_EXISTS').")
    doc.add_paragraph("Staff views active queue at GET /api/v1/service-requests/active and resolves via PATCH /api/v1/service-requests/{id}/status (status: 'COMPLETED').")

    # -------------------------------------------------------------------------
    # PART 6: BILL SETTLEMENT & CLEAN TEARDOWN
    # -------------------------------------------------------------------------
    h6 = doc.add_heading("6. Bill Settlement & Clean Teardown", level=1)
    h6.paragraph_format.space_before = Pt(16)

    doc.add_heading("6.1 Cash Payment Settlement", level=2)
    doc.add_paragraph("Guest requests table-side cash settlement (moves table to BILL_REQUESTED):")
    add_code_block(doc, "POST /api/v1/payments/offline/request\n"
                        "Payload: {\"method\": \"CASH\"}\n"
                        "Returns: Payment record with status 'PENDING_CASHIER_VERIFICATION'.")

    doc.add_paragraph("Cashier confirms physical cash receipt at register:")
    add_code_block(doc, "POST /api/v1/payments/offline/<PAYMENT_ID>/verify\n"
                        "Headers: Authorization: Bearer <STAFF_TOKEN>, X-Branch-ID: <BRANCH_ID>\n"
                        "Payload: {\"notes\": \"Received 92.00 SAR exact cash\"}\n"
                        "Returns: order_status = 'CLOSED', table_status = 'AVAILABLE', is_paid = true.")

    doc.add_heading("6.2 Table Reset & Session Lockout Guard", level=2)
    doc.add_paragraph("Verify that upon table settlement, lingering guest session tokens are instantly locked out:")
    add_code_block(doc, "POST /api/v1/orders/checkout (using old GUEST_TOKEN)\n"
                        "Expected Response: 401 Unauthorized\n"
                        "Detail: 'SESSION_TERMINATED_TABLE_AVAILABLE'\n"
                        "Security Assertion: The table is cleanly recycled. Diners must scan the physical QR again to begin a new session.")

    # Save document
    doc.save(output_path)
    print(f"Document successfully created at: {output_path}")


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "API_Testing_Manual_Phase1_Phase2.docx")
    generate_documentation(out)
