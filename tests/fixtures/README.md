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
