"""MCP server: screen titles/abstracts against a query or CQ with TypeSafe Jev.

One Jev request per article. Jev returns calibrated probabilities (Noul) and a
graded relevance (Score); the include / maybe / exclude rule lives in code here.
"""

import asyncio
import json
import os
import subprocess
import xml.etree.ElementTree as ET

import httpx
from mcp.server.mcpserver import MCPServer

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
KEYCHAIN_SERVICE = "typesafe-api-key"
CONCURRENCY = 8
MAX_RETRIES = 5
MAX_ABSTRACT_CHARS = 12000
EFETCH_BATCH = 200
MAX_SEARCH_RESULTS = 5000
DEFAULT_RETURN = ("include", "maybe", "error")

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
        "inclusion_criteria": inclusion,
        "exclusion_criteria": exclusion,
        "thresholds": {"include": include_threshold, "exclude": exclude_threshold},
        "counts": counts,
        "input_tokens": input_tokens,
        "results": results,
    }


def _ncbi_params() -> dict:
    key = os.environ.get("NCBI_API_KEY")
    return {"api_key": key} if key else {}


ROUTINE_REASONS = ("high match", "low match", "uncertain match")
ECHOED_INPUTS = ("pubmed_query", "research_question", "inclusion_criteria", "exclusion_criteria")


def _line(r: dict) -> str:
    """One article per line: PMID | match | title, plus the reason when it is not routine."""
    parts = [r["id"], f"{r['match']:.2f}" if "match" in r else "-", r["title"][:120]]
    if r["reason"] not in ROUTINE_REASONS:
        parts.append(r["reason"])
    return " | ".join(parts)


def _finalize(result: dict, return_decisions: list[str] | None, save_full_results_to: str | None,
              detailed: bool) -> dict:
    """Optionally save every result to a file, then return only the requested decisions."""
    if save_full_results_to:
        path = os.path.abspath(os.path.expanduser(save_full_results_to))
        with open(path, "x", encoding="utf-8") as f:  # "x": never overwrite an existing file
            json.dump(result, f, ensure_ascii=False, indent=1)
        result = {**result, "full_results_file": path}
    wanted = [d for d in ("include", "maybe", "exclude", "error") if d in set(return_decisions or DEFAULT_RETURN)]
    shown = [r for r in result["results"] if r["decision"] in wanted]
    if detailed:
        return {**result, "results": shown}
    slim = {k: v for k, v in result.items() if k not in ECHOED_INPUTS}
    slim["results_format"] = "PMID | match probability | title [| reason]"
    slim["results"] = {d: [_line(r) for r in shown if r["decision"] == d] for d in wanted}
    return slim


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
    params = {"db": "pubmed", "retmode": "xml", **_ncbi_params()}
    records = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i in range(0, len(pmids), EFETCH_BATCH):
            resp = await client.post(EFETCH_URL, data={**params, "id": ",".join(pmids[i:i + EFETCH_BATCH])})
            resp.raise_for_status()
            records.extend(_parse_pubmed_xml(resp.text))
    return records


