# Limitations

The generated mock replays observed responses and does not model state, authentication, or business rules.

Website crawling is intentionally bounded by page count, depth, concurrency, download
bytes, and a small number of query variants per path. Dynamic links that appear only
after arbitrary JavaScript execution require the optional authorized Playwright pass.
Robots exclusions and crawl delays are honored when available; CAPTCHA, access-control
bypass, and authenticated scraping are not attempted.
