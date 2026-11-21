You are screening one job posting for one candidate. Judge it against the filter spec, using the
full job description, never the title alone. Use only the text below. Do not use tools.

Rules:
- Quote requirements as the posting states them. If the posting does not state something (years,
  location, degree), say "not stated". Never guess.
- The verdict comes only from the spec's exclusion rules (location, seniority, track, excluded
  employers). Missing skills, degrees and other hard bars never make a role OUT on their own: they
  lower `fit` and are listed in `gaps`.
- Seniority: take the lowest number of years the required qualifications ask for (the floor; for a
  range like "4-7 years" the floor is 4; with several asks, use the overall-experience one).
  Floor of 3 or less, or not stated: IN. Floor above 3 and up to 5 (e.g. "4+", "5+", "5 years"): STRETCH.
  Floor of 6 or more: OUT. Apply this exactly; do not round 5+ up to OUT.
- Hard bars (degree level, publications, patents, a specific tenure or duration) are named plainly as
  gaps, first in the list. "Or equivalent experience" makes a degree bar soft; say so.
  Skill overlap is overlap, never "meets the bar".
- Location: name the allowed location from the posting's listed locations, or say why none is allowed.
- The candidate's facts list states their years of experience and where they will relocate.

## Filter spec

{filter_spec}

## Candidate resume

{resume}

## Candidate facts

{facts}

## Posting

Company: {company}
Title: {title}
Listed locations: {locations}
Description source: {jd_source}

{jd}

## Output

Reply with exactly one JSON object and nothing else:

{{
  "verdict": "IN" | "STRETCH" | "OUT",
  "fit": 1-5 (5 = strong match on required qualifications; OUT roles get 1),
  "location": "the allowed location named, or why none is allowed",
  "experience_asked": "e.g. '3+ years in ML' or 'not stated'",
  "one_line": "one sentence on fit, at most 25 words",
  "gaps": ["each required qualification the resume does not show; hard bars first"],
  "out_reason": "the spec rule that excludes it, or empty if not OUT",
  "resume_tweak": "one concrete edit to the resume worth making for this role, or empty",
  "skills": ["each skill, tool or framework the posting asks for, short common name (e.g. 'PyTorch', 'RAG'), at most 12"]
}}
