# Example input summary

The curator sees a `data/paper_raw/<paper_number>/` folder. This file summarises
the two inputs the skill reads: the metadata JSON and the MinerU Markdown.

## metadata (excerpt)

```json
{
  "schema_version": "1.0",
  "paper_number": "0000000000000001",
  "paper_raw_id": "0000000000000001",
  "title": { "original": "A bulk blowing-snow model", "translated_zh": "", "short_zh": "" },
  "authors": [ { "full_name": "Stephen J. Déry", "family": "Déry", "given": "Stephen J.", "orcid": "", "affiliation": "" } ],
  "first_author": { "family": "Déry", "display": "Déry" },
  "year": 1999,
  "container": { "journal": "Boundary-Layer Meteorology", "booktitle": "", "conference": "", "series": "", "publisher": "", "institution": "", "school": "" },
  "identifiers": { "doi": "10.1023/A:100052170", "arxiv_id": "", "isbn": "", "issn": "" },
  "metadata_match": { "status": "matched", "source": "crossref", "confidence": 0.93 }
}
```

## markdown excerpt

```markdown
# A bulk blowing-snow model

Stephen J. Déry, M. K. Yau
Boundary-Layer Meteorology (1999)

A one-dimensional bulk model of blowing snow is developed ...
```


