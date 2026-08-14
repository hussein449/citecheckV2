# Regression corpus

Real papers that have broken the parser at some point. `tests/test_corpus.py`
reads them straight from disk — no network, no API keys.

The PDFs are **not committed**: they are published articles belonging to their
publishers, and one of them is 8 MB. Tests skip any paper that is missing, so a
fresh checkout runs green with no setup. Format coverage that must hold on every
checkout lives in `tests/test_styles.py`, which needs no files.

## Populating it

Drop the PDFs in this directory, or leave them in `uploads/` where the app puts
them — both are searched. Files are matched on the end of the filename, so the
run-id prefix the app adds (`20260806-193217-44149f_j.jclinepi…`) is fine.

| Filename ends with | Why it is here |
|---|---|
| `Drone Logistics (2).pdf` | Author-year review whose summary tables have a `[126]` "Study ID" column. Those cells used to outvote the real citations and blank the whole run. |
| `j.jclinepi.2022.03.004.pdf` | Two-column Elsevier paper. Column interleaving shredded the reference list into alternating halves. |
| `s13638-024-02373-5.pdf` | Springer, numeric. |
| `Flying_ad_hoc_paper_1.pdf` | 149 references, numeric — the long case. |
| `2017STOPSpeedRadar.pdf` | IEEE, numeric, quoted titles. |
| `test_paper.pdf` | Tiny five-reference smoke case. |

## Adding a paper

Add a row to `CORPUS` in `test_corpus.py` with the expected style and the
*minimum* references, linked references and *maximum* orphans. They are bounds,
not exact counts: parsing more references than expected is an improvement, not a
regression, and should never require editing the test.
