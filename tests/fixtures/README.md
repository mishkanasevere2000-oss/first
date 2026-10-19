# Synthetic bank fixtures

`sber_credit_synthetic.txt` and `sber_credit_synthetic.pdf` contain entirely
invented test data: an all-zero account, generic TEST counterparties, fictional
amounts and codes. They are not an anonymized copy of an actual customer's
statement and contain no customer transactions, identity or QR/signature data.

The text includes two identical purchases, a wrapped description and a July
operation processed in August. The text is a readable source for the fixture;
the PDF was generated once from these lines using ReportLab with an embedded
DejaVu Sans Mono font. ReportLab is not needed to install or run the application
or its tests. The normal tests exercise actual PDF extraction using pypdf.

Never put real statements or extracted customer text in this directory.

`legacy_stage2_synthetic.db` was created with the released v0.3.0 Python code
using the same synthetic PDF, plus an explicitly synthetic manual decision and
rule. It is a schema-1 compatibility fixture, never a copy of a real bank database.
It verifies migrations and preservation of original IDs/decisions/rule versions.
`tests/pdf_variants.py` generates shorter, longer and re-encoded synthetic PDF
variants at test time using pypdf; no additional dependency is required.

`ozon_synthetic.txt` and `ozon_synthetic.pdf` are independently invented, two-page
fixtures with an all-zero account, TEST-only descriptions, four fictional
operations and a wrapped document number. Generated with ReportLab and the
same DejaVu font; ReportLab remains unnecessary at runtime or in tests.
Tests create shorter, re-encoded and conflicting variants via pypdf.
