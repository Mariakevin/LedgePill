# Publishing LedgePill on Google Play

What it actually takes, reviewed 2026-11-12.

## Progress

Done:
- [x] targetSdk 36
- [x] compileSdk 37
- [x] AAB build in CI (`LedgePill-android-bundle` artifact on tags)
- [x] manifest exported flags correct
- [x] allowBackup false
- [x] signing keystore provisioned
- [x] privacy policy page (https://mariakevin.github.io/LedgePill/privacy.html)
- [x] Play Protect install workaround documented

Pending (user action):
- [ ] Play Console account + $25
- [ ] D-U-N-S (if org)
- [ ] closed test with ≥12 testers × 14 days (personal account)
- [ ] data safety form
- [ ] content rating questionnaire
- [ ] upload AAB
- [ ] Play Protect review request

## Account

- **$25 one-time** Google Play developer registration.
- A **personal** account (created after Nov 2023) cannot publish straight to
  production: you must first run a **closed test with ≥ 12 testers opted in
  continuously for 14 days**, then apply for production access from the
  Console dashboard. "Opted in" = accepted the invite *and* installed the
  app with that Google account — invites alone don't count.
- An **organization** account (needs a D-U-N-S number) skips the testing
  gate entirely.

## Technical requirements

- **AAB, not APK** — new apps upload an Android App Bundle
  (`gradle bundleRelease`), and **Play App Signing is mandatory**: Google
  holds the release key, you keep an upload key. Keep all signing keys outside
  the repository and configure Play builds with a dedicated upload key.
- **Target API level**: since **Aug 31 2026, new apps and updates must target
  API 36 (Android 16)**; existing apps need API 35 to stay visible. See the
  [current Google Play target API requirements](https://developer.android.com/google/play/requirements/target-sdk).
  This project now compiles against API 37 while still targeting API 36, so
  the Play target-API requirement is met; a Play build still needs a
  re-test of overlay and foreground-service behavior (both change across
  Android releases).
- **Sensitive access must be justified in review**:
  - `SYSTEM_ALERT_WINDOW` (the overlay) — explain it *is* the product.
  - Notification access (`BIND_NOTIFICATION_LISTENER_SERVICE`) — declare
    what is read (media sessions, notification titles for the island) and
    that nothing leaves the device.
- **Privacy policy URL** is required because of the notification access —
  a one-page "everything stays on device, no data collected, lyrics
  fetched anonymously from lrclib.net" hosted anywhere public (the GitHub
  Pages site works).
- **Data safety form**: declare "no data collected, no data shared";
  mention the lrclib.net lookups (song title/artist sent, nothing personal).
- **Content rating questionnaire**: trivial (utility, no UGC).

## Store listing assets

- App icon 512×512 PNG (render from `data/ledge-pill.svg`)
- Feature graphic 1024×500
- ≥ 2 phone screenshots (island over home screen, expanded card, settings)
- Short (80 chars) + full (4000 chars) description — reuse the README copy.

## Realistic path from here

1. Create the developer account, pay the $25.
2. `gradle bundleRelease` with `targetSdk = 36`, upload to a **closed
   testing** track.
3. Recruit 12+ testers (friends, or the r/AndroidClosedTesting /
   tester-exchange communities exist exactly for this), keep them opted in
   14 days, keep shipping small updates meanwhile.
4. Apply for production access, answer the readiness questions, submit.

Total cost: $25 and roughly two-three weeks of waiting, most of it the
tester clock.
