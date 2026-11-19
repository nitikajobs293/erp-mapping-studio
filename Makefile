.PHONY: install run test test-pg security lint docker
install:   ; pip install -r requirements-dev.txt
run:       ; python scripts/init_db.py && streamlit run app.py
test:      ; pytest
test-pg:   ; TEST_DATABASE_URL=$${TEST_DATABASE_URL:?set TEST_DATABASE_URL} pytest
security:  ; bandit -c pyproject.toml -q -r erpdm ui app.py && pip-audit -r requirements.txt
lint:      ; ruff check erpdm ui app.py tests
docker:    ; docker compose up --build
