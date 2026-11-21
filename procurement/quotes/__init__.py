"""Step 4: read vendor quotations (PDF) into one shape, and re-price them on the tender quantities.

- parse.py   PDF text layer -> Quote (no LLM). A scan without a text layer is marked "needs vision".
- compare.py Re-price every quote on the same tender quantities; flag unit mismatches, lines that
             can't be compared (not measured on the site sheet) and quantity drift. Plain arithmetic.
- pack.py    Finds the files in the data pack (git-ignored), caches the reads, writes a deals seed.
- api.py     /api/quotes/...

No real vendor names live in this code: they come from the quotation file names at runtime.
"""
