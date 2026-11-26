"""SuccessFactors Career Site Builder adapter against recorded page shapes (Nomura, 2026-11-26)."""
import httpx
import pytest

from detector import sfcsb

BOARD = {"name": "Nomura", "endpoint": "https://careers.nomura.com/Nomura/search/"}


def row(i, title="Sr. Software Engineer", place="Mumbai, IN"):
    jid = 1419476700 + i
    href = f"/Nomura/job/Mumbai-{title.replace(' ', '-').replace('&', '&amp;')}/{jid}/"
    return f"""<tr class="data-row">
<td class="colTitle"><span class="jobTitle hidden-phone"><a href="{href}" class="jobTitle-link">{title.replace('&', '&amp;')}</a></span>
<div class="jobdetail-phone visible-phone"><span class="jobTitle visible-phone"><a class="jobTitle-link" href="{href}">{title}</a></span>
<span class="jobLocation visible-phone">
        <span class="jobLocation">
            {place}
        </span></span></div></td>
<td class="colLocation hidden-phone">
        <span class="jobLocation">
            {place}
        </span>
</td></tr>"""


ROWS = [row(0, "AI &amp; ML Engineer".replace("&amp;", "&"))] + [row(i) for i in range(1, 230)] + [row(230, place="Gurugram, IN")]
JOB_PAGE = """<html><meta itemprop="datePosted" content="Mon Nov 09 08:00:00 UTC 2026">
<span itemprop="title">AI &amp; ML Engineer</span>
<div><span class="jobdescription"><p>Nomura Overview: a financial services group.</p>
<p>Requirements: 3+ years with <span>LLMs</span> and Python.</p></span></div>
<p class="job-location"><span class="jobmarkets"></span></p></html>"""


def client(open_ids=("1419476700",)):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path == "/Nomura/search/":
            p = req.url.params
            assert p["locationsearch"] == "India" and p["sortColumn"] == "referencedate" and p["sortDirection"] == "desc"
            s = int(p["startrow"])
            label = f'<span class="paginationLabel">Results <b>{s + 1} – {s + 100}</b> of <b>{len(ROWS)}</b></span>'
            return httpx.Response(200, html=label + "<table>" + "".join(ROWS[s:s + 100]) + "</table>")
        if "/Nomura/job/" in req.url.path:
            jid = req.url.path.rstrip("/").rsplit("/", 1)[-1]
            if jid in open_ids:
                return httpx.Response(200, html=JOB_PAGE)
            return httpx.Response(302, headers={"location": "https://careers.nomura.com/errorpage/?errortype=Exception"})
        if req.url.path == "/errorpage/":
            return httpx.Response(200, html="<title>Careers I NOMURA</title>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), calls


def test_fetch_reads_every_page_once():
    c, calls = client()
    jobs = sfcsb.fetch(c, BOARD)
    assert len(jobs) == 231 and len(calls) == 3 and len({j.id for j in jobs}) == 231
    j = jobs[0]
    assert j.id == "1419476700" and j.title == "AI & ML Engineer" and j.locations == ["Mumbai, IN"]
    assert j.url == "https://careers.nomura.com/Nomura/job/Mumbai-AI-&-ML-Engineer/1419476700/" and j.apply_url == j.url
    assert j.posted_at is None and jobs[-1].locations == ["Gurugram, IN"]


def test_enrich_states_date_and_jd():
    c, _ = client()
    j = sfcsb.fetch(c, BOARD)[0]
    sfcsb.enrich(c, j)
    assert j.posted_at == "2026-11-09"
    assert "3+ years with" in j.jd and "LLMs" in j.jd and "Nomura Overview" in j.jd
    assert sfcsb.live(c, j)[0]


def test_closed_job_redirects_to_error_page():
    c, _ = client(open_ids=())
    j = sfcsb.fetch(c, BOARD)[0]
    with pytest.raises(sfcsb.CsbError, match="errorpage"):
        sfcsb.enrich(c, j)
