# Employer Identity Resolution

`app/sponsorship/identity.py`. Evidence rows arrive with a raw employer name
(and sometimes domain/city/state) that must resolve to a `registry_companies`
row before it can contribute to that company's sponsorship profile. Never
merges on name similarity alone (CLAUDE.md Phase 7 sections 8, 36, 44).

## Resolution order (first match wins, most specific first)

1. **Domain match** -- normalized name + normalized domain exact match
   against `registry_companies`. If the domain alone matches a different
   name, that's still accepted (a company legitimately renamed, name text
   differs but domain is authoritative) and reported as `matched_via="domain"`.
2. **Verified alias match** -- exact normalized-alias lookup in
   `company_aliases`, but **only if exactly one company claims that alias**
   (`find_company_id_by_alias`). An alias claimed by more than one company
   (a collision) never resolves here -- see "Doctor checks" below.
3. **Name-only match** -- if no domain/alias match, and **exactly one**
   registry company shares the normalized name, resolve to it
   (`matched_via="name_only"`).
4. **Anything else is unresolved.** Two sub-cases:
   - **Ambiguous**: more than one registry company shares the normalized
     name (different domains) -- a row is written to
     `employer_identity_review` (`status=PENDING`) with every candidate
     company id, and `company_id` stays null on the evidence row.
   - **None**: no registry company matches at all -- `company_id` stays
     null, no review needed (nothing to disambiguate).

"Acme Corp" and "Acme Corp of Texas" never merge just because the names look
similar -- only an exact normalized-name, domain, or verified-alias match
resolves an identity (`tests/test_sponsorship_identity.py::
test_similar_unrelated_company_names_are_not_merged`).

## Aliases (`company_aliases`)

`alias_type` is one of `LEGAL_NAME` / `DBA` / `BRAND_NAME` / `FORMER_NAME` /
`SUBSIDIARY_NAME`. Every alias is `verified=False` by default -- only a
`verified=True` alias is used for automatic resolution
(`find_company_id_by_alias` and the doctor's collision check both filter on
it). This lets an operator record an unverified candidate alias (e.g. from a
bulk import guess) without it silently taking effect.

## Parent / subsidiary / affiliate / acquired (`company_relationships`)

Stored for display and doctor contradiction-checking. **Never used to
transfer sponsorship evidence between the two companies** -- a parent's
history is never attributed to a subsidiary's profile or vice versa
(`app.sponsorship.profile` always aggregates strictly by `company_id`). An
acquired company keeps its own distinct identity and its own distinct
evidence trail; the relationship is metadata, not a merge.

## Manual identity review (`employer_identity_review`)

Dashboard: `/sponsorship/identity-review`. An operator resolves a pending
item to one of the listed candidate companies, or rejects it (no match) --
`resolve_review(review_id, company_id_or_None, note)`. Resolution is
recorded (`status`, `resolved_company_id`, `resolution_note`,
`resolved_at`) but does **not** retroactively re-attach the evidence rows
that triggered the review automatically -- an operator (or a future
re-import) reruns resolution once the review is settled, keeping the two
steps auditable and separate.

## Doctor checks

`app/sponsorship/doctor.py` catches identity-layer problems specifically:

- `verified_alias_collision` -- the same normalized alias verified for more
  than one company (should never happen; if it does, one of the two
  verifications was wrong and needs correcting).
- `parent_subsidiary_contradiction` -- company A recorded as company B's
  PARENT *and* company B recorded as company A's PARENT simultaneously.
- `pending_identity_review_backlog` (warning, not serious) -- how many
  ambiguous matches are still awaiting a human decision.