@mcp.tool()
async def search_and_screen(
    pubmed_query: str,
    research_question: str,
    max_results: int = 500,
    inclusion_criteria: list[str] | None = None,
    exclusion_criteria: list[str] | None = None,
    include_threshold: float = 0.7,
    exclude_threshold: float = 0.3,
    return_decisions: list[str] | None = None,
    save_full_results_to: str | None = None,
    detailed: bool = False,
) -> dict:
    """Run a PubMed search and judge every hit against the user's query / clinical question (CQ).

    Search, abstract retrieval and judgement all happen server-side; only the decisions
    come back. Use a broad, sensitive query - screening is cheap (hundreds of articles
    in seconds) - and let the judgement do the narrowing.

    Args:
        pubmed_query: PubMed search expression (field tags, MeSH, boolean operators).
            Put date, language and numeric limits here, e.g. ("2021/01/01"[dp] : "3000"[dp]);
            Jev is unreliable with numbers and dates.
        research_question: The user's query or CQ, as ONE self-contained sentence in ENGLISH
            (Jev is most accurate in English and reads literally - translate Japanese input,
            spell out abbreviations, avoid negations/double negatives).
        max_results: Screen at most this many hits, taken in PubMed relevance order (max 5000).
            Check "total_hits" against "screened" in the result to see whether hits were left out.
        inclusion_criteria: Optional extra criteria, each a short positive English statement
            (e.g. "The study is a randomized controlled trial"). An unmet criterion demotes
            include to maybe; it never excludes, since abstracts often omit such details.
        exclusion_criteria: Optional; a confidently met criterion excludes the article
            (e.g. "The article is a case report").
        include_threshold: match probability at or above which an article is included.
        exclude_threshold: match probability at or below which an article is excluded.
        return_decisions: Which groups to list in "results". Default ["include", "maybe", "error"];
            "counts" always covers every article. Add "exclude" only for small batches.
        save_full_results_to: Optional file path (.json). Every result, including excluded
            articles, is written there in full detail. Fails if the file already exists.
        detailed: False (default) returns one line per article, grouped by decision:
            "PMID | match | title". True returns every probability per article (much longer).
    """
    retmax = max(1, min(max_results, MAX_SEARCH_RESULTS))
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(ESEARCH_URL, data={
            "db": "pubmed", "term": pubmed_query, "retmax": retmax, "retmode": "json",
            "sort": "relevance", **_ncbi_params()})
        resp.raise_for_status()
    found = resp.json()["esearchresult"]
    if "ERROR" in found:
        raise ValueError(f"PubMed search failed: {found['ERROR']}")
    pmids = found.get("idlist", [])
    records = await _fetch_pubmed(pmids)
    result = await _screen(research_question, records, inclusion_criteria or [], exclusion_criteria or [],
                           include_threshold, exclude_threshold)
    result = {
        "pubmed_query": pubmed_query,
        "query_translation": found.get("querytranslation", ""),
        "total_hits": int(found.get("count", 0)),
        "screened": len(records),
        **result,
    }
    return _finalize(result, return_decisions, save_full_results_to, detailed)


@mcp.tool()
async def screen_pmids(
    research_question: str,
    pmids: list[str],
    inclusion_criteria: list[str] | None = None,
    exclusion_criteria: list[str] | None = None,
    include_threshold: float = 0.7,
    exclude_threshold: float = 0.3,
    return_decisions: list[str] | None = None,
    save_full_results_to: str | None = None,
    detailed: bool = False,
) -> dict:
    """Judge whether the given PubMed articles match the user's query / clinical question (CQ).

    Use this when you already have PMIDs; use search_and_screen to search and judge in one step.
    Titles and abstracts are fetched server-side from PubMed, so abstracts stay out of the
    conversation. Each article gets include / maybe / exclude plus the underlying
    probabilities, sorted by match probability.

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
        return_decisions: Which groups to list in "results". Default ["include", "maybe", "error"];
            "counts" always covers every article. Add "exclude" only for small batches.
        save_full_results_to: Optional file path (.json). Every result, including excluded
            articles, is written there in full detail. Fails if the file already exists.
        detailed: False (default) returns one line per article, grouped by decision:
            "PMID | match | title". True returns every probability per article (much longer).
    """
    ids = [str(p).strip() for p in pmids if str(p).strip()]
    records = await _fetch_pubmed([p for p in ids if p.isdigit()])
    result = await _screen(research_question, records, inclusion_criteria or [], exclusion_criteria or [],
                           include_threshold, exclude_threshold)
    found = {r["id"] for r in records}
    result["not_found"] = [p for p in ids if p not in found]
    return _finalize(result, return_decisions, save_full_results_to, detailed)


@mcp.tool()
async def screen_records(
    research_question: str,
    records: list[dict],
    inclusion_criteria: list[str] | None = None,
    exclusion_criteria: list[str] | None = None,
    include_threshold: float = 0.7,
    exclude_threshold: float = 0.3,
    return_decisions: list[str] | None = None,
    save_full_results_to: str | None = None,
    detailed: bool = False,
) -> dict:
    """Same as screen_pmids, for articles that are not in PubMed (CiNii, arXiv, Embase exports...).

    Args:
        research_question: The user's query or CQ as one self-contained English sentence.
        records: List of {"id": str, "title": str, "abstract": str}. English text works best.
        Other arguments: see screen_pmids.
    """
    result = await _screen(research_question, records, inclusion_criteria or [], exclusion_criteria or [],
                           include_threshold, exclude_threshold)
    return _finalize(result, return_decisions, save_full_results_to, detailed)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
