# Known issues and follow-up work

These observations were recorded from the live test on 2026-07-19.

## The Economist (EU) is unavailable to the VPS worker

- The 2026-07-18 issue can be read in the user's local Firefox session and is
  present in that session's **My Publications**.
- The VPS browser session does not list the publication. Opening the same issue
  URL there redirects to the PressReader catalog.
- Transferring the normal PressReader cookies and local-storage authentication
  ticket to an isolated VPS browser did not reproduce the local entitlement.
- No local-browser automation is currently part of this project. A practical
  short-term fallback is to export the Nook EPUB in the authorised local
  browser, run it through `automation/epub_cleaner.py`, and place it in the
  bridge library.

Before resuming automation, determine whether this title's access depends on a
local HotSpot/library entitlement, browser state not represented by cookies, or
the client network. Do not assume that copying authentication data alone will
make it available on the VPS.

## Foreign Affairs contains duplicate articles

The exported Foreign Affairs EPUB still shows duplicated articles after the
current cleaner runs. Add the affected source EPUB as a private test fixture or
construct a minimal equivalent fixture, identify how its duplicate entries
differ in markup or identifiers, and extend the cleaner's content fingerprint
and navigation pruning. The fix should retain legitimately distinct articles
with similar headlines and should include a regression test.
