"""
ERP catalogue: canonical (SAP-style) models + per-ERP source mappings.

Each ERP object definition:
    tables : list of (table, description)
    ctes   : SQL string or callable(params) -> SQL (without the leading WITH)
    from   : SQL string or callable(params) -> SQL (FROM clause body)
    where  : SQL string or callable(params) -> SQL (may be "")
    fields : {CANONICAL_FIELD: (sql_expression, source_reference)}
             canonical fields not listed are emitted as NULL
    notes  : list of caveats shown in the app

Tokens like {{schema}} are replaced with the ERP's parameter values.
All SQL targets Snowflake.
"""

# ---------------------------------------------------------------------------
# Date helpers (return Snowflake SQL expressions)
# ---------------------------------------------------------------------------
def sap_d(c):
    """SAP DATS 'YYYYMMDD' string, '00000000' = empty."""
    return f"TRY_TO_DATE(NULLIF(NULLIF(TRIM({c}), ''), '00000000'), 'YYYYMMDD')"


def jde_d(c):
    """JD Edwards Julian date CYYDDD (e.g. 125032 = 2025-02-01), 0 = empty."""
    return (f"IFF(COALESCE({c}, 0) = 0, NULL, "
            f"DATEADD(day, MOD({c}, 1000) - 1, DATE_FROM_PARTS(1900 + FLOOR({c} / 1000), 1, 1)))")


def low_d(c):
    """Datetime where 1900-01-01 (or earlier) means 'no date' (iScala, D365)."""
    return f"IFF({c}::DATE <= '1900-01-01'::DATE, NULL, {c}::DATE)"


def ts_d(c):
    """Plain timestamp -> date."""
    return f"TO_DATE({c})"


def t(c):
    """Trim + blank-to-NULL."""
    return f"NULLIF(TRIM({c}), '')"


# ---------------------------------------------------------------------------
# Canonical models (SAP semantics)
# (field, type, display label, SAP reference)
# ---------------------------------------------------------------------------
CANONICAL = {
    "serial": {
        "label": "Serial numbers (SAP IQ09 equivalent)",
        "short": "SERIAL",
        "fields": [
            ("MATERIAL", "VARCHAR", "Material", "EQUI-MATNR"),
            ("SERIAL_NUMBER", "VARCHAR", "Serial number", "EQUI-SERNR"),
            ("PLANT", "VARCHAR", "Plant", "EQBS-B_WERK"),
            ("STORAGE_LOCATION", "VARCHAR", "Stor. Location", "EQBS-B_LAGER"),
            ("BIN_LOCATION", "VARCHAR", "Bin / locator", "—"),
            ("SYSTEM_STATUS", "VARCHAR", "SysStatus", "JEST + TJ02T"),
            ("USER_STATUS", "VARCHAR", "User status", "JEST + TJ30T"),
            ("CUSTOMER", "VARCHAR", "Customer", "EQUI-KUNDE"),
            ("CURRENT_CUSTOMER", "VARCHAR", "CurCustomer", "EQBS-KUNNR"),
            ("LICENSE_NO", "VARCHAR", "License no.", "EQUI-LIZNR"),
            ("DESCRIPTION", "VARCHAR", "Description", "EQKT-EQKTX"),
            ("SALES_ORG", "VARCHAR", "Sales Org.", "ILOA-VKORG"),
            ("COMPANY_CODE", "VARCHAR", "Company Code", "ILOA-BUKRS"),
            ("WARRANTY_START", "DATE", "Cus. Wrty Start", "BGMKOBJ-GWLDT"),
            ("WARRANTY_END", "DATE", "Cust. wrty end", "BGMKOBJ-GWLEN"),
            ("CHANGED_BY", "VARCHAR", "Changed by", "EQUI-AENAM"),
            ("CHANGED_ON", "DATE", "Changed on", "EQUI-AEDAT"),
            ("CREATED_ON", "DATE", "Created on", "EQUI-ERDAT"),
            ("EQUIPMENT", "VARCHAR", "Equipment", "EQUI-EQUNR"),
            ("CONSTRUCTION_TYPE", "VARCHAR", "ConstType", "EQUI-SUBMT"),
            ("TECH_ID_NO", "VARCHAR", "TechIdentNo.", "EQUZ-TIDNR"),
            ("CONSTRUCTION_TYPE_DESC", "VARCHAR", "ConstTypedesc.", "MAKT-MAKTX"),
            ("CONSTRUCTION_YEAR", "VARCHAR", "ConstructYear", "EQUI-BAUJJ"),
            ("CONSTRUCTION_MONTH", "VARCHAR", "ConstructMth", "EQUI-BAUMM"),
            ("ACQUISITION_DATE", "DATE", "Acquisition", "EQUI-ANSDT"),
            ("BATCH", "VARCHAR", "Batch / lot", "EQBS-B_CHARG"),
        ],
    },
    "delivery": {
        "label": "Outbound deliveries (SAP LIKP / LIPS equivalent)",
        "short": "DELIVERY",
        "fields": [
            ("COMPANY_CODE", "VARCHAR", "Company code", "TVKO-BUKRS"),
            ("PLANT", "VARCHAR", "Plant / warehouse", "LIPS-WERKS"),
            ("STORAGE_LOCATION", "VARCHAR", "Storage location", "LIPS-LGORT"),
            ("DELIVERY_NO", "VARCHAR", "Delivery", "LIKP-VBELN"),
            ("DELIVERY_ITEM", "VARCHAR", "Delivery item", "LIPS-POSNR"),
            ("DELIVERY_TYPE", "VARCHAR", "Delivery type", "LIKP-LFART"),
            ("SOLD_TO", "VARCHAR", "Sold-to", "LIKP-KUNAG"),
            ("SHIP_TO", "VARCHAR", "Ship-to", "LIKP-KUNNR"),
            ("SHIP_TO_NAME", "VARCHAR", "Ship-to name", "KNA1-NAME1"),
            ("SALES_ORDER_NO", "VARCHAR", "Sales order", "LIPS-VGBEL"),
            ("SALES_ORDER_ITEM", "VARCHAR", "Sales order item", "LIPS-VGPOS"),
            ("CUSTOMER_PO", "VARCHAR", "Customer PO", "VBKD-BSTKD"),
            ("MATERIAL", "VARCHAR", "Material", "LIPS-MATNR"),
            ("MATERIAL_DESC", "VARCHAR", "Material description", "LIPS-ARKTX"),
            ("BATCH", "VARCHAR", "Batch / lot", "LIPS-CHARG"),
            ("SERIAL_NUMBER", "VARCHAR", "Serial number", "SER01/OBJK"),
            ("DELIVERED_QTY", "NUMBER", "Delivered qty", "LIPS-LFIMG"),
            ("SALES_UNIT", "VARCHAR", "Unit", "LIPS-VRKME"),
            ("NET_VALUE", "NUMBER", "Net value", "LIPS-NETWR"),
            ("CURRENCY", "VARCHAR", "Currency", "VBAK-WAERK"),
            ("GROSS_WEIGHT", "NUMBER", "Gross weight", "LIPS-BRGEW"),
            ("WEIGHT_UNIT", "VARCHAR", "Weight unit", "LIPS-GEWEI"),
            ("VOLUME", "NUMBER", "Volume", "LIPS-VOLUM"),
            ("VOLUME_UNIT", "VARCHAR", "Volume unit", "LIPS-VOLEH"),
            ("SHIPPING_CONDITION", "VARCHAR", "Shipping condition / mode", "LIKP-VSBED"),
            ("INCOTERMS", "VARCHAR", "Incoterms / freight terms", "LIKP-INCO1"),
            ("CARRIER", "VARCHAR", "Carrier / forwarder", "VBPA (SP)"),
            ("TRACKING_NO", "VARCHAR", "Bill of lading / tracking", "LIKP-BOLNR"),
            ("REQUESTED_DELIVERY_DATE", "DATE", "Requested delivery date", "LIKP-LFDAT"),
            ("PLANNED_GI_DATE", "DATE", "Planned goods issue", "LIKP-WADAT"),
            ("ACTUAL_GI_DATE", "DATE", "Actual goods issue / ship date", "LIKP-WADAT_IST"),
            ("CREATED_ON", "DATE", "Created on", "LIKP-ERDAT"),
            ("INVOICE_NO", "VARCHAR", "Invoice", "VBFA (M)"),
            ("INVOICE_DATE", "DATE", "Invoice date", "VBFA-ERDAT"),
            ("DELIVERY_STATUS", "VARCHAR", "Status", "VBUK/LIKP-WBSTK"),
        ],
    },
}

CONFIDENCE = {
    "high": "✅ Column names checked against a published data dictionary.",
    "medium": "⚠️ Standard tables; confirm column names against your version / replication tool.",
    "low": "🧪 Illustrative only. The structure depends on how the data is landed (API / JSON); adapt the paths.",
}


# ---------------------------------------------------------------------------
# SAP ECC / S/4HANA
# ---------------------------------------------------------------------------
def _sap_serial_ctes(p):
    if p.get("status_profile_from") == "JSTO":
        profile_join = """JOIN {{schema}}.JSTO p
        ON p.MANDT = j.MANDT AND p.OBJNR = j.OBJNR
      JOIN {{schema}}.TJ30T ut
        ON ut.MANDT = j.MANDT AND ut.STSMA = p.STSMA AND ut.ESTAT = j.STAT AND ut.SPRAS = '{{lang}}'"""
    else:  # T370T – status profile from equipment category
        profile_join = """JOIN {{schema}}.EQUI eq
        ON eq.MANDT = j.MANDT AND eq.OBJNR = j.OBJNR
      JOIN {{schema}}.T370T cat
        ON cat.MANDT = eq.MANDT AND cat.EQTYP = eq.EQTYP
      JOIN {{schema}}.TJ30T ut
        ON ut.MANDT = j.MANDT AND ut.STSMA = cat.STSMA AND ut.ESTAT = j.STAT AND ut.SPRAS = '{{lang}}'"""
    return f"""equz_cur AS (
    SELECT MANDT, EQUNR, ILOAN, TIDNR
      FROM {{{{schema}}}}.EQUZ
     WHERE DATBI = '99991231'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY MANDT, EQUNR ORDER BY EQLFN DESC) = 1
),
warranty AS (
    SELECT MANDT, J_OBJNR, GWLDT, GWLEN
      FROM {{{{schema}}}}.BGMKOBJ
     WHERE GAART = '1'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY MANDT, J_OBJNR ORDER BY GWLEN DESC) = 1
),
sys_status AS (
    SELECT j.MANDT, j.OBJNR,
           LISTAGG(tx.TXT04, ' ') WITHIN GROUP (ORDER BY j.STAT) AS SYS_STATUS
      FROM {{{{schema}}}}.JEST j
      JOIN {{{{schema}}}}.TJ02T tx
        ON tx.ISTAT = j.STAT AND tx.SPRAS = '{{{{lang}}}}'
     WHERE COALESCE(TRIM(j.INACT), '') = '' AND j.STAT LIKE 'I%'
     GROUP BY j.MANDT, j.OBJNR
),
user_status AS (
    SELECT j.MANDT, j.OBJNR,
           LISTAGG(ut.TXT04, ' ') WITHIN GROUP (ORDER BY j.STAT) AS USER_STATUS
      FROM {{{{schema}}}}.JEST j
      {profile_join}
     WHERE COALESCE(TRIM(j.INACT), '') = '' AND j.STAT LIKE 'E%'
     GROUP BY j.MANDT, j.OBJNR
)"""


