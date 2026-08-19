# Security

## Reporting

If you find personal data, credentials, or company-confidential material that
survived the sanitization pass in this repository, please report it privately
rather than opening a public issue — use GitHub's **Report a vulnerability**
button under the Security tab of this repository, which opens a private
advisory visible only to the maintainer.

That includes partially masked values. A mask that keeps the first and last
characters still leaks length and generation pattern — we treat those as leaks,
not as redactions.

## How this repository is produced

This is a curated export of a private system, not a mirror. The export runs
through `scripts/public_export.py` in the private repository, which:

1. keeps an explicit inclusion boundary (business-specific scrapers, store
   recommendation models, and all runtime data are excluded by name);
2. applies a version-controlled substitution map for names, hosts, and internal
   project names;
3. refuses to write anything if a final scan finds a known-real value, a private
   biographical reference, an internal endpoint, or a masked credential.

The substitution map used to live only inside the published files, which meant a
straight overwrite from the private repository would have resurrected real
names. It is now version-controlled and applied on every export.

## Known limitation

Removing a file from `HEAD` does not remove it from git history. When something
does leak, rotating the affected credential is the reliable remedy; history
rewriting is secondary.
