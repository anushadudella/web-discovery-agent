# Project Plan: Web Discovery Agent (Per-College Freebie Scraping)

## 1. Executive Summary

Build a bot that scrapes legitimate public sources for each Merch-supported college, finds real freebie/event opportunities as they're posted, and turns them into real Merch posts — automatically when we're confident, held for a quick human check when we're not. Same job the GroupMe bot already does for one source (a single GroupMe chat); this project extends that same idea to the open web, across every college we support.

This isn't a new system from scratch — it plugs into the discovery pipeline that already exists and is already live in production (candidates → review → publish, currently fed by the GroupMe bot). This project's job is to add new ways for candidates to get into that pipeline, not to rebuild it.

## 2. Background & Problem Statement

Right now, freebie posts on Merch come from two places: students posting them directly in the app, and our GroupMe bot picking them up from one chat at one school. That leaves a lot on the table — most colleges have official channels (event calendars, student org platforms, dining/rec-center announcements) that regularly post free food, giveaways, and events, and nobody's watching them. If we can reliably turn those into real Merch posts, every supported college gets a denser, more useful feed on day one, without waiting on students to post things themselves.

## 3. Goals & Objectives

- **Find real freebies/events automatically, every day, per college.** The bot should be configurable per college — point it at a school, and it finds what's actually being posted there.
- **Only use legitimate sources.** Official university event calendars, student organization platforms, department pages, dining/rec-center announcements — public, above-board sources a college would recognize as its own. Nothing that requires bypassing a login, privacy setting, or terms of service to access. (Sources are the intern's call — this is deliberately open-ended — but every source should be one a college could see us using and have no objection to.)
- **Ensure post accuracy.** Title, location (a real, correct map pin — not a guess), image, date/time, and college attribution all need to actually be right. This has been the single biggest failure point in the GroupMe bot so far — getting this right is the actual hard part of this project, not the scraping itself.
- **Rigorous testing.** Before anything from a new source is trusted to auto-publish, it needs to prove itself with real evals against real examples — not just "it ran without crashing."

## 4. Acceptance Criteria ("What Done Looks Like")

Given a source pointed at a specific college, the bot should fulfill the following criteria:

- **Scheduled checks:** Check the source on a schedule (daily, at minimum) and find anything new since the last check — no missed posts, and it should pick up where it left off even if it's restarted.
- **Deduplication:** Never double-post the same real freebie. If a freebie is already on Merch — whether this bot already caught it, a different source caught it first, or a student already posted it themselves — recognize that and skip it instead of creating a second post for the same real-world thing. This applies across runs, across sources, and across colleges, not just "don't repeat within one run."
- **Complete metadata extraction:** Extract everything Merch needs to make a real post accurately:
  - Title / what the freebie or event actually is
  - Location — resolved to a real, correct coordinate a student could actually walk to, not a plausible-looking guess
  - An image, when one exists on the source
  - Date/time (and correctly recognize when something's already expired — don't post stale events)
  - Which college it belongs to
  - Enough evidence that it's genuinely free (not just discounted, not a paid event with one free element)
- **Confidence thresholding:** Know what it doesn't know. If any of the required fields can't be extracted with real confidence, the candidate should be held for a human to check, not published on a guess. Confidently wrong is worse than admitting uncertainty.
- **Pipeline integration:** Feed into the existing pipeline, the same way the GroupMe bot already does — new candidates land in the same review queue, get the same auto-publish treatment when everything checks out, and get the same warnings/flags when something looks off.
- **Safe state management:** Be safely re-runnable. Turning a source on/off, or re-running it, should never lose track of what's already been processed.

## 5. Reference Implementation

The GroupMe bot (`groupme-bot/`) already solves this same problem end-to-end for one source. Before writing anything, the intern should understand:

- How it decides something is a genuine freebie vs. noise
- How it turns a location into a real coordinate — a verified list of known places first, a real geocoding lookup second, an AI guess only as an absolute last resort (skipping straight to "AI guess" leads to incorrect location tags)
- How it decides what's confident enough to auto-publish vs. what needs a human to look at it first
- How candidates flow through InternHub's review screen before anything goes live

This project should extend that same pattern to new sources, not invent a new one. Two existing planning docs are also directly relevant background: `NATIONWIDE_DISCOVERY_STRATEGY.md` (which source types are realistic and legitimate across many colleges without a custom scraper per school) and `PLATFORM_EXPANSION_PLAN.md` (the broader roadmap this project is a piece of).

## 6. Legitimate Source Categories

In rough order of how reliable and structured they tend to be across institutions:

1. **Official university event calendar platforms:** Many schools run the same handful of calendar systems, so one well-built approach can often work across several colleges with light per-school setup.
2. **Student organization / campus involvement platforms:** Platforms where clubs and student orgs post their own events.
3. **Official room-scheduling and event calendars:** University-managed event and space listings.
4. **Plain calendar feeds and RSS/news feeds:** Feeds published directly by dining services, libraries, rec centers, and academic departments.
5. **Structured event data in web pages:** Machine-readable event details (title, time, location) embedded in campus web pages, accessible without needing a full API.

The intern should confirm a source is genuinely public and appropriate to use before building against it — when in doubt, ask.

## 7. Testing & Evaluation Requirements

Nothing from a new source should be trusted to auto-publish until it's been proven with real evals, not just spot-checked by eye. At minimum, before a source is considered trustworthy:

- **Real eval suites:** Build a real eval suite against actual saved examples from that source (not synthetic/made-up data) — the GroupMe bot's location-accuracy eval suite is the model to follow here.
- **Location accuracy stress-testing:** Test location accuracy hard — this is the field most likely to quietly go wrong while looking fine. A source shouldn't be trusted with real coordinates until this has been proven out with real examples, including tricky ones (informal place names, buildings not in any list, ambiguous references).
- **Edge-case validation:** Test the full range of messy real-world cases: missing images, missing/ambiguous locations, expired or already-past events, recurring events, and events getting updated or canceled after being found.
- **Duplicate handling checks:** The same real freebie should never get posted twice, whether from re-running the same source, two different sources catching the same thing, or a student having already posted it manually.
- **Accuracy proof before auto-publishing:** Demonstrate high accuracy across a sizable batch of real examples before enabling auto-publish without human review.

## 8. Milestones & Implementation Roadmap

Rather than covering all colleges and sources at once, follow a phased approach:

- **Phase 1 (Pilot College & Source):** Select a starting college (TBD) and build one source end-to-end. Process one candidate through to a reviewed Merch post.
- **Phase 2 (Eval Verification):** Prove extraction accuracy using eval suites before enabling auto-publish.
- **Phase 3 (Second Source Type):** Integrate a second, distinct source type to confirm generalizability.
- **Phase 4 (Multi-College Expansion):** Add a second college using an existing source type to validate per-college configuration design.
- **Phase 5 (Scale):** Scale across additional sources and colleges once the foundation is proven.

## 9. Non-Goals (Out of Scope)

- **Real-time / sub-minute discovery:** Daily checks satisfy requirements for now.
- **Private or authenticated sources:** Closed communities or sources requiring non-authorized logins will not be used.
- **Covering every college on day one:** Depth and accuracy on a couple of sources/colleges first, breadth later.

## 10. Success Metrics

- **Extraction Accuracy:** High fraction of reviewed candidates with correct title, location, image, and timestamp details.
- **Zero Duplicate Rate:** No duplicate postings generated for the same event across any sources or manual posts.
- **Discovery Latency:** Same-day discovery window between event posting on the source and candidate generation.
- **Human Correction Rate:** Low frequency of manual field edits required during the review phase.
- **Coverage Growth:** Number of fully verified colleges and source types integrated.

> Correctness comes before volume. A source that finds fewer, but reliably accurate, candidates is strictly better than one that finds more but needs constant correction.