def _sap_delivery_from(p):
    vbuk = ("" if p.get("release") == "S/4HANA" else
            "\nLEFT JOIN {{schema}}.VBUK u\n       ON u.MANDT = k.MANDT AND u.VBELN = k.VBELN")
    return """{{schema}}.LIKP k
JOIN {{schema}}.LIPS p
       ON p.MANDT = k.MANDT AND p.VBELN = k.VBELN
LEFT JOIN {{schema}}.TVKO o
       ON o.MANDT = k.MANDT AND o.VKORG = k.VKORG
LEFT JOIN {{schema}}.KNA1 c
       ON c.MANDT = k.MANDT AND c.KUNNR = k.KUNNR
LEFT JOIN {{schema}}.VBAK a
       ON a.MANDT = p.MANDT AND a.VBELN = p.VGBEL
LEFT JOIN {{schema}}.VBKD b
       ON b.MANDT = p.MANDT AND b.VBELN = p.VGBEL AND b.POSNR = '000000'
LEFT JOIN inv i
       ON i.MANDT = p.MANDT AND i.VBELV = p.VBELN AND i.POSNV = p.POSNR""" + vbuk


SAP = {
    "label": "SAP ECC / S/4HANA",
    "vendor": "SAP",
    "confidence": "high",
    "params": {
        "schema": ("SAP_RAW", "Snowflake schema holding the replicated SAP tables", None),
        "lang": ("E", "Language key (SPRAS) for texts", None),
        "client": ("", "Client (MANDT) filter. Leave blank for all clients", None),
        "release": ("ECC", "Release: sets where the delivery status comes from", ["ECC", "S/4HANA"]),
        "status_profile_from": ("T370T", "User status profile source", ["T370T", "JSTO"]),
    },
    "objects": {
        "serial": {
            "tables": [
                ("EQUI", "Equipment master (serial number + material)"),
                ("EQKT", "Equipment short text"),
                ("EQUZ", "Equipment time segment (current: DATBI = 99991231)"),
                ("ILOA", "Location & account assignment (sales org, company code)"),
                ("EQBS", "Serial number stock segment (plant, sloc, batch, stock customer)"),
                ("JEST", "Active object statuses"),
                ("TJ02T / TJ30T", "System / user status texts"),
                ("T370T or JSTO", "Status profile (equipment category or status object)"),
                ("BGMKOBJ", "Warranty assignment (GAART 1 = customer)"),
                ("MAKT", "Material text for construction type"),
            ],
            "ctes": _sap_serial_ctes,
            "from": """{{schema}}.EQUI e
LEFT JOIN {{schema}}.EQKT t
       ON t.MANDT = e.MANDT AND t.EQUNR = e.EQUNR AND t.SPRAS = '{{lang}}'
LEFT JOIN equz_cur z
       ON z.MANDT = e.MANDT AND z.EQUNR = e.EQUNR
LEFT JOIN {{schema}}.ILOA l
       ON l.MANDT = z.MANDT AND l.ILOAN = z.ILOAN
LEFT JOIN {{schema}}.EQBS s
       ON s.MANDT = e.MANDT AND s.EQUNR = e.EQUNR
LEFT JOIN warranty w
       ON w.MANDT = e.MANDT AND w.J_OBJNR = e.OBJNR
LEFT JOIN {{schema}}.MAKT mk
       ON mk.MANDT = e.MANDT AND mk.MATNR = e.SUBMT AND mk.SPRAS = '{{lang}}'
LEFT JOIN sys_status ss
       ON ss.MANDT = e.MANDT AND ss.OBJNR = e.OBJNR
LEFT JOIN user_status us
       ON us.MANDT = e.MANDT AND us.OBJNR = e.OBJNR""",
            "where": """COALESCE(TRIM(e.SERNR), '') <> ''
  AND COALESCE(TRIM(e.MATNR), '') <> ''
  AND ('{{client}}' = '' OR e.MANDT = '{{client}}')""",
            "fields": {
                "MATERIAL": ("e.MATNR", "EQUI-MATNR"),
                "SERIAL_NUMBER": ("e.SERNR", "EQUI-SERNR"),
                "PLANT": ("s.B_WERK", "EQBS-B_WERK"),
                "STORAGE_LOCATION": ("s.B_LAGER", "EQBS-B_LAGER"),
                "SYSTEM_STATUS": ("ss.SYS_STATUS", "JEST (I*) → TJ02T-TXT04"),
                "USER_STATUS": ("us.USER_STATUS", "JEST (E*) → TJ30T-TXT04"),
                "CUSTOMER": (t("e.KUNDE"), "EQUI-KUNDE"),
                "CURRENT_CUSTOMER": (t("s.KUNNR"), "EQBS-KUNNR"),
                "LICENSE_NO": (t("e.LIZNR"), "EQUI-LIZNR"),
                "DESCRIPTION": ("t.EQKTX", "EQKT-EQKTX"),
                "SALES_ORG": ("l.VKORG", "ILOA-VKORG"),
                "COMPANY_CODE": ("l.BUKRS", "ILOA-BUKRS"),
                "WARRANTY_START": (sap_d("w.GWLDT"), "BGMKOBJ-GWLDT"),
                "WARRANTY_END": (sap_d("w.GWLEN"), "BGMKOBJ-GWLEN"),
                "CHANGED_BY": ("e.AENAM", "EQUI-AENAM"),
                "CHANGED_ON": (sap_d("e.AEDAT"), "EQUI-AEDAT"),
                "CREATED_ON": (sap_d("e.ERDAT"), "EQUI-ERDAT"),
                "EQUIPMENT": ("e.EQUNR", "EQUI-EQUNR"),
                "CONSTRUCTION_TYPE": (t("e.SUBMT"), "EQUI-SUBMT"),
                "TECH_ID_NO": (t("z.TIDNR"), "EQUZ-TIDNR"),
                "CONSTRUCTION_TYPE_DESC": ("mk.MAKTX", "MAKT-MAKTX"),
                "CONSTRUCTION_YEAR": (t("e.BAUJJ"), "EQUI-BAUJJ"),
                "CONSTRUCTION_MONTH": (t("e.BAUMM"), "EQUI-BAUMM"),
                "ACQUISITION_DATE": (sap_d("e.ANSDT"), "EQUI-ANSDT"),
                "BATCH": (t("s.B_CHARG"), "EQBS-B_CHARG"),
            },
            "notes": [
                "User status profile: the T370T option reads it from the equipment category; "
                "the JSTO option reads the profile stored on each object.",
                "S/4HANA Cloud Public Edition does not support user statuses on equipment.",
            ],
        },
        "delivery": {
            "tables": [
                ("LIKP / LIPS", "Delivery header / item"),
                ("TVKO", "Sales org → company code"),
                ("KNA1", "Customer name"),
                ("VBAK / VBKD", "Order currency, customer PO"),
                ("VBFA", "Document flow → invoice"),
                ("VBUK (ECC)", "Header status (S/4: LIKP-WBSTK)"),
            ],
            "ctes": """inv AS (
    SELECT MANDT, VBELV, POSNV, VBELN AS INVOICE_NO, ERDAT AS INVOICE_DATE
      FROM {{schema}}.VBFA
     WHERE VBTYP_N = 'M'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY MANDT, VBELV, POSNV ORDER BY ERDAT, VBELN) = 1
)""",
            "from": _sap_delivery_from,
            "where": """p.LFIMG <> 0
  AND ('{{client}}' = '' OR k.MANDT = '{{client}}')""",
            "fields": {
                "COMPANY_CODE": ("o.BUKRS", "TVKO-BUKRS"),
                "PLANT": ("p.WERKS", "LIPS-WERKS"),
                "STORAGE_LOCATION": (t("p.LGORT"), "LIPS-LGORT"),
                "DELIVERY_NO": ("k.VBELN", "LIKP-VBELN"),
                "DELIVERY_ITEM": ("p.POSNR", "LIPS-POSNR"),
                "DELIVERY_TYPE": ("k.LFART", "LIKP-LFART"),
                "SOLD_TO": ("k.KUNAG", "LIKP-KUNAG"),
                "SHIP_TO": ("k.KUNNR", "LIKP-KUNNR"),
                "SHIP_TO_NAME": ("c.NAME1", "KNA1-NAME1"),
                "SALES_ORDER_NO": ("p.VGBEL", "LIPS-VGBEL"),
                "SALES_ORDER_ITEM": ("p.VGPOS", "LIPS-VGPOS"),
                "CUSTOMER_PO": (t("b.BSTKD"), "VBKD-BSTKD"),
                "MATERIAL": ("p.MATNR", "LIPS-MATNR"),
                "MATERIAL_DESC": ("p.ARKTX", "LIPS-ARKTX"),
                "BATCH": (t("p.CHARG"), "LIPS-CHARG"),
                "DELIVERED_QTY": ("p.LFIMG", "LIPS-LFIMG"),
                "SALES_UNIT": ("p.VRKME", "LIPS-VRKME"),
                "NET_VALUE": ("p.NETWR", "LIPS-NETWR"),
                "CURRENCY": ("a.WAERK", "VBAK-WAERK"),
                "GROSS_WEIGHT": ("p.BRGEW", "LIPS-BRGEW"),
                "WEIGHT_UNIT": ("p.GEWEI", "LIPS-GEWEI"),
                "VOLUME": ("p.VOLUM", "LIPS-VOLUM"),
                "VOLUME_UNIT": ("p.VOLEH", "LIPS-VOLEH"),
                "SHIPPING_CONDITION": ("k.VSBED", "LIKP-VSBED"),
                "INCOTERMS": ("k.INCO1", "LIKP-INCO1"),
                "TRACKING_NO": (t("k.BOLNR"), "LIKP-BOLNR"),
                "REQUESTED_DELIVERY_DATE": (sap_d("k.LFDAT"), "LIKP-LFDAT"),
                "PLANNED_GI_DATE": (sap_d("k.WADAT"), "LIKP-WADAT"),
                "ACTUAL_GI_DATE": (sap_d("k.WADAT_IST"), "LIKP-WADAT_IST"),
                "CREATED_ON": (sap_d("k.ERDAT"), "LIKP-ERDAT"),
                "INVOICE_NO": ("i.INVOICE_NO", "VBFA-VBELN (VBTYP_N = M)"),
                "INVOICE_DATE": (sap_d("i.INVOICE_DATE"), "VBFA-ERDAT"),
                "DELIVERY_STATUS": ("u.WBSTK", "VBUK-WBSTK"),
            },
            "notes": [
                "Pick S/4HANA under Release; status then comes from LIKP-WBSTK and VBUK is not joined.",
                "Carrier: join VBPA with PARVW = 'SP' if you need the forwarding agent.",
                "Serial numbers on deliveries live in SER01 + OBJK (not joined by default).",
            ],
        },
    },
}


