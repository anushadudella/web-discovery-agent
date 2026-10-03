# web-discovery-agent

Finds free events/freebies for Merch-supported colleges and feeds them into the existing
candidates -> review -> publish pipeline. **Correctness before volume**: every candidate
is `needs_review=True` until a source has passed real evals.

## Sources
- `src/feed_client.py`: reads official calendar ICS feeds (free, no AI). Preferred where a feed exists.
- `src/sonar_client.py`: Perplexity Sonar web-search discovery (needs `PERPLEXITY_API_KEY`).

## Quick start
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v

# Free path (no API key): paste a real ICS URL into config/colleges/<college>.yaml (feed_urls)
export FEED_CONTACT="you@example.com"      # identifies our bot to the calendar host
python -m src.run_feed --college ut-austin
python -m src.run_feed --college ut-austin --file saved.ics   # offline

# AI path
export PERPLEXITY_API_KEY=...
python -m src.run_sonar --college ut-austin --dry-run
python -m src.run_sonar --college ut-austin --days 7
```
Every run is saved to `runs/<college>/` (raw request/response, plus the raw `.ics`) for evals.

## Not built yet
Page verifier, UT known-places list + location ladder, dedup against Merch posts, review-queue hand-off, evals on real saved data.
