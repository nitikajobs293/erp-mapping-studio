"""Connected workflow state carried across modules (so users never re-enter information)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from erpdm.schemas import JoinPlan, SelectedColumn


class WorkflowState(BaseModel):
    erp_code: str | None = None
    tcode: str | None = None
    use_case_text: str | None = None
    use_case_name: str | None = None
    tables: list[str] = Field(default_factory=list)
    root_table: str | None = None
    columns: list[SelectedColumn] = Field(default_factory=list)
    include_pending: bool = False
    prefer_relationship_ids: list[int] = Field(default_factory=list)
    plan: JoinPlan | None = None
    dialect: str = "Snowflake"
    sql: str | None = None
    sql_meta: dict[str, Any] | None = None      # GeneratedSQL dump
    mapping_id: int | None = None
    dbt_files: list[str] = Field(default_factory=list)
    dbt_models: list[str] = Field(default_factory=list)
    history_required: bool = False

    def reset_for_erp(self, erp_code: str) -> None:
        if erp_code != self.erp_code:
            fresh = WorkflowState(erp_code=erp_code)
            for k in self.model_fields:
                setattr(self, k, getattr(fresh, k))

    def set_scope(self, tables: list[str], root: str | None, columns: list[SelectedColumn], *,
                  tcode: str | None = None, use_case_text: str | None = None, use_case_name: str | None = None,
                  plan: JoinPlan | None = None) -> None:
        self.tables, self.root_table, self.columns = list(dict.fromkeys(tables)), root, columns
        self.tcode, self.use_case_text, self.use_case_name = tcode, use_case_text, use_case_name
        self.plan = plan
        self.sql = self.sql_meta = None
        self.mapping_id = None
        self.dbt_files, self.dbt_models = [], []

    def steps(self) -> list[tuple[str, bool]]:
        return [("ERP selected", bool(self.erp_code)),
                ("T-Code / use case", bool(self.tcode or self.use_case_text)),
                ("Tables & joins", bool(self.plan)),
                ("Columns selected", bool(self.columns)),
                ("SQL generated", bool(self.sql)),
                ("Mapping created", bool(self.mapping_id)),
                ("dbt generated", bool(self.dbt_models))]