def _sap_status_override(p):
    if p.get("release") == "S/4HANA":
        return ("k.WBSTK", "LIKP-WBSTK (S/4HANA)")
    return ("u.WBSTK", "VBUK-WBSTK (ECC)")


SAP["objects"]["delivery"]["dynamic_fields"] = {"DELIVERY_STATUS": _sap_status_override}


# ---------------------------------------------------------------------------
# Epicor iScala
# ---------------------------------------------------------------------------
ISCALA = {
    "label": "Epicor iScala",
    "vendor": "Epicor",
    "confidence": "high",
    "params": {
        "schema": ("ISCALA_RAW", "Snowflake schema holding the iScala tables", None),
        "company": ("01", "iScala company code (table suffix: OR19 + 01 + 00 → OR190100)", None),
    },
    "objects": {
        "serial": {
            "tables": [("SC05<cc>00", "Serial Numbers")],
            "ctes": "",
            "from": "{{schema}}.SC05{{company}}00 sn",
            "where": "TRIM(sn.SC05002) <> ''",
            "fields": {
                "MATERIAL": ("TRIM(sn.SC05001)", "SC05001 Stock Item Code"),
                "SERIAL_NUMBER": ("TRIM(sn.SC05002)", "SC05002 Serial Number"),
                "PLANT": (t("sn.SC05038"), "SC05038 Warehouse"),
                "STORAGE_LOCATION": (t("sn.SC05047"), "SC05047 Location"),
                "BIN_LOCATION": (t("sn.SC05039"), "SC05039 Bin"),
                "SYSTEM_STATUS": (t("sn.SC05046"), "SC05046 Owner (status flag)"),
                "CUSTOMER": (t("sn.SC05004"), "SC05004 Customer Code"),
                "CURRENT_CUSTOMER": (t("sn.SC05004"), "SC05004 Customer Code"),
                "DESCRIPTION": (t("sn.SC05003"), "SC05003 Description"),
                "COMPANY_CODE": ("'{{company}}'", "company code of the table"),
                "WARRANTY_START": (low_d("sn.SC05027"), "SC05027 Customer Warranty Start Date"),
                "WARRANTY_END": (low_d("sn.SC05007"), "SC05007 Customer Warranty Expiration Date"),
                "CREATED_ON": (low_d("sn.SC05033"), "SC05033 Date Received"),
                "EQUIPMENT": (t("sn.SC05049"), "SC05049 Asset Code"),
                "ACQUISITION_DATE": (low_d("sn.SC05033"), "SC05033 Date Received"),
                "BATCH": (t("sn.SC05026"), "SC05026 Batch Number"),
            },
            "notes": [
                "iScala has no system/user status engine. SC05046 (Owner) is the closest status flag; "
                "decode its values for your setup.",
                "Other useful SC05 columns: SC05006 delivery date, SC05010 customer order no., "
                "SC05031 supplier, SC05034/35 supplier warranty dates.",
            ],
        },
        "delivery": {
            "tables": [
                ("OR01 / OR20", "Sales order header (live / history)"),
                ("OR03 / OR21", "Sales order lines (live / history)"),
                ("OR19 / OR23", "Order delivery lines (live / history) ≈ LIPS"),
                ("SL01", "Customer file (name)"),
            ],
            "ctes": """hd AS (
    SELECT TRIM(OR01001) AS ORDER_NO, OR01002 AS ORDER_TYPE, TRIM(OR01003) AS CUST_INV,
           TRIM(OR01004) AS CUST_DEL, TRIM(OR01045) AS CUST_ORD, TRIM(OR01013) AS DEL_TERMS,
           OR01014 AS DEL_METHOD, OR01015 AS ORDER_DATE, TRIM(OR01028) AS CURRENCY,
           TRIM(OR01050) AS WAREHOUSE, TRIM(OR01072) AS CUST_PO, TRIM(OR01090) AS FORWARDER
      FROM {{schema}}.OR01{{company}}00
    UNION ALL
    SELECT TRIM(OR20001), OR20002, TRIM(OR20003), TRIM(OR20004), TRIM(OR20045), TRIM(OR20013),
           OR20014, OR20015, TRIM(OR20028), TRIM(OR20050), TRIM(OR20072), TRIM(OR20090)
      FROM {{schema}}.OR20{{company}}00
),
ln AS (
    SELECT TRIM(OR03001) AS ORDER_NO, TRIM(OR03002) AS LINE_NO, TRIM(OR03003) AS SUB_LINE,
           TRIM(OR03005) AS STOCK_CODE, TRIM(OR03006) AS DESCR, TRIM(OR03010) AS UNIT,
           OR03008 AS UNIT_PRICE, COALESCE(OR03017, 0) AS VOL_DISC, COALESCE(OR03018, 0) AS CUST_DISC,
           TRIM(OR03046) AS WAREHOUSE
      FROM {{schema}}.OR03{{company}}00
    UNION ALL
    SELECT TRIM(OR21001), TRIM(OR21002), TRIM(OR21003), TRIM(OR21005), TRIM(OR21006), TRIM(OR21010),
           OR21008, COALESCE(OR21017, 0), COALESCE(OR21018, 0), TRIM(OR21046)
      FROM {{schema}}.OR21{{company}}00
),
dl AS (
    SELECT TRIM(OR19001) AS ORDER_NO, TRIM(OR19002) AS LINE_NO, TRIM(OR19003) AS SUB_LINE,
           TRIM(OR19004) AS DEL_LINE, TRIM(OR19005) AS BATCH_ID, OR19007 AS QTY,
           OR19009 AS DATE_REQ, OR19010 AS DATE_COMMIT, OR19011 AS DATE_ACTUAL,
           TRIM(OR19012) AS DEL_NOTE, OR19025 AS NET_WT, OR19026 AS GROSS_WT, OR19027 AS VOLUME,
           TRIM(OR19034) AS INVOICE_NO, OR19035 AS INVOICE_DATE, TRIM(OR19041) AS CURRENCY
      FROM {{schema}}.OR19{{company}}00
    UNION ALL
    SELECT TRIM(OR23001), TRIM(OR23002), TRIM(OR23003), TRIM(OR23004), TRIM(OR23005), OR23007,
           OR23009, OR23010, OR23011, TRIM(OR23012), OR23025, OR23026, OR23027,
           TRIM(OR23034), OR23035, TRIM(OR23041)
      FROM {{schema}}.OR23{{company}}00
)""",
            "from": """dl
JOIN hd
       ON hd.ORDER_NO = dl.ORDER_NO
LEFT JOIN ln
       ON ln.ORDER_NO = dl.ORDER_NO AND ln.LINE_NO = dl.LINE_NO AND ln.SUB_LINE = dl.SUB_LINE
LEFT JOIN {{schema}}.SL01{{company}}00 cu
       ON TRIM(cu.SL01001) = hd.CUST_DEL""",
            "where": "dl.DEL_NOTE <> ''",
            "fields": {
                "COMPANY_CODE": ("'{{company}}'", "company code of the table"),
                "PLANT": ("COALESCE(" + t("ln.WAREHOUSE") + ", hd.WAREHOUSE)", "OR03046 / OR01050 Warehouse"),
                "DELIVERY_NO": ("dl.DEL_NOTE", "OR19012 Delivery Note Number"),
                "DELIVERY_ITEM": ("dl.LINE_NO || '.' || dl.SUB_LINE || '.' || dl.DEL_LINE",
                                  "OR19002.OR19003.OR19004"),
                "DELIVERY_TYPE": ("'ISC_' || hd.ORDER_TYPE", "OR01002 Order Type"),
                "SOLD_TO": ("COALESCE(NULLIF(hd.CUST_ORD, ''), hd.CUST_INV)", "OR01045 / OR01003"),
                "SHIP_TO": ("hd.CUST_DEL", "OR01004 Customer Code Delivery"),
                "SHIP_TO_NAME": ("TRIM(cu.SL01002)", "SL01002 Customer Name"),
                "SALES_ORDER_NO": ("dl.ORDER_NO", "OR19001 Order Number"),
                "SALES_ORDER_ITEM": ("dl.LINE_NO", "OR19002 Line Number"),
                "CUSTOMER_PO": ("NULLIF(hd.CUST_PO, '')", "OR01072 Customer PO Number"),
                "MATERIAL": ("ln.STOCK_CODE", "OR03005 Stock Item Code"),
                "MATERIAL_DESC": ("ln.DESCR", "OR03006 Description Line 1"),
                "BATCH": ("NULLIF(dl.BATCH_ID, '')", "OR19005 Batch ID"),
                "DELIVERED_QTY": ("dl.QTY", "OR19007 Quantity Delivered"),
                "SALES_UNIT": ("ln.UNIT", "OR03010 Unit Code"),
                "NET_VALUE": ("ROUND(dl.QTY * ln.UNIT_PRICE * (1 - ln.VOL_DISC / 100) * (1 - ln.CUST_DISC / 100), 2)",
                              "OR19007 × OR03008 less OR03017/OR03018 discounts"),
                "CURRENCY": ("COALESCE(NULLIF(dl.CURRENCY, ''), hd.CURRENCY)", "OR19041 / OR01028"),
                "GROSS_WEIGHT": ("dl.GROSS_WT", "OR19026 Gross Weight"),
                "VOLUME": ("dl.VOLUME", "OR19027 Volume"),
                "SHIPPING_CONDITION": ("hd.DEL_METHOD", "OR01014 Delivery Method"),
                "INCOTERMS": ("NULLIF(hd.DEL_TERMS, '')", "OR01013 Delivery Terms"),
                "CARRIER": ("NULLIF(hd.FORWARDER, '')", "OR01090 Forwarder"),
                "REQUESTED_DELIVERY_DATE": (low_d("dl.DATE_REQ"), "OR19009 Delivery Date Requested"),
                "PLANNED_GI_DATE": (low_d("dl.DATE_COMMIT"), "OR19010 Delivery Date Committed"),
                "ACTUAL_GI_DATE": (low_d("dl.DATE_ACTUAL"), "OR19011 Delivery Date Actual"),
                "CREATED_ON": (low_d("hd.ORDER_DATE"), "OR01015 Order Date (proxy)"),
                "INVOICE_NO": ("NULLIF(dl.INVOICE_NO, '')", "OR19034 Invoice Number"),
                "INVOICE_DATE": (low_d("dl.INVOICE_DATE"), "OR19035 Invoice Date"),
                "DELIVERY_STATUS": (f"IFF({low_d('dl.DATE_ACTUAL')} IS NOT NULL AND dl.QTY <> 0, 'C', 'A')",
                                    "derived: actual date + qty"),
            },
            "notes": [
                "History tables (OR20/21/23) are assumed to have the same layout as OR01/03/19.",
                "For more companies, generate one view per company and add them to the combined view.",
                "Net value is calculated from price and discounts; iScala does not store a delivery value.",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# JD Edwards EnterpriseOne
# ---------------------------------------------------------------------------
JDE = {
    "label": "JD Edwards EnterpriseOne",
    "vendor": "Oracle",
    "confidence": "medium",
    "params": {
        "schema": ("JDE_PRODDTA", "Snowflake schema holding JDE business data (PRODDTA)", None),
        "order_types": ("SO", "Comma-separated sales order types (SDDCTO). Leave blank for all", None),
        "qty_divisor": ("1", "Divisor for quantities (implied decimals, e.g. 100)", None),
        "amt_divisor": ("100", "Divisor for amounts (implied decimals of currency)", None),
        "wt_divisor": ("1", "Divisor for weight / volume", None),
    },
    "objects": {
        "serial": {
            "tables": [
                ("F4108", "Lot / serial master"),
                ("F41021", "Item location balances (lot/serial on hand)"),
                ("F4101", "Item master"),
                ("F0006", "Business unit master (branch → company)"),
            ],
            "ctes": """loc AS (
    SELECT LIITM, LIMCU, LILOTN, LILOCN, LIPQOH
      FROM {{schema}}.F41021
     WHERE LIPQOH <> 0
    QUALIFY ROW_NUMBER() OVER (PARTITION BY LIITM, LIMCU, LILOTN ORDER BY LIPQOH DESC) = 1
)""",
            "from": """{{schema}}.F4108 lm
LEFT JOIN {{schema}}.F4101 im
       ON im.IMITM = lm.IOITM
LEFT JOIN loc
       ON loc.LIITM = lm.IOITM AND loc.LIMCU = lm.IOMCU AND loc.LILOTN = lm.IOLOTN
LEFT JOIN {{schema}}.F0006 bu
       ON bu.MCMCU = lm.IOMCU""",
            "where": "TRIM(lm.IOLOTN) <> ''",
            "fields": {
                "MATERIAL": ("TRIM(lm.IOLITM)", "F4108-IOLITM (2nd item no.)"),
                "SERIAL_NUMBER": ("TRIM(lm.IOLOTN)", "F4108-IOLOTN Lot/serial"),
                "PLANT": ("TRIM(lm.IOMCU)", "F4108-IOMCU Branch/plant"),
                "BIN_LOCATION": (t("loc.LILOCN"), "F41021-LILOCN Location"),
                "SYSTEM_STATUS": ("IFF(loc.LIPQOH IS NOT NULL, 'IN STOCK', 'NOT IN STOCK')",
                                  "derived from F41021-LIPQOH"),
                "USER_STATUS": (t("lm.IOLOTS"), "F4108-IOLOTS Lot status code"),
                "DESCRIPTION": ("COALESCE(" + t("lm.IOLDSC") + ", TRIM(im.IMDSC1))",
                                "F4108-IOLDSC / F4101-IMDSC1"),
                "COMPANY_CODE": ("TRIM(bu.MCCO)", "F0006-MCCO"),
                "CHANGED_BY": ("TRIM(lm.IOUSER)", "F4108-IOUSER"),
                "CHANGED_ON": (jde_d("lm.IOUPMJ"), "F4108-IOUPMJ (Julian)"),
                "BATCH": ("TRIM(lm.IOLOTN)", "F4108-IOLOTN"),
            },
            "notes": [
                "JDE stores lots and serials in the same table (F4108). To keep only serialised items, "
                "filter on the item's lot process type in F4101/F4102.",
                "Serial → customer and warranty data lives in Service Management (F1201 / F1217) if you use it.",
                "Branch/plant (MCU) is right-justified CHAR(12); it is trimmed here.",
            ],
        },
        "delivery": {
            "tables": [
                ("F4211 / F42119", "Sales order detail (open / history)"),
                ("F0101", "Address book (ship-to name)"),
            ],
            "ctes": """sd AS (
    SELECT * FROM {{schema}}.F4211
    UNION ALL
    SELECT * FROM {{schema}}.F42119
)""",
            "from": """sd
LEFT JOIN {{schema}}.F0101 ab
       ON ab.ABAN8 = sd.SDSHAN""",
            "where": """COALESCE(sd.SDADDJ, 0) > 0
  AND ('{{order_types}}' = '' OR ARRAY_CONTAINS(TRIM(sd.SDDCTO)::VARIANT, SPLIT('{{order_types}}', ',')))""",
            "fields": {
                "COMPANY_CODE": ("TRIM(sd.SDCO)", "F4211-SDCO"),
                "PLANT": ("TRIM(sd.SDMCU)", "F4211-SDMCU Branch/plant"),
                "STORAGE_LOCATION": (t("sd.SDLOCN"), "F4211-SDLOCN Location"),
                "DELIVERY_NO": ("COALESCE(NULLIF(sd.SDSHPN, 0)::VARCHAR, "
                                "sd.SDDOCO::VARCHAR || '-' || TRIM(sd.SDDCTO))",
                                "F4211-SDSHPN Shipment no. (else order no.)"),
                "DELIVERY_ITEM": ("(sd.SDLNID / 1000)::VARCHAR", "F4211-SDLNID / 1000"),
                "DELIVERY_TYPE": ("TRIM(sd.SDDCTO)", "F4211-SDDCTO Order type"),
                "SOLD_TO": ("sd.SDAN8::VARCHAR", "F4211-SDAN8"),
                "SHIP_TO": ("sd.SDSHAN::VARCHAR", "F4211-SDSHAN"),
                "SHIP_TO_NAME": ("TRIM(ab.ABALPH)", "F0101-ABALPH"),
                "SALES_ORDER_NO": ("sd.SDDOCO::VARCHAR", "F4211-SDDOCO"),
                "SALES_ORDER_ITEM": ("(sd.SDLNID / 1000)::VARCHAR", "F4211-SDLNID / 1000"),
                "CUSTOMER_PO": (t("sd.SDVR01"), "F4211-SDVR01 Reference"),
                "MATERIAL": ("TRIM(sd.SDLITM)", "F4211-SDLITM"),
                "MATERIAL_DESC": ("TRIM(sd.SDDSC1)", "F4211-SDDSC1"),
                "BATCH": (t("sd.SDLOTN"), "F4211-SDLOTN Lot/serial"),
                "SERIAL_NUMBER": (t("sd.SDLOTN"), "F4211-SDLOTN Lot/serial"),
                "DELIVERED_QTY": ("sd.SDSOQS / {{qty_divisor}}", "F4211-SDSOQS Qty shipped"),
                "SALES_UNIT": ("TRIM(sd.SDUOM)", "F4211-SDUOM"),
                "NET_VALUE": ("sd.SDAEXP / {{amt_divisor}}", "F4211-SDAEXP Extended price"),
                "CURRENCY": ("TRIM(sd.SDCRCD)", "F4211-SDCRCD"),
                "GROSS_WEIGHT": ("sd.SDITWT / {{wt_divisor}}", "F4211-SDITWT"),
                "WEIGHT_UNIT": (t("sd.SDWTUM"), "F4211-SDWTUM"),
                "VOLUME": ("sd.SDITVL / {{wt_divisor}}", "F4211-SDITVL"),
                "VOLUME_UNIT": (t("sd.SDVLUM"), "F4211-SDVLUM"),
                "SHIPPING_CONDITION": (t("sd.SDMOT"), "F4211-SDMOT Mode of transport"),
                "INCOTERMS": (t("sd.SDFRTH"), "F4211-SDFRTH Freight handling code"),
                "CARRIER": ("NULLIF(sd.SDCARS, 0)::VARCHAR", "F4211-SDCARS Carrier (address no.)"),
                "REQUESTED_DELIVERY_DATE": (jde_d("sd.SDDRQJ"), "F4211-SDDRQJ"),
                "PLANNED_GI_DATE": (jde_d("sd.SDPPDJ"), "F4211-SDPPDJ Promised ship date"),
                "ACTUAL_GI_DATE": (jde_d("sd.SDADDJ"), "F4211-SDADDJ Actual ship date"),
                "CREATED_ON": (jde_d("sd.SDTRDJ"), "F4211-SDTRDJ Order date"),
                "INVOICE_NO": ("NULLIF(sd.SDDOC, 0)::VARCHAR", "F4211-SDDOC"),
                "INVOICE_DATE": (jde_d("sd.SDIVD"), "F4211-SDIVD"),
                "DELIVERY_STATUS": ("TRIM(sd.SDLTTR) || '/' || TRIM(sd.SDNXTR)", "F4211-SDLTTR / SDNXTR"),
            },
            "notes": [
                "JDE dates are Julian (CYYDDD) and are converted here.",
                "Quantities and amounts are stored with implied decimals. Set the divisors to match "
                "your data dictionary (display decimals).",
                "If Transportation Management is used, F4215 / F4941 hold shipment and load details.",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Oracle E-Business Suite
# ---------------------------------------------------------------------------
def _ebs_serial_from(p):
    base = """{{schema}}.MTL_SERIAL_NUMBERS msn
LEFT JOIN {{schema}}.MTL_SYSTEM_ITEMS_B msi
       ON msi.INVENTORY_ITEM_ID = msn.INVENTORY_ITEM_ID AND msi.ORGANIZATION_ID = msn.CURRENT_ORGANIZATION_ID
LEFT JOIN {{schema}}.MTL_PARAMETERS mp
       ON mp.ORGANIZATION_ID = msn.CURRENT_ORGANIZATION_ID
LEFT JOIN {{schema}}.MTL_ITEM_LOCATIONS mil
       ON mil.INVENTORY_LOCATION_ID = msn.CURRENT_LOCATOR_ID AND mil.ORGANIZATION_ID = msn.CURRENT_ORGANIZATION_ID"""
    if p.get("installed_base") == "Y":
        base += """
LEFT JOIN inst cii
       ON cii.INVENTORY_ITEM_ID = msn.INVENTORY_ITEM_ID AND cii.SERIAL_NUMBER = msn.SERIAL_NUMBER
LEFT JOIN {{schema}}.HZ_CUST_ACCOUNTS hca
       ON hca.CUST_ACCOUNT_ID = cii.OWNER_PARTY_ACCOUNT_ID"""
    return base


def _ebs_serial_ctes(p):
    if p.get("installed_base") != "Y":
        return ""
    return """inst AS (
    SELECT INVENTORY_ITEM_ID, SERIAL_NUMBER, INSTANCE_NUMBER, OWNER_PARTY_ACCOUNT_ID
      FROM {{schema}}.CSI_ITEM_INSTANCES
     WHERE SERIAL_NUMBER IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY INVENTORY_ITEM_ID, SERIAL_NUMBER ORDER BY LAST_UPDATE_DATE DESC) = 1
)"""


def _ebs_ib_fields(p):
    if p.get("installed_base") == "Y":
        return {"CUSTOMER": "hca.ACCOUNT_NUMBER", "CURRENT_CUSTOMER": "hca.ACCOUNT_NUMBER",
                "EQUIPMENT": "cii.INSTANCE_NUMBER::VARCHAR"}
    return {"CUSTOMER": None, "CURRENT_CUSTOMER": None, "EQUIPMENT": None}


EBS = {
    "label": "Oracle E-Business Suite (R12)",
    "vendor": "Oracle",
    "confidence": "high",
    "params": {
        "schema": ("EBS_RAW", "Snowflake schema holding the EBS tables", None),
        "installed_base": ("Y", "Use Installed Base (CSI_ITEM_INSTANCES) for the customer", ["Y", "N"]),
    },
    "objects": {
        "serial": {
            "tables": [
                ("MTL_SERIAL_NUMBERS", "Serial numbers and current status / location"),
                ("MTL_SYSTEM_ITEMS_B", "Item master"),
                ("MTL_PARAMETERS", "Inventory organisation code"),
                ("MTL_ITEM_LOCATIONS", "Locators (bin)"),
                ("CSI_ITEM_INSTANCES", "Installed Base: owner customer (optional)"),
                ("HZ_CUST_ACCOUNTS", "Customer account"),
            ],
            "ctes": _ebs_serial_ctes,
            "from": _ebs_serial_from,
            "where": "",
            "fields": {
                "MATERIAL": ("msi.SEGMENT1", "MTL_SYSTEM_ITEMS_B.SEGMENT1"),
                "SERIAL_NUMBER": ("msn.SERIAL_NUMBER", "MTL_SERIAL_NUMBERS.SERIAL_NUMBER"),
                "PLANT": ("mp.ORGANIZATION_CODE", "MTL_PARAMETERS.ORGANIZATION_CODE"),
                "STORAGE_LOCATION": ("msn.CURRENT_SUBINVENTORY_CODE", "MSN.CURRENT_SUBINVENTORY_CODE"),
                "BIN_LOCATION": ("mil.SEGMENT1", "MTL_ITEM_LOCATIONS.SEGMENT1"),
                "SYSTEM_STATUS": ("""CASE msn.CURRENT_STATUS
        WHEN 1 THEN 'DEFINED NOT USED' WHEN 3 THEN 'IN STORES' WHEN 4 THEN 'ISSUED OUT'
        WHEN 5 THEN 'IN TRANSIT' WHEN 6 THEN 'PENDING' WHEN 7 THEN 'IN RECEIVING'
        WHEN 8 THEN 'IN WIP' ELSE TO_VARCHAR(msn.CURRENT_STATUS) END""",
                                  "MSN.CURRENT_STATUS (decoded)"),
                "USER_STATUS": ("msn.STATUS_ID::VARCHAR", "MSN.STATUS_ID (material status)"),
                "CUSTOMER": ("hca.ACCOUNT_NUMBER", "CSI_ITEM_INSTANCES → HZ_CUST_ACCOUNTS"),
                "CURRENT_CUSTOMER": ("hca.ACCOUNT_NUMBER", "CSI_ITEM_INSTANCES → HZ_CUST_ACCOUNTS"),
                "DESCRIPTION": ("msi.DESCRIPTION", "MTL_SYSTEM_ITEMS_B.DESCRIPTION"),
                "CHANGED_BY": ("msn.LAST_UPDATED_BY::VARCHAR", "MSN.LAST_UPDATED_BY (user id)"),
                "CHANGED_ON": (ts_d("msn.LAST_UPDATE_DATE"), "MSN.LAST_UPDATE_DATE"),
                "CREATED_ON": (ts_d("msn.CREATION_DATE"), "MSN.CREATION_DATE"),
                "EQUIPMENT": ("cii.INSTANCE_NUMBER::VARCHAR", "CSI_ITEM_INSTANCES.INSTANCE_NUMBER"),
                "ACQUISITION_DATE": (ts_d("msn.INITIALIZATION_DATE"), "MSN.INITIALIZATION_DATE"),
                "BATCH": ("msn.LOT_NUMBER", "MSN.LOT_NUMBER"),
            },
            "notes": [
                "Set Installed Base to N if CSI is not implemented; the customer fields then stay empty.",
                "Warranty: use CSI contracts (OKS) if needed; it is not joined by default.",
            ],
        },
        "delivery": {
            "tables": [
                ("WSH_DELIVERY_DETAILS", "Delivery lines ≈ LIPS"),
                ("WSH_DELIVERY_ASSIGNMENTS", "Line → delivery link"),
                ("WSH_NEW_DELIVERIES", "Delivery header ≈ LIKP"),
                ("OE_ORDER_LINES_ALL", "Sales order line number"),
                ("MTL_SYSTEM_ITEMS_B / MTL_PARAMETERS", "Item, organisation"),
                ("HZ_CUST_ACCOUNTS / HZ_PARTIES", "Customer"),
            ],
            "ctes": "",
            "from": """{{schema}}.WSH_DELIVERY_DETAILS wdd
JOIN {{schema}}.WSH_DELIVERY_ASSIGNMENTS wda
       ON wda.DELIVERY_DETAIL_ID = wdd.DELIVERY_DETAIL_ID
JOIN {{schema}}.WSH_NEW_DELIVERIES wnd
       ON wnd.DELIVERY_ID = wda.DELIVERY_ID
LEFT JOIN {{schema}}.OE_ORDER_LINES_ALL oola
       ON oola.LINE_ID = wdd.SOURCE_LINE_ID
LEFT JOIN {{schema}}.MTL_SYSTEM_ITEMS_B msi
       ON msi.INVENTORY_ITEM_ID = wdd.INVENTORY_ITEM_ID AND msi.ORGANIZATION_ID = wdd.ORGANIZATION_ID
LEFT JOIN {{schema}}.MTL_PARAMETERS mp
       ON mp.ORGANIZATION_ID = wdd.ORGANIZATION_ID
LEFT JOIN {{schema}}.HZ_CUST_ACCOUNTS hca
       ON hca.CUST_ACCOUNT_ID = wdd.CUSTOMER_ID
LEFT JOIN {{schema}}.HZ_PARTIES hp
       ON hp.PARTY_ID = hca.PARTY_ID""",
            "where": "wdd.SOURCE_CODE = 'OE' AND wdd.RELEASED_STATUS IN ('C', 'I')",
            "fields": {
                "COMPANY_CODE": ("wdd.ORG_ID::VARCHAR", "WDD.ORG_ID (operating unit)"),
                "PLANT": ("mp.ORGANIZATION_CODE", "MTL_PARAMETERS.ORGANIZATION_CODE"),
                "STORAGE_LOCATION": ("wdd.SUBINVENTORY", "WDD.SUBINVENTORY"),
                "DELIVERY_NO": ("wnd.NAME", "WSH_NEW_DELIVERIES.NAME"),
                "DELIVERY_ITEM": ("wdd.DELIVERY_DETAIL_ID::VARCHAR", "WDD.DELIVERY_DETAIL_ID"),
                "DELIVERY_TYPE": ("wdd.SOURCE_CODE", "WDD.SOURCE_CODE"),
                "SOLD_TO": ("hca.ACCOUNT_NUMBER", "HZ_CUST_ACCOUNTS.ACCOUNT_NUMBER"),
                "SHIP_TO": ("wdd.SHIP_TO_SITE_USE_ID::VARCHAR", "WDD.SHIP_TO_SITE_USE_ID"),
                "SHIP_TO_NAME": ("hp.PARTY_NAME", "HZ_PARTIES.PARTY_NAME"),
                "SALES_ORDER_NO": ("wdd.SOURCE_HEADER_NUMBER", "WDD.SOURCE_HEADER_NUMBER"),
                "SALES_ORDER_ITEM": ("oola.LINE_NUMBER || '.' || oola.SHIPMENT_NUMBER", "OOLA.LINE_NUMBER.SHIPMENT_NUMBER"),
                "CUSTOMER_PO": ("wdd.CUST_PO_NUMBER", "WDD.CUST_PO_NUMBER"),
                "MATERIAL": ("msi.SEGMENT1", "MTL_SYSTEM_ITEMS_B.SEGMENT1"),
                "MATERIAL_DESC": ("wdd.ITEM_DESCRIPTION", "WDD.ITEM_DESCRIPTION"),
                "BATCH": ("wdd.LOT_NUMBER", "WDD.LOT_NUMBER"),
                "SERIAL_NUMBER": ("wdd.SERIAL_NUMBER", "WDD.SERIAL_NUMBER (single serials)"),
                "DELIVERED_QTY": ("wdd.SHIPPED_QUANTITY", "WDD.SHIPPED_QUANTITY"),
                "SALES_UNIT": ("wdd.REQUESTED_QUANTITY_UOM", "WDD.REQUESTED_QUANTITY_UOM"),
                "NET_VALUE": ("wdd.SHIPPED_QUANTITY * wdd.UNIT_PRICE", "SHIPPED_QUANTITY × UNIT_PRICE"),
                "CURRENCY": ("wdd.CURRENCY_CODE", "WDD.CURRENCY_CODE"),
                "GROSS_WEIGHT": ("wdd.GROSS_WEIGHT", "WDD.GROSS_WEIGHT"),
                "WEIGHT_UNIT": ("wdd.WEIGHT_UOM_CODE", "WDD.WEIGHT_UOM_CODE"),
                "VOLUME": ("wdd.VOLUME", "WDD.VOLUME"),
                "VOLUME_UNIT": ("wdd.VOLUME_UOM_CODE", "WDD.VOLUME_UOM_CODE"),
                "SHIPPING_CONDITION": ("wdd.SHIP_METHOD_CODE", "WDD.SHIP_METHOD_CODE"),
                "INCOTERMS": ("wdd.FOB_CODE", "WDD.FOB_CODE"),
                "CARRIER": ("wdd.CARRIER_ID::VARCHAR", "WDD.CARRIER_ID"),
                "TRACKING_NO": ("wdd.TRACKING_NUMBER", "WDD.TRACKING_NUMBER"),
                "REQUESTED_DELIVERY_DATE": (ts_d("wdd.DATE_REQUESTED"), "WDD.DATE_REQUESTED"),
                "PLANNED_GI_DATE": (ts_d("wdd.DATE_SCHEDULED"), "WDD.DATE_SCHEDULED"),
                "ACTUAL_GI_DATE": (ts_d("COALESCE(wnd.CONFIRM_DATE, wnd.INITIAL_PICKUP_DATE)"),
                                   "WND.CONFIRM_DATE / INITIAL_PICKUP_DATE"),
                "CREATED_ON": (ts_d("wdd.CREATION_DATE"), "WDD.CREATION_DATE"),
                "DELIVERY_STATUS": ("wdd.RELEASED_STATUS", "WDD.RELEASED_STATUS (C = shipped, I = interfaced)"),
            },
            "notes": [
                "Only shipped lines (RELEASED_STATUS C/I) from Order Management are included.",
                "Multiple serials per line are held in MTL_SERIAL_NUMBERS_TEMP / WSH_SERIAL_NUMBERS.",
                "Invoice: join RA_CUSTOMER_TRX_LINES_ALL on INTERFACE_LINE_ATTRIBUTE6 = order line id.",
            ],
        },
    },
}
EBS["objects"]["serial"]["dynamic_fields"] = {
    k: (lambda p, k=k: _ebs_ib_fields(p)[k]) for k in ("CUSTOMER", "CURRENT_CUSTOMER", "EQUIPMENT")
}


# ---------------------------------------------------------------------------
# SYSPRO
# ---------------------------------------------------------------------------
SYSPRO = {
    "label": "SYSPRO 8",
    "vendor": "SYSPRO",
    "confidence": "medium",
    "params": {
        "schema": ("SYSPRO_RAW", "Snowflake schema holding the SYSPRO company tables", None),
    },
    "objects": {
        "serial": {
            "tables": [
                ("InvSerialHead", "Serial number header (stock code, serial, warehouse, customer)"),
                ("InvMaster", "Inventory master (description)"),
            ],
            "ctes": "",
            "from": """{{schema}}.InvSerialHead sh
LEFT JOIN {{schema}}.InvMaster im
       ON im.StockCode = sh.StockCode""",
            "where": "TRIM(sh.Serial) <> ''",
            "fields": {
                "MATERIAL": ("TRIM(sh.StockCode)", "InvSerialHead.StockCode"),
                "SERIAL_NUMBER": ("TRIM(sh.Serial)", "InvSerialHead.Serial"),
                "PLANT": (t("sh.Warehouse"), "InvSerialHead.Warehouse"),
                "SYSTEM_STATUS": ("IFF(" + t("sh.Customer") + " IS NULL, 'IN STOCK', 'AT CUSTOMER')",
                                  "derived from InvSerialHead.Customer"),
                "CUSTOMER": (t("sh.Customer"), "InvSerialHead.Customer"),
                "CURRENT_CUSTOMER": (t("sh.Customer"), "InvSerialHead.Customer"),
                "DESCRIPTION": ("TRIM(im.Description)", "InvMaster.Description"),
                "BATCH": (t("sh.Lot"), "InvSerialHead.Lot"),
            },
            "notes": [
                "Serial movements are in InvSerialTrn; use them for history and dates.",
                "Confirm the column names against SYSPRO's table reference for your release.",
            ],
        },
        "delivery": {
            "tables": [
                ("MdnMaster / MdnDetail", "Dispatch note header / lines ≈ LIKP / LIPS"),
                ("SorMaster / SorDetail", "Sales order header / lines"),
                ("ArCustomer", "Customer name"),
            ],
            "ctes": "",
            "from": """{{schema}}.MdnDetail md
JOIN {{schema}}.MdnMaster mm
       ON mm.DispatchNote = md.DispatchNote
LEFT JOIN {{schema}}.SorMaster so
       ON so.SalesOrder = mm.SalesOrder
LEFT JOIN {{schema}}.SorDetail sl
       ON sl.SalesOrder = mm.SalesOrder AND sl.SalesOrderLine = md.SalesOrderLine
LEFT JOIN {{schema}}.ArCustomer cu
       ON cu.Customer = mm.Customer""",
            "where": "COALESCE(md.MQtyToDispatch, 0) <> 0",
            "fields": {
                "PLANT": ("TRIM(md.MWarehouse)", "MdnDetail.MWarehouse"),
                "DELIVERY_NO": ("TRIM(md.DispatchNote)", "MdnDetail.DispatchNote"),
                "DELIVERY_ITEM": ("md.DispatchNoteLine::VARCHAR", "MdnDetail.DispatchNoteLine"),
                "DELIVERY_TYPE": ("'DISPATCH'", "constant"),
                "SOLD_TO": ("TRIM(mm.Customer)", "MdnMaster.Customer"),
                "SHIP_TO": ("TRIM(mm.Customer)", "MdnMaster.Customer"),
                "SHIP_TO_NAME": ("TRIM(cu.Name)", "ArCustomer.Name"),
                "SALES_ORDER_NO": ("TRIM(mm.SalesOrder)", "MdnMaster.SalesOrder"),
                "SALES_ORDER_ITEM": ("md.SalesOrderLine::VARCHAR", "MdnDetail.SalesOrderLine"),
                "CUSTOMER_PO": (t("so.CustomerPoNumber"), "SorMaster.CustomerPoNumber"),
                "MATERIAL": ("TRIM(md.MStockCode)", "MdnDetail.MStockCode"),
                "MATERIAL_DESC": ("TRIM(md.MStockDes)", "MdnDetail.MStockDes"),
                "DELIVERED_QTY": ("md.MQtyToDispatch", "MdnDetail.MQtyToDispatch"),
                "SALES_UNIT": ("TRIM(md.MOrderUom)", "MdnDetail.MOrderUom"),
                "NET_VALUE": ("md.MQtyToDispatch * md.MPrice", "MQtyToDispatch × MPrice"),
                "SHIPPING_CONDITION": (t("so.ShippingInstrsCod"), "SorMaster.ShippingInstrsCod"),
                "REQUESTED_DELIVERY_DATE": (ts_d("so.ReqShipDate"), "SorMaster.ReqShipDate"),
                "PLANNED_GI_DATE": (ts_d("sl.MLineShipDate"), "SorDetail.MLineShipDate"),
                "CREATED_ON": (ts_d("so.OrderDate"), "SorMaster.OrderDate"),
                "DELIVERY_STATUS": ("TRIM(mm.DispatchNoteStatus)", "MdnMaster.DispatchNoteStatus"),
            },
            "notes": [
                "Only applies if dispatch notes are used. Otherwise ship directly from SorDetail.MShipQty.",
                "Actual ship and invoice dates: take them from ArTrnDetail / InvMovements (not joined here).",
                "Confirm the column names against SYSPRO's table reference for your release.",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Microsoft Dynamics 365 Finance & Operations
# ---------------------------------------------------------------------------
D365 = {
    "label": "Microsoft Dynamics 365 F&O",
    "vendor": "Microsoft",
    "confidence": "medium",
    "params": {
        "schema": ("D365_RAW", "Snowflake schema (Synapse Link / Export to Data Lake tables)", None),
        "lang": ("en-us", "Language for product names", None),
        "company": ("", "DATAAREAID filter (legal entity). Leave blank for all", None),
    },
    "objects": {
        "serial": {
            "tables": [
                ("INVENTSERIAL", "Serial numbers"),
                ("INVENTSUM + INVENTDIM", "On-hand by serial (site, warehouse, location, batch)"),
                ("CUSTPACKINGSLIPTRANS / JOUR", "Last packing slip → customer"),
                ("INVENTTABLE / ECORESPRODUCTTRANSLATION", "Product name"),
            ],
            "ctes": """onhand AS (
    SELECT d.DATAAREAID, s.ITEMID, d.INVENTSERIALID, d.INVENTSITEID, d.INVENTLOCATIONID,
           d.WMSLOCATIONID, d.INVENTBATCHID
      FROM {{schema}}.INVENTSUM s
      JOIN {{schema}}.INVENTDIM d
        ON d.DATAAREAID = s.DATAAREAID AND d.INVENTDIMID = s.INVENTDIMID
     WHERE s.PHYSICALINVENT > 0 AND d.INVENTSERIALID <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY d.DATAAREAID, s.ITEMID, d.INVENTSERIALID
                               ORDER BY s.PHYSICALINVENT DESC) = 1
),
sold AS (
    SELECT t.DATAAREAID, t.ITEMID, d.INVENTSERIALID, j.ORDERACCOUNT, t.DELIVERYDATE
      FROM {{schema}}.CUSTPACKINGSLIPTRANS t
      JOIN {{schema}}.CUSTPACKINGSLIPJOUR j
        ON j.DATAAREAID = t.DATAAREAID AND j.PACKINGSLIPID = t.PACKINGSLIPID
       AND j.SALESID = t.SALESID AND j.DELIVERYDATE = t.DELIVERYDATE
      JOIN {{schema}}.INVENTDIM d
        ON d.DATAAREAID = t.DATAAREAID AND d.INVENTDIMID = t.INVENTDIMID
     WHERE d.INVENTSERIALID <> ''
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.DATAAREAID, t.ITEMID, d.INVENTSERIALID
                               ORDER BY t.DELIVERYDATE DESC) = 1
)""",
            "from": """{{schema}}.INVENTSERIAL sr
LEFT JOIN onhand oh
       ON oh.DATAAREAID = sr.DATAAREAID AND oh.ITEMID = sr.ITEMID AND oh.INVENTSERIALID = sr.INVENTSERIALID
LEFT JOIN sold so
       ON so.DATAAREAID = sr.DATAAREAID AND so.ITEMID = sr.ITEMID AND so.INVENTSERIALID = sr.INVENTSERIALID
LEFT JOIN {{schema}}.INVENTTABLE it
       ON it.DATAAREAID = sr.DATAAREAID AND it.ITEMID = sr.ITEMID
LEFT JOIN {{schema}}.ECORESPRODUCTTRANSLATION pt
       ON pt.PRODUCT = it.PRODUCT AND pt.LANGUAGEID = '{{lang}}'""",
            "where": "('{{company}}' = '' OR sr.DATAAREAID = '{{company}}')",
            "fields": {
                "MATERIAL": ("sr.ITEMID", "INVENTSERIAL.ITEMID"),
                "SERIAL_NUMBER": ("sr.INVENTSERIALID", "INVENTSERIAL.INVENTSERIALID"),
                "PLANT": ("oh.INVENTSITEID", "INVENTDIM.INVENTSITEID"),
                "STORAGE_LOCATION": ("oh.INVENTLOCATIONID", "INVENTDIM.INVENTLOCATIONID (warehouse)"),
                "BIN_LOCATION": (t("oh.WMSLOCATIONID"), "INVENTDIM.WMSLOCATIONID"),
                "SYSTEM_STATUS": ("CASE WHEN oh.ITEMID IS NOT NULL THEN 'IN STOCK' "
                                  "WHEN so.ITEMID IS NOT NULL THEN 'DELIVERED' ELSE 'NO STOCK' END",
                                  "derived from INVENTSUM / packing slips"),
                "CUSTOMER": ("so.ORDERACCOUNT", "CUSTPACKINGSLIPJOUR.ORDERACCOUNT (last delivery)"),
                "CURRENT_CUSTOMER": ("IFF(oh.ITEMID IS NULL, so.ORDERACCOUNT, NULL)", "last customer if not in stock"),
                "DESCRIPTION": ("pt.NAME", "ECORESPRODUCTTRANSLATION.NAME"),
                "COMPANY_CODE": ("sr.DATAAREAID", "DATAAREAID"),
                "CHANGED_BY": ("sr.MODIFIEDBY", "INVENTSERIAL.MODIFIEDBY"),
                "CHANGED_ON": (ts_d("sr.MODIFIEDDATETIME"), "INVENTSERIAL.MODIFIEDDATETIME"),
                "CREATED_ON": (ts_d("sr.CREATEDDATETIME"), "INVENTSERIAL.CREATEDDATETIME"),
                "ACQUISITION_DATE": (low_d("sr.PRODDATE"), "INVENTSERIAL.PRODDATE"),
                "BATCH": (t("oh.INVENTBATCHID"), "INVENTDIM.INVENTBATCHID"),
            },
            "notes": [
                "Column names follow the Synapse Link / BYOD table export (upper case).",
                "Warranty / installed base needs Field Service or a customisation; it is not mapped.",
            ],
        },
        "delivery": {
            "tables": [
                ("CUSTPACKINGSLIPJOUR / TRANS", "Packing slip header / lines ≈ LIKP / LIPS"),
                ("INVENTDIM", "Site, warehouse, batch, serial"),
                ("SALESTABLE", "Customer PO, currency"),
                ("CUSTTABLE / DIRPARTYTABLE", "Customer name"),
            ],
            "ctes": "",
            "from": """{{schema}}.CUSTPACKINGSLIPTRANS t
JOIN {{schema}}.CUSTPACKINGSLIPJOUR j
       ON j.DATAAREAID = t.DATAAREAID AND j.PACKINGSLIPID = t.PACKINGSLIPID
      AND j.SALESID = t.SALESID AND j.DELIVERYDATE = t.DELIVERYDATE
LEFT JOIN {{schema}}.INVENTDIM d
       ON d.DATAAREAID = t.DATAAREAID AND d.INVENTDIMID = t.INVENTDIMID
LEFT JOIN {{schema}}.SALESTABLE s
       ON s.DATAAREAID = t.DATAAREAID AND s.SALESID = t.SALESID
LEFT JOIN {{schema}}.CUSTTABLE c
       ON c.DATAAREAID = j.DATAAREAID AND c.ACCOUNTNUM = j.ORDERACCOUNT
LEFT JOIN {{schema}}.DIRPARTYTABLE dp
       ON dp.RECID = c.PARTY""",
            "where": "t.QTY <> 0 AND ('{{company}}' = '' OR t.DATAAREAID = '{{company}}')",
            "fields": {
                "COMPANY_CODE": ("t.DATAAREAID", "DATAAREAID"),
                "PLANT": ("d.INVENTSITEID", "INVENTDIM.INVENTSITEID"),
                "STORAGE_LOCATION": ("d.INVENTLOCATIONID", "INVENTDIM.INVENTLOCATIONID"),
                "DELIVERY_NO": ("t.PACKINGSLIPID", "CUSTPACKINGSLIPTRANS.PACKINGSLIPID"),
                "DELIVERY_ITEM": ("t.LINENUM::VARCHAR", "CUSTPACKINGSLIPTRANS.LINENUM"),
                "DELIVERY_TYPE": ("'PACKING_SLIP'", "constant"),
                "SOLD_TO": ("j.ORDERACCOUNT", "CUSTPACKINGSLIPJOUR.ORDERACCOUNT"),
                "SHIP_TO": ("j.ORDERACCOUNT", "CUSTPACKINGSLIPJOUR.ORDERACCOUNT"),
                "SHIP_TO_NAME": ("COALESCE(" + t("j.DELIVERYNAME") + ", dp.NAME)", "JOUR.DELIVERYNAME / DIRPARTYTABLE.NAME"),
                "SALES_ORDER_NO": ("t.SALESID", "CUSTPACKINGSLIPTRANS.SALESID"),
                "SALES_ORDER_ITEM": ("t.INVENTTRANSID", "CUSTPACKINGSLIPTRANS.INVENTTRANSID"),
                "CUSTOMER_PO": (t("s.PURCHORDERFORMNUM"), "SALESTABLE.PURCHORDERFORMNUM"),
                "MATERIAL": ("t.ITEMID", "CUSTPACKINGSLIPTRANS.ITEMID"),
                "MATERIAL_DESC": ("t.NAME", "CUSTPACKINGSLIPTRANS.NAME"),
                "BATCH": (t("d.INVENTBATCHID"), "INVENTDIM.INVENTBATCHID"),
                "SERIAL_NUMBER": (t("d.INVENTSERIALID"), "INVENTDIM.INVENTSERIALID"),
                "DELIVERED_QTY": ("t.QTY", "CUSTPACKINGSLIPTRANS.QTY"),
                "SALES_UNIT": ("t.SALESUNIT", "CUSTPACKINGSLIPTRANS.SALESUNIT"),
                "NET_VALUE": ("t.VALUEMST", "CUSTPACKINGSLIPTRANS.VALUEMST (accounting currency)"),
                "CURRENCY": ("s.CURRENCYCODE", "SALESTABLE.CURRENCYCODE"),
                "SHIPPING_CONDITION": (t("j.DLVMODE"), "CUSTPACKINGSLIPJOUR.DLVMODE"),
                "INCOTERMS": (t("j.DLVTERM"), "CUSTPACKINGSLIPJOUR.DLVTERM"),
                "REQUESTED_DELIVERY_DATE": (low_d("s.DELIVERYDATE"), "SALESTABLE.DELIVERYDATE"),
                "ACTUAL_GI_DATE": (low_d("t.DELIVERYDATE"), "CUSTPACKINGSLIPTRANS.DELIVERYDATE"),
                "CREATED_ON": (ts_d("j.CREATEDDATETIME"), "CUSTPACKINGSLIPJOUR.CREATEDDATETIME"),
                "DELIVERY_STATUS": ("'C'", "packing slip posted = shipped"),
            },
            "notes": [
                "VALUEMST is in the company's accounting currency; CURRENCY shows the order currency.",
                "Carrier and tracking come from WHSSHIPMENTTABLE / TMS (if WMS/TMS are used).",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Oracle NetSuite (SuiteAnalytics / connector tables)
# ---------------------------------------------------------------------------
NETSUITE = {
    "label": "Oracle NetSuite",
    "vendor": "Oracle",
    "confidence": "medium",
    "params": {
        "schema": ("NETSUITE", "Snowflake schema (SuiteAnalytics Connect / Fivetran netsuite2)", None),
    },
    "objects": {
        "serial": {
            "tables": [
                ("INVENTORYNUMBER", "Serial / lot numbers"),
                ("INVENTORYBALANCE", "On-hand by item, location, bin, number"),
                ("ITEM / LOCATION", "Item and location master"),
            ],
            "ctes": """bal AS (
    SELECT ITEM, INVENTORYNUMBER, LOCATION, BINNUMBER, QUANTITYONHAND
      FROM {{schema}}.INVENTORYBALANCE
     WHERE QUANTITYONHAND <> 0
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ITEM, INVENTORYNUMBER ORDER BY QUANTITYONHAND DESC) = 1
)""",
            "from": """{{schema}}.INVENTORYNUMBER n
LEFT JOIN {{schema}}.ITEM i
       ON i.ID = n.ITEM
LEFT JOIN bal b
       ON b.ITEM = n.ITEM AND b.INVENTORYNUMBER = n.ID
LEFT JOIN {{schema}}.LOCATION l
       ON l.ID = b.LOCATION""",
            "where": "",
            "fields": {
                "MATERIAL": ("i.ITEMID", "ITEM.ITEMID"),
                "SERIAL_NUMBER": ("n.INVENTORYNUMBER", "INVENTORYNUMBER.INVENTORYNUMBER"),
                "PLANT": ("l.NAME", "LOCATION.NAME"),
                "BIN_LOCATION": ("b.BINNUMBER::VARCHAR", "INVENTORYBALANCE.BINNUMBER"),
                "SYSTEM_STATUS": ("IFF(b.ITEM IS NOT NULL, 'IN STOCK', 'NOT IN STOCK')", "derived from INVENTORYBALANCE"),
                "DESCRIPTION": ("i.DISPLAYNAME", "ITEM.DISPLAYNAME"),
                "WARRANTY_END": (ts_d("n.EXPIRATIONDATE"), "INVENTORYNUMBER.EXPIRATIONDATE (expiry)"),
            },
            "notes": [
                "NetSuite has no installed base. Find the customer via the Item Fulfillment that "
                "consumed the serial (INVENTORYASSIGNMENT → TRANSACTION).",
                "Table names follow the NetSuite2.com analytics data source.",
            ],
        },
        "delivery": {
            "tables": [
                ("TRANSACTION (type ItemShip)", "Item Fulfillment ≈ delivery header"),
                ("TRANSACTIONLINE", "Fulfillment lines"),
                ("TRANSACTION (SalesOrd)", "Source sales order (via CREATEDFROM)"),
                ("ITEM / LOCATION / CUSTOMER", "Masters"),
            ],
            "ctes": "",
            "from": """{{schema}}.TRANSACTION f
JOIN {{schema}}.TRANSACTIONLINE fl
       ON fl.TRANSACTION = f.ID
LEFT JOIN {{schema}}.TRANSACTION so
       ON so.ID = fl.CREATEDFROM
LEFT JOIN {{schema}}.ITEM i
       ON i.ID = fl.ITEM
LEFT JOIN {{schema}}.LOCATION l
       ON l.ID = fl.LOCATION
LEFT JOIN {{schema}}.CUSTOMER c
       ON c.ID = f.ENTITY""",
            "where": "f.TYPE = 'ItemShip' AND fl.MAINLINE = 'F' AND fl.ITEM IS NOT NULL",
            "fields": {
                "COMPANY_CODE": ("fl.SUBSIDIARY::VARCHAR", "TRANSACTIONLINE.SUBSIDIARY"),
                "PLANT": ("l.NAME", "LOCATION.NAME"),
                "DELIVERY_NO": ("f.TRANID", "TRANSACTION.TRANID"),
                "DELIVERY_ITEM": ("fl.ID::VARCHAR", "TRANSACTIONLINE.ID"),
                "DELIVERY_TYPE": ("f.TYPE", "TRANSACTION.TYPE"),
                "SOLD_TO": ("c.ENTITYID", "CUSTOMER.ENTITYID"),
                "SHIP_TO": ("c.ENTITYID", "CUSTOMER.ENTITYID"),
                "SHIP_TO_NAME": ("c.COMPANYNAME", "CUSTOMER.COMPANYNAME"),
                "SALES_ORDER_NO": ("so.TRANID", "TRANSACTION.TRANID (sales order)"),
                "CUSTOMER_PO": ("so.OTHERREFNUM", "TRANSACTION.OTHERREFNUM"),
                "MATERIAL": ("i.ITEMID", "ITEM.ITEMID"),
                "MATERIAL_DESC": ("i.DISPLAYNAME", "ITEM.DISPLAYNAME"),
                "DELIVERED_QTY": ("ABS(fl.QUANTITY)", "TRANSACTIONLINE.QUANTITY"),
                "SHIPPING_CONDITION": ("f.SHIPMETHOD::VARCHAR", "TRANSACTION.SHIPMETHOD"),
                "CARRIER": ("f.SHIPCARRIER", "TRANSACTION.SHIPCARRIER"),
                "ACTUAL_GI_DATE": (ts_d("f.TRANDATE"), "TRANSACTION.TRANDATE"),
                "CREATED_ON": (ts_d("f.CREATEDDATE"), "TRANSACTION.CREATEDDATE"),
                "DELIVERY_STATUS": ("f.STATUS", "TRANSACTION.STATUS (A picked, B packed, C shipped)"),
            },
            "notes": [
                "Fulfillment line quantities can be negative depending on the connector; ABS() is applied.",
                "Tracking numbers: TRANSACTION.TRACKINGNUMBERLIST or the package sublist, depending on setup.",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Blue Yonder (JDA / RedPrairie) WMS
# ---------------------------------------------------------------------------
BLUEYONDER = {
    "label": "Blue Yonder WMS (JDA / RedPrairie)",
    "vendor": "Blue Yonder",
    "confidence": "medium",
    "params": {
        "schema": ("BY_WMS", "Snowflake schema holding the WMS tables", None),
        "wh_id": ("", "Warehouse (wh_id) filter. Leave blank for all", None),
    },
    "objects": {
        "serial": {
            "tables": [
                ("inv_ser_num", "Serial numbers captured against inventory"),
                ("invdtl / invsub / invlod", "Inventory detail → sub-load → load (location)"),
            ],
            "ctes": "",
            "from": """{{schema}}.inv_ser_num s
JOIN {{schema}}.invdtl dtl
       ON dtl.dtlnum = s.invtid
LEFT JOIN {{schema}}.invsub sub
       ON sub.subnum = dtl.subnum
LEFT JOIN {{schema}}.invlod lod
       ON lod.lodnum = sub.lodnum""",
            "where": "('{{wh_id}}' = '' OR lod.wh_id = '{{wh_id}}')",
            "fields": {
                "MATERIAL": ("dtl.prtnum", "invdtl.prtnum"),
                "SERIAL_NUMBER": ("s.ser_num", "inv_ser_num.ser_num"),
                "PLANT": ("lod.wh_id", "invlod.wh_id"),
                "BIN_LOCATION": ("lod.stoloc", "invlod.stoloc"),
                "SYSTEM_STATUS": ("dtl.invsts", "invdtl.invsts (inventory status)"),
                "COMPANY_CODE": ("dtl.prt_client_id", "invdtl.prt_client_id"),
                "BATCH": (t("dtl.lotnum"), "invdtl.lotnum"),
                "CREATED_ON": (ts_d("s.ins_dt"), "inv_ser_num.ins_dt"),
            },
            "notes": [
                "Only serials still in the warehouse are in invdtl. Shipped serials move to the "
                "archive / shipped-inventory tables.",
                "Serial capture level (load / sub / detail) varies; here the serial is linked at detail level.",
            ],
        },
        "delivery": {
            "tables": [
                ("shipment / shipment_line", "Shipment header / lines ≈ LIKP / LIPS"),
                ("ord / ord_line", "Customer order header / lines"),
                ("adrmst", "Ship-to address name"),
            ],
            "ctes": "",
            "from": """{{schema}}.shipment_line sl
JOIN {{schema}}.shipment sh
       ON sh.ship_id = sl.ship_id AND sh.wh_id = sl.wh_id
LEFT JOIN {{schema}}.ord o
       ON o.ordnum = sl.ordnum AND o.client_id = sl.client_id AND o.wh_id = sl.wh_id
LEFT JOIN {{schema}}.ord_line ol
       ON ol.ordnum = sl.ordnum AND ol.ordlin = sl.ordlin AND ol.ordsln = sl.ordsln
      AND ol.client_id = sl.client_id AND ol.wh_id = sl.wh_id
LEFT JOIN {{schema}}.adrmst a
       ON a.adr_id = o.st_adr_id""",
            "where": "COALESCE(sl.shpqty, 0) <> 0 AND ('{{wh_id}}' = '' OR sl.wh_id = '{{wh_id}}')",
            "fields": {
                "COMPANY_CODE": ("sl.client_id", "shipment_line.client_id"),
                "PLANT": ("sh.wh_id", "shipment.wh_id"),
                "DELIVERY_NO": ("sh.ship_id", "shipment.ship_id"),
                "DELIVERY_ITEM": ("sl.ship_line_id", "shipment_line.ship_line_id"),
                "DELIVERY_TYPE": ("o.ordtyp", "ord.ordtyp"),
                "SOLD_TO": ("o.btcust", "ord.btcust"),
                "SHIP_TO": ("o.stcust", "ord.stcust"),
                "SHIP_TO_NAME": ("a.adrnam", "adrmst.adrnam"),
                "SALES_ORDER_NO": ("sl.ordnum", "shipment_line.ordnum"),
                "SALES_ORDER_ITEM": ("sl.ordlin || '.' || sl.ordsln", "ordlin.ordsln"),
                "CUSTOMER_PO": ("o.cponum", "ord.cponum"),
                "MATERIAL": ("ol.prtnum", "ord_line.prtnum"),
                "DELIVERED_QTY": ("sl.shpqty", "shipment_line.shpqty"),
                "SHIPPING_CONDITION": ("sh.srvlvl", "shipment.srvlvl"),
                "CARRIER": ("sh.carcod", "shipment.carcod"),
                "ACTUAL_GI_DATE": (ts_d("sh.loddte"), "shipment.loddte (loaded)"),
                "CREATED_ON": (ts_d("o.entdte"), "ord.entdte"),
                "DELIVERY_STATUS": ("sh.shpsts", "shipment.shpsts"),
            },
            "notes": [
                "Item descriptions are in prtdsc (keyed by colnam / colval); not joined here.",
                "Trailer, dispatch date and tracking: stop → car_move → trlr (dispatch_dte).",
                "Older versions use ord.stcust only (no st_adr_id). Adjust the adrmst join if so.",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Infor Nexus (GT Nexus) – JSON landed in Snowflake
# ---------------------------------------------------------------------------
NEXUS = {
    "label": "Infor Nexus (GT Nexus)",
    "vendor": "Infor",
    "confidence": "low",
    "params": {
        "schema": ("NEXUS_RAW", "Snowflake schema where API payloads land", None),
        "table": ("SHIPMENT_JSON", "Table holding one shipment document per row", None),
        "json_col": ("PAYLOAD", "VARIANT column with the JSON document", None),
        "lines_path": ("shipmentItem", "JSON array holding the shipment lines", None),
    },
    "objects": {
        "delivery": {
            "tables": [
                ("<table>.<json_col>", "Shipment / packing list JSON from the Infor Nexus REST API"),
            ],
            "ctes": "",
            "from": """{{schema}}.{{table}} r,
LATERAL FLATTEN(input => r.{{json_col}}:{{lines_path}}, OUTER => TRUE) ln""",
            "where": "",
            "fields": {
                "PLANT": ("r.{{json_col}}:portOfLoading::VARCHAR", ":portOfLoading"),
                "DELIVERY_NO": ("r.{{json_col}}:shipmentNumber::VARCHAR", ":shipmentNumber"),
                "DELIVERY_ITEM": ("ln.index::VARCHAR", "line index"),
                "DELIVERY_TYPE": ("r.{{json_col}}:type::VARCHAR", ":type"),
                "CUSTOMER_PO": ("ln.value:poNumber::VARCHAR", "line :poNumber"),
                "MATERIAL": ("ln.value:itemKey::VARCHAR", "line :itemKey"),
                "MATERIAL_DESC": ("ln.value:itemDescription::VARCHAR", "line :itemDescription"),
                "DELIVERED_QTY": ("ln.value:quantity::NUMBER(38,6)", "line :quantity"),
                "SALES_UNIT": ("ln.value:quantityUom::VARCHAR", "line :quantityUom"),
                "GROSS_WEIGHT": ("ln.value:grossWeight::NUMBER(38,6)", "line :grossWeight"),
                "SHIPPING_CONDITION": ("r.{{json_col}}:modeOfTransport::VARCHAR", ":modeOfTransport"),
                "INCOTERMS": ("r.{{json_col}}:incoterm::VARCHAR", ":incoterm"),
                "CARRIER": ("r.{{json_col}}:carrier.name::VARCHAR", ":carrier.name"),
                "TRACKING_NO": ("r.{{json_col}}:billOfLadingNumber::VARCHAR", ":billOfLadingNumber"),
                "REQUESTED_DELIVERY_DATE": ("TRY_TO_DATE(r.{{json_col}}:estimatedArrivalDate::VARCHAR)", ":estimatedArrivalDate (ETA)"),
                "PLANNED_GI_DATE": ("TRY_TO_DATE(r.{{json_col}}:estimatedDepartureDate::VARCHAR)", ":estimatedDepartureDate (ETD)"),
                "ACTUAL_GI_DATE": ("TRY_TO_DATE(r.{{json_col}}:actualDepartureDate::VARCHAR)", ":actualDepartureDate (ATD)"),
                "DELIVERY_STATUS": ("r.{{json_col}}:status::VARCHAR", ":status"),
            },
            "notes": [
                "Infor Nexus is a SaaS network with no database access. Data comes from its REST API "
                "(e.g. ShipmentDetail / PackingList) and lands in Snowflake as JSON.",
                "The JSON paths are placeholders. Map them to your actual payload (check one document "
                "with SELECT PAYLOAD FROM ... LIMIT 1).",
                "It is an inbound / ocean logistics view (ETD/ATD/ETA), not an ERP delivery.",
            ],
        },
    },
}


ERPS = {
    "SAP": SAP,
    "ISCALA": ISCALA,
    "JDE": JDE,
    "EBS": EBS,
    "SYSPRO": SYSPRO,
    "D365": D365,
    "NETSUITE": NETSUITE,
    "BLUEYONDER": BLUEYONDER,
    "NEXUS": NEXUS,
}
