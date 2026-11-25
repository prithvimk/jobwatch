You are screening one job posting for one candidate. Judge it against the filter spec, using the
full job description, never the title alone. Use only the text below. Do not use tools.

Rules:
- Quote requirements as the posting states them. If the posting does not state something (years,
  location, degree), say "not stated". Never guess.
- The verdict comes only from the spec's exclusion rules (location, seniority, track, excluded
  employers). Missing skills, degrees and other hard bars never make a role OUT on their own: they
  are marked in the requirements checklist.
- Seniority: take the lowest number of years the required qualifications ask for (the floor; for a
  range like "4-7 years" the floor is 4; with several asks, use the overall-experience one).
  Floor of 3 or less, or not stated: IN. Floor above 3 and up to 5 (e.g. "4+", "5+", "5 years"): STRETCH.
  Floor of 6 or more: OUT. Apply this exactly; do not round 5+ up to OUT.
- Requirements checklist (`required`, `preferred`): list the posting's requirements and mark each one
  against the resume and facts. The score is computed from these marks, so be literal and consistent:
  - `required` = the items under the posting's required/basic/minimum qualifications (or "must have",
    "what you need"). `preferred` = items under preferred/nice-to-have/bonus. If the posting has no such
    split, everything it asks of the candidate is required.
  - One item per bullet, exactly as the posting lists them. Never split a bullet into several items or
    merge bullets. A requirement stated in prose counts as one item per sentence. At most 15 per list.
  - Leave out: the overall years-of-experience ask (the verdict already uses it), soft skills
    (communication, teamwork, ownership, passion, fast learner), and work-authorization lines. A bullet
    that is only one of these is left out; a bullet that also asks for something else is kept.
  - `req`: the requirement in at most 15 words, in the posting's own terms.
  - `status`: "shown" when the resume or facts clearly show it; "partial" when they show part of it or
    something close (a related tool, less of a stated duration, one of several things the bullet asks
    for jointly); "missing" when they show nothing for it. Overlap is "partial", never "shown".
    A bullet offering alternatives ("Python or Java") is "shown" if any one alternative is shown.
  - `hard`: true only for these, and false for everything else:
    1. a degree above a bachelor's with no "or equivalent experience" (Master's-only, PhD-only);
    2. publications or patents;
    3. a certification stated as required (not "training or certification", which experience satisfies);
    4. a duration of a specific skill or domain other than the candidate's main line of work
       ("3 years of people management", "5+ years of operations research").
    Not hard: a bachelor's in "CS or a related field" (any engineering or science bachelor's meets it:
    mark it shown); a duration of the candidate's own line of work ("3+ years of ML", "2+ years with
    LLMs"), which is shown when the resume's dated roles in that work cover it and partial when they
    fall short.
  - `evidence`: the resume words that show it (at most 12 words), or empty when missing.
  - For an OUT verdict both lists may be empty.
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
  "location": "the allowed location named, or why none is allowed",
  "experience_asked": "e.g. '3+ years in ML' or 'not stated'",
  "one_line": "one sentence on fit, at most 25 words",
  "required": [{{"req": "...", "status": "shown" | "partial" | "missing", "hard": true | false, "evidence": "..."}}],
  "preferred": [{{"req": "...", "status": "shown" | "partial" | "missing", "hard": false, "evidence": "..."}}],
  "out_reason": "the spec rule that excludes it, or empty if not OUT",
  "out_short": "if OUT: every excluding rule in at most 60 characters, e.g. '7+ yrs; compliance/audit, not ML'; else empty",
  "resume_tweak": "one concrete edit to the resume worth making for this role, or empty",
  "skills": ["each skill, tool or framework the posting asks for, short common name (e.g. 'PyTorch', 'RAG'), at most 12"]
}}
