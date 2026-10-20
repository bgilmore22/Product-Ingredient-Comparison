"""
run_manual.py - runs both scrapers against the saved fixture HTML and writes
real JSON output to the project's /data folder.

This is a manual smoke-test, separate from pytest. The tests only check the
extraction functions return the right strings - they never call
scrape_and_save(), so they never produce a file. This script does.

Run from Product 1/:
    python run_manual.py
"""
import pathlib

import src1_scraper
import src2_scraper

ICONIC_HTML = pathlib.Path("test_cases/src1_cases/iconic_full_details.html").read_text(encoding="utf-8")
ICONIC_URL = "https://www.theiconic.com.au/bioactive-collagen-peptides-hyaluronic-acid-vitamin-c-wellness-powder-unflavoured-1350938.html"

INCA_HTML = pathlib.Path("test_cases/src2_cases/inca_full_details.html").read_text(encoding="utf-8")
INCA_URL = "https://incaorganics.com.au/products/bioactive-pure-collagen-peptides-hyaluronic-acid-vitamin-c-unflavoured-230g"

if __name__ == "__main__":
    path1 = src1_scraper.scrape_and_save(ICONIC_HTML, ICONIC_URL)
    print(f"Wrote: {path1}")

    path2 = src2_scraper.scrape_and_save(INCA_HTML, INCA_URL)
    print(f"Wrote: {path2}")