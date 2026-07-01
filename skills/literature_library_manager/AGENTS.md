# Literature Library Manager

Formal imports must use `data/paper_raw/<000001>/` and the v2 CLIs. API and writing read from `all.catalog.json` and `paper_number_ledger.json`.

Start/reuse persistent MinerU API with `python scripts/start_mineru_services.py --wait`
and stop it with `python scripts/stop_mineru_services.py`. MinerU conversion has
no process-level timeout; metadata title/author/affiliation/abstract/keyword/DOI
candidates prefer converted Markdown first 100 lines as front-matter evidence
before PDF title fallback.
Catalog natural-language values default to Chinese; JSON keys/schema enums stay
English, technical terms may remain English, and metadata remains original
bibliographic facts.
