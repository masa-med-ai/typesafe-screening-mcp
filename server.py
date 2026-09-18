"""MCP server: screen titles/abstracts against a query or CQ with TypeSafe Jev.

One Jev request per article. Jev returns calibrated probabilities (Noul) and a
graded relevance (Score); the include / maybe / exclude rule lives in code here.
"""

import asyncio
import os
import subprocess
import xml.etree.ElementTree as ET

import httpx
from mcp.server.mcpserver import MCPServer

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
KEYCHAIN_SERVICE = "typesafe-api-key"
CONCURRENCY = 8
MAX_RETRIES = 5
MAX_ABSTRACT_CHARS = 12000
EFETCH_BATCH = 200

RELEVANCE_LEVELS = [
    "Different topic; does not concern the subject of the research question",
    "Related topic or background, but does not itself provide evidence answering the research question",
    "Reports data or a synthesis that directly answers the research question",
]

mcp = MCPServer("typesafe-screening")


def _api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise RuntimeError(
        "TypeSafe API key not found. Set TYPESAFE_API_KEY, or store it in the macOS keychain: "
        f"security add-generic-password -s {KEYCHAIN_SERVICE} -a $USER -w"
    )


def _build_questions(inclusion: list[str], exclusion: list[str]) -> dict:
    questions = {
        "match": {
            "type": "noul",
            "instructions": "Does this article directly address the research question?",
            "criteria": {
                "true": "The article studies the subject of the research question and reports findings relevant to answering it",
                "false": "The article is on a different subject, or only mentions the subject in passing",
            },
        },
        "relevance": {
            "type": "score",
            "instructions": "How relevant is this article to the research question?",
            "criteria": RELEVANCE_LEVELS,
        },
    }
    for i, text in enumerate(inclusion):
        questions[f"inc_{i}"] = {"type": "noul", "instructions": f"Does the article satisfy this criterion: {text}"}
    for i, text in enumerate(exclusion):
        questions[f"exc_{i}"] = {"type": "noul", "instructions": f"Does the article satisfy this criterion: {text}"}
    return questions


async def _ask_jev(client: httpx.AsyncClient, state: dict, questions: dict) -> dict:
    payload = {"model": MODEL, "state": state, "questions": questions}
    for attempt in range(MAX_RETRIES):
        last = attempt == MAX_RETRIES - 1
        try:
            resp = await client.post(TYPESAFE_URL, json=payload)
        except httpx.TransportError:
            if last:
                raise
            await asyncio.sleep(2 ** attempt)
            continue
        if (resp.status_code in (429, 529) or resp.status_code >= 500) and not last:
            await asyncio.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def _decide(match: float, inc: list[float], exc: list[float], has_abstract: bool,
            include_threshold: float, exclude_threshold: float) -> tuple[str, str]:
    """Sensitivity-first rule: only confident negatives are excluded."""
    if exc and max(exc) >= include_threshold:
        return "exclude", f"exclusion criterion #{exc.index(max(exc)) + 1} met"
    if match <= exclude_threshold:
        if not has_abstract:
            return "maybe", "low match but title only (no abstract)"
        return "exclude", "low match"
    if match >= include_threshold:
        if inc and min(inc) < 0.5:
            return "maybe", f"inclusion criterion #{inc.index(min(inc)) + 1} not evident in abstract"
        return "include", "high match"
    return "maybe", "uncertain match"


async def _screen(research_question: str, records: list[dict], inclusion: list[str], exclusion: list[str],
                  include_threshold: float, exclude_threshold: float) -> dict:
    questions = _build_questions(inclusion, exclusion)
    sem = asyncio.Semaphore(CONCURRENCY)
    input_tokens = 0

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {_api_key()}"}, timeout=60
    ) as client:

        async def one(rec: dict) -> dict:
            nonlocal input_tokens
            abstract = (rec.get("abstract") or "")[:MAX_ABSTRACT_CHARS]
            base = {"id": str(rec.get("id", "")), "title": (rec.get("title") or "")[:200]}
            state = {
                "research_question": research_question,
                "article": {"title": rec.get("title") or "", "abstract": abstract},
            }
            try:
                async with sem:
                    data = await _ask_jev(client, state, questions)
            except Exception as e:  # keep the batch alive; failed items go to human review
                return {**base, "decision": "error", "reason": f"{type(e).__name__}: {e}"[:300]}
            input_tokens += data.get("usage", {}).get("input_tokens", 0)
            ans = data["answers"]
            match = ans["match"]["noul"]
            inc = [ans[f"inc_{i}"]["noul"] for i in range(len(inclusion))]
            exc = [ans[f"exc_{i}"]["noul"] for i in range(len(exclusion))]
            decision, reason = _decide(match, inc, exc, bool(abstract), include_threshold, exclude_threshold)
            out = {
                **base,
                "decision": decision,
                "reason": reason,
                "match": round(match, 3),
                "relevance": round(ans["relevance"]["score"], 2),
                "relevance_confidence": round(ans["relevance"]["confidence"], 2),
            }
            if inc:
                out["inclusion"] = [round(x, 2) for x in inc]
            if exc:
                out["exclusion"] = [round(x, 2) for x in exc]
            return out

        results = await asyncio.gather(*(one(r) for r in records))

    results.sort(key=lambda r: r.get("match", -1), reverse=True)
    counts = {d: sum(r["decision"] == d for r in results) for d in ("include", "maybe", "exclude", "error")}
    return {
        "model": MODEL,
        "research_question": research_question,
        "thresholds": {"include": include_threshold, "exclude": exclude_threshold},
        "counts": counts,
        "input_tokens": input_tokens,
        "results": results,
    }


