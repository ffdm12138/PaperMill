# Example input: paper_raw/0000000000000001/

This is a sample MinerU Markdown excerpt for an unmatched paper_raw folder.
The metadata.json is not frozen yet and has an empty
`identifiers.doi`. The resolver skill reads this Markdown header region to
find a DOI and bibliographic candidates.

## 0000000000000001.metadata.json (state)

```json
{
  "paper_number": "0000000000000001",
  "paper_raw_id": "0000000000000001",
  "source_type": "manual_pdf",
  "title": { "original": "", "subtitle": "" },
  "authors": [{ "full_name": "", "family": "", "given": "", "orcid": "", "affiliation": "" }],
  "year": null,
  "container": { "journal": "" },
  "identifiers": { "doi": "" }
}
```

## 0000000000000001.md (excerpt, header region)

```markdown
# Simulation of wind-induced snow transport and sublimation in alpine terrain

Vionnet, V., Martin, E., Masson, V., et al.

The Cryosphere, 8, 395–414, 2014
https://doi.org/10.5194/tc-8-395-2014

## Abstract
We develop a fully coupled snowpack/atmosphere model...

## References
1. Bagnold, R. A. (1941). The Physics of Blown Sand... doi:10.1007/978-3-642-61171-1_19
```

Note: the DOI `10.5194/tc-8-395-2014` appears in the header region (before
`## References`) and is a valid candidate. The DOI `10.1007/978-3-642-61171-1_19`
appears only in the References section and must NOT be used as this paper's DOI.
