# Known issues and follow-up work

These observations were recorded from the live test on 2026-07-19.

## The Economist (EU) is unavailable to the VPS worker

- The 2026-07-18 issue can be read in the user's local Firefox session and is
  present in that session's **My Publications**.
- The VPS browser session does not list the publication. Opening the same issue
  URL there redirects to the PressReader catalog.
- Transferring the normal PressReader cookies and local-storage authentication
  ticket to an isolated VPS browser did not reproduce the local entitlement.
- The VPS session does have an active Biblioteca Lúcio Craveiro da Silva
  HotSpot sponsorship. PressReader reports that the subscription does not
  require a maximum-session limit, so another open app/browser session is not
  the likely cause.
- The identical VPS Chromium profile was tested through two Portuguese
  residential MEO connections. The Economist worked through the Braga egress
  but still redirected to the catalog through the Trancoso/Guarda egress.
  This rules out a simple Germany-versus-Portugal, datacenter-versus-residential,
  or ISP-level explanation.
- No local-browser automation is currently part of this project. A practical
  short-term fallback is to export the Nook EPUB in the authorised local
  browser, run it through `automation/epub_cleaner.py`, and place it in the
  bridge library.

For now, describe the observed requirement narrowly as the specific Braga
egress/context. Do not generalize it to Portugal or residential IP addresses.
Capture and compare the PressReader country, sponsorship, subscription, and
catalog-route API responses through both Portuguese routes before attributing
the difference to IP location, BLCS state, or another part of the request
context. Do not assume that copying authentication data alone will make it
available on the VPS.

## Foreign Affairs continuation articles

PressReader represents some long articles as separate body records on nearby
print pages and repeats the headline for each record. These are continuations,
not duplicate bodies, so removing them would truncate the article. The cleaner
keeps every body record in the reading spine but collapses adjacent repeated
headlines in NCX and HTML navigation. A regression test covers this layout.
