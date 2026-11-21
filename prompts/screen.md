You are screening one job posting for one candidate. Judge it against the filter spec, using the
full job description, never the title alone. Use only the text below. Do not use tools.

Rules:
- Quote requirements as the posting states them. If the posting does not state something (years,
  location, degree), say "not stated". Never guess.
- Seniority follows the spec's bands. Read the experience floor from the required qualifications.
- Hard bars (degree level, publications, patents, a specific tenure or duration) are named plainly as
  gaps. Skill overlap is overlap, never "meets the bar".
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
  "resume_tweak": "one concrete edit to the resume worth making for this role, or empty"
}}
