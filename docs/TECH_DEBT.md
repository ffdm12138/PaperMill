# Technical Debt

## Planned v2_library.py split

`src/services/v2_library.py` still combines ingest id allocation, ledger,
converter, curation, commit, readiness, and catalog build logic. The ingest
v2.3 paper_number workspace migration intentionally does not split it.

Future refactor should separate:

- `src/services/paper_number_ledger.py`
- `src/services/paper_raw_readiness.py`
- `src/services/paper_raw_contract.py`
- `src/services/all_catalog_builder.py`
