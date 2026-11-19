"""Build the SAP S/4HANA sample project from the sample CSV files.

    python -m sample_data.build_sample_project    (run from the project root)
"""
from __future__ import annotations

from config import SAMPLE_DATA_DIR
from mapping_engine import run_analysis
from metadata_parser import normalize_source_metadata, normalize_targets, read_any
from models import MappingProject

CONTEXT = """Map outbound delivery and shipment information from SAP S/4HANA into one delivery-item level view.
Identify the relationships between delivery header (LIKP), delivery item (LIPS), shipment header (VTTK)
and shipment item (VTTP). Required attributes: delivery number, delivery item, delivery type, sales organization,
shipping point, material, plant and plant name, storage location, movement type, delivered quantity and unit,
delivery and goods issue dates, shipment number, shipment type, shipment start date and carrier.
A delivery is normally assigned to one shipment; multi-leg shipments must not duplicate delivery items."""


def build() -> MappingProject:
    p = MappingProject()
    meta_file = SAMPLE_DATA_DIR / "sap_s4hana_delivery_shipment_metadata.csv"
    tgt_file = SAMPLE_DATA_DIR / "sap_delivery_target_columns.csv"
    meta, _ = normalize_source_metadata(read_any(meta_file.name, meta_file.read_bytes())[0])
    tgts, _ = normalize_targets(read_any(tgt_file.name, tgt_file.read_bytes())[0])
    p.source_metadata = meta.to_dict("records")
    p.target_columns = tgts.to_dict("records")
    p.business_context = CONTEXT
    p.config.update({
        "project_name": "SAP S4HANA Delivery-Shipment Item", "source_erps": ["SAP S/4HANA"],
        "source_module": "Delivery", "business_process": "Outbound delivery and shipment",
        "source_database": "RAW_SAP", "source_schema": "SAP_S4", "target_database": "EDW",
        "target_schema": "LOGISTICS", "target_object": "DELIVERY_SHIPMENT_ITEM", "target_object_type": "VIEW",
        "mapping_type": "Single ERP", "history_requirement": "Current records only",
        "expected_grain": "One row per delivery item", "load_type": "Incremental (MERGE)", "sap_client": "100",
        "author": "ERP Mapping Studio sample",
    })
    run_analysis(p)
    p.save_version("Sample: initial generated mapping")
    return p


if __name__ == "__main__":
    proj = build()
    out = SAMPLE_DATA_DIR / "sample_sap_s4hana_delivery_project.json"
    out.write_text(proj.to_json(), encoding="utf-8")
    print(f"Wrote {out}")
