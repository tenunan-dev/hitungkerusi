# delivery/ — publish helpers

## Purpose
Owns the publisher that binds an approved, sealed outputs release into the
delivery channel. Delivery records what was published; it never prunes or
rewrites history.

## Layout
- `publish_delivery.py` — publishes the exact externally bound seal without
  pruning or Git writes (sole helper today)

## Commands
None verified in V3 yet; the publish flow is exercised at P5.

## Rules
- Publish only a sealed, validated release: exact release ID plus the
  40-character sealed commit, passing hash and rollback gates.
- `site/` is a read-only consumer of delivery output; delivery never writes
  the website, deployments or cron.
- The first V3 publication is an owner-approval boundary.

## Don't
- Don't publish an incomplete or unsealed edition.
- Don't make external writes, commits, pushes or API calls from here.
- Don't delete or rewrite published delivery records.