def _parse_pubmed_xml(xml_text: str) -> list[dict]:
    records = []
    for art in ET.fromstring(xml_text).iter("PubmedArticle"):
        pmid = art.findtext("./MedlineCitation/PMID", default="")
        title_el = art.find("./MedlineCitation/Article/ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        parts = []
        for ab in art.iterfind("./MedlineCitation/Article/Abstract/AbstractText"):
            text = "".join(ab.itertext()).strip()
            label = ab.get("Label")
            parts.append(f"{label}: {text}" if label else text)
        records.append({"id": pmid, "title": title, "abstract": "\n".join(parts)})
    return records


async def _fetch_pubmed(pmids: list[str]) -> list[dict]:
    params = {"db": "pubmed", "retmode": "xml"}
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    records = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(pmids), EFETCH_BATCH):
            resp = await client.post(EFETCH_URL, data={**params, "id": ",".join(pmids[i:i + EFETCH_BATCH])})
            resp.raise_for_status()
            records.extend(_parse_pubmed_xml(resp.text))
    return records


@mcp.tool()
async def screen_pmids(
    research_question: str,
    pmids: list[str],
    inclusion_criteria: list[str] | None = None,
    exclusion_criteria: list[str] | None = None,
    include_threshold: float = 0.7,
    exclude_threshold: float = 0.3,
) -> dict:
    """Judge whether PubMed articles match the user's query / clinical question (CQ).

    Titles and abstracts are fetched server-side from PubMed, so pass only PMIDs
    (e.g. from a PubMed search tool) and keep abstracts out of the conversation.
    Each article is evaluated by TypeSafe Jev and gets include / maybe / exclude
    plus the underlying probabilities. Results are sorted by match probability.

    Args:
        research_question: The user's query or CQ, as ONE self-contained sentence in ENGLISH
            (Jev is most accurate in English and reads literally - translate Japanese input,
            spell out abbreviations, avoid negations/double negatives).
        pmids: PubMed IDs to screen.
        inclusion_criteria: Optional extra criteria, each a short positive English statement
            (e.g. "The study is a randomized controlled trial"). An unmet criterion demotes
            include to maybe; it never excludes, since abstracts often omit such details.
        exclusion_criteria: Optional; a confidently met criterion excludes the article
            (e.g. "The article is a case report").
        include_threshold: match probability at or above which an article is included.
        exclude_threshold: match probability at or below which an article is excluded.
    """
    ids = [str(p).strip() for p in pmids if str(p).strip()]
    records = await _fetch_pubmed([p for p in ids if p.isdigit()])
    result = await _screen(research_question, records, inclusion_criteria or [], exclusion_criteria or [],
                           include_threshold, exclude_threshold)
    found = {r["id"] for r in records}
    result["not_found"] = [p for p in ids if p not in found]
    return result


@mcp.tool()
async def screen_records(
    research_question: str,
    records: list[dict],
    inclusion_criteria: list[str] | None = None,
    exclusion_criteria: list[str] | None = None,
    include_threshold: float = 0.7,
    exclude_threshold: float = 0.3,
) -> dict:
    """Same as screen_pmids, for articles that are not in PubMed (CiNii, arXiv, Embase exports...).

    Args:
        research_question: The user's query or CQ as one self-contained English sentence.
        records: List of {"id": str, "title": str, "abstract": str}. English text works best.
        inclusion_criteria / exclusion_criteria / thresholds: see screen_pmids.
    """
    return await _screen(research_question, records, inclusion_criteria or [], exclusion_criteria or [],
                         include_threshold, exclude_threshold)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
