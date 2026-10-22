Saved product pages for the scraper tests. Each `<site>/<name>.html` has a
`<name>.expected.json` next to it: what the extractors should return.
Add one with `python3 capture_fixture.py mecca|goto <url> <name>` (see that
file's header). `test_scraper_fixtures.py` replays them all.
