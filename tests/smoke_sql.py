import sys, sqlglot
sys.path.insert(0, ".")
from services.erp_knowledge_service import get_repository
from services.mapping_engine import build_mapping, default_columns_for
from services.sql_service import generate_all
repo = get_repository()
fails = 0
for erp_id, erp in repo.erps.items():
    for bo in erp.business_objects:
        cols = default_columns_for(repo, erp_id, bo) + ["Nonexistent Thing"]
        r = build_mapping(repo, erp_id, cols, bo)
        for k, v in generate_all(r, erp).items():
            if "dbt" in k:
                continue
            try:
                sqlglot.parse(v.replace("$", "_v_"), read="snowflake")
            except Exception as e:
                fails += 1
                print("FAIL", erp_id, bo, k, str(e)[:300])
        unreach = [w for w in r.warnings if "No known join path" in w]
        if unreach:
            print("UNREACH", erp_id, bo, unreach[:2])
print("fails", fails)
