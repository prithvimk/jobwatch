from detector.board_map import locate, hint_locators, workday_india


def test_locate_workday_and_oracle_from_html():
    html = '''<a href="https://citi.wd5.myworkdayjobs.com/en-US/2/job/Pune/X_1/apply">Apply</a>
              <a href="https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs">WF</a>
              <a href="https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1">JPMC</a>'''
    found = locate(html)
    assert ("workday", ("citi", "wd5", "2")) in found
    assert ("workday", ("wd1", "wf", "WellsFargoJobs")) in found
    assert ("oracle", ("jpmc.fa.oraclecloud.com", "CX_1001")) in found


def test_locate_public_ats_slugs():
    html = ("https://boards.greenhouse.io/embed/job_board?for=databricks "
            "https://jobs.lever.co/acme https://jobs.ashbyhq.com/sarvam https://jobs.smartrecruiters.com/Swiggy")
    found = locate(html)
    assert ("greenhouse", ("databricks",)) in found
    assert ("lever", ("acme",)) in found
    assert ("ashby", ("sarvam",)) in found
    assert ("smartrecruiters", ("Swiggy",)) in found
    assert not any(g == ("embed",) for _, g in found)


def test_hint_locators_ignore_custom_platforms():
    hints = {"workday": "https://lseg.wd3.myworkdayjobs.com/Careers", "greenhouse": "x", "jibe": "https://h"}
    assert hint_locators(hints) == [("workday", ("lseg", "wd3", "Careers")), ("greenhouse", ("x",))]


def test_workday_india_nested_facet():
    facets = [
        {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Tech", "id": "a", "count": 9}]},
        {"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Locations", "values": [
                {"descriptor": "United States of America", "id": "us", "count": 40},
                {"descriptor": "India", "id": "c4f78be1a8f14da0ab49ce1162348a5e", "count": 211},
            ]},
        ]},
    ]
    assert workday_india(facets) == (211, {"param": "locationCountry", "id": "c4f78be1a8f14da0ab49ce1162348a5e"})


def test_workday_india_ignores_a_city_level_india_location():
    # Roche (2026-11-27): no country facet; "India" is one value of the city list, beside Hyderabad.
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "Hyderabad", "id": "h", "count": 46},
            {"descriptor": "India", "id": "54c59631019f01c479dfb787a377c235", "count": 5},
        ]},
    ]}]
    assert workday_india(facets) == ("unknown", None)


def test_workday_india_missing_is_unknown():
    assert workday_india([{"facetParameter": "x", "values": [{"descriptor": "Mumbai", "id": "m", "count": 3}]}]) == ("unknown", None)
