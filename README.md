# typesafe-screening-mcp

An MCP server that judges whether articles match a user's query or clinical question (CQ) from their **title and abstract**, using [TypeSafe](https://docs.typesafe.ai/introduction) **Jev** — a "System One" model that returns calibrated probabilities instead of generated text.

Give it a PubMed query (or PMIDs you already have); it runs the search, fetches the abstracts itself, asks Jev, and returns `include` / `maybe` / `exclude` with the underlying probabilities. No separate PubMed tool is required. Abstracts never enter the LLM conversation, so hundreds of records can be screened in seconds for a fraction of a cent.

> 日本語の説明は[下](#日本語)にあります。

## Tools

| Tool | Input | Use |
|---|---|---|
| `search_and_screen` | PubMed query + research question | Search PubMed and judge every hit in one step (up to 5000 hits, relevance order) |
| `screen_pmids` | research question + PMIDs | PubMed articles you already have PMIDs for (title/abstract fetched server-side via E-utilities) |
| `screen_records` | research question + `[{id, title, abstract}]` | Anything outside PubMed (CiNii, arXiv, database exports) |

Optional arguments for all tools:

- `inclusion_criteria`, `exclusion_criteria` — lists of short English statements
- `include_threshold` (default 0.7), `exclude_threshold` (default 0.3)
- `return_decisions` — which groups to list in `results`; default `["include", "maybe", "error"]`. `counts` always covers every article, so a 300-hit search does not flood the conversation with excluded records.
- `detailed` — `false` (default) returns one line per article, grouped by decision; `true` returns every probability per article (much longer).
- `save_full_results_to` — path of a JSON file that receives every result, including excluded articles in full detail (for PRISMA-style records or a spreadsheet). An existing file is never overwritten.

### How an article is judged

One Jev request per article, several typed questions at once:

- `match` (Noul) — probability that the article directly addresses the research question
- `relevance` (Score, 0–2) — off-topic / related background / directly answers, with confidence
- one Noul per inclusion / exclusion criterion

The decision rule is plain code (`_decide` in `server.py`), tuned for sensitivity:

1. an exclusion criterion ≥ include threshold → **exclude**
2. `match` ≤ exclude threshold → **exclude** (or **maybe** when the record has no abstract)
3. `match` ≥ include threshold → **include**, demoted to **maybe** if any inclusion criterion < 0.5 (abstracts often omit such details, so an unmet inclusion criterion never excludes)
4. otherwise → **maybe**

Results are sorted by `match`. The detailed output and the saved file keep every probability, so you can re-threshold later.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and a TypeSafe API key ([console](https://console.typesafe.ai)).

```sh
git clone https://github.com/masa-med-ai/typesafe-screening-mcp.git
cd typesafe-screening-mcp
uv sync
```

Provide the key in one of two ways (never write it into a config file in plain text):

```sh
# macOS keychain (prompts for the key)
security add-generic-password -s typesafe-api-key -a "$USER" -w

# or an environment variable
export TYPESAFE_API_KEY=...
```

Optional environment variables: `NCBI_API_KEY` (higher E-utilities rate limit), `TYPESAFE_MODEL` (default `jev-latest`).

### Claude Code

```sh
claude mcp add --scope user typesafe-screening -- \
  uv run --project /path/to/typesafe-screening-mcp python /path/to/typesafe-screening-mcp/server.py
```

### Claude Desktop / other MCP clients

```json
{
  "mcpServers": {
    "typesafe-screening": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/typesafe-screening-mcp", "python", "/path/to/typesafe-screening-mcp/server.py"]
    }
  }
}
```

## Usage

Ask your assistant something like:

> With typesafe-screening, search PubMed for prospective studies of CADx in colonoscopy from the last 5 years and judge them against the question "How well does CADx characterize colorectal polyps during colonoscopy?" Save the full results to ~/cadx_screening.json.

The assistant writes the PubMed query and the English research question; the server does the rest. `search_and_screen` also returns `total_hits`, `screened` and PubMed's `query_translation`, so you can see whether `max_results` cut anything off.

Default (slim) result — one line per article, `PMID | match probability | title`, with the reason appended when a criterion or a missing abstract drove the decision:

```json
{
  "total_hits": 15,
  "screened": 15,
  "counts": {"include": 8, "maybe": 5, "exclude": 2, "error": 0},
  "results": {
    "include": [
      "41563136 | 0.98 | Real-Time Characterization of Colonic Polyps: A Multicenter Prospective Study Evaluating the CAD-EYE System in Screening"
    ],
    "maybe": [
      "40375757 | 0.96 | Computer-aided diagnosis for colorectal polyp in comparison with endoscopists: Systematic review and meta-analysis. | inclusion criterion #1 not evident in abstract"
    ],
    "error": []
  }
}
```

With `detailed: true` (and always in the saved file), each article carries every probability:

```json
{
  "id": "32371116",
  "title": "Efficacy of Real-Time Computer-Aided Detection of Colorectal Neoplasia in a Randomized Trial.",
  "decision": "include",
  "reason": "high match",
  "match": 0.99,
  "relevance": 2.0,
  "relevance_confidence": 1.0,
  "inclusion": [0.99]
}
```

In one real run, 326 PubMed hits were screened in about 17 seconds using ~330k input tokens (about US$0.014 at the time of writing).

## Writing good questions and criteria

Jev reads literally, so the wording decides the result.

- Write the research question as **one self-contained English sentence**; spell out abbreviations. Non-English input works less well.
- Phrase criteria as **positive statements** ("The study is a randomized controlled trial"). Avoid negations, "only", and double negatives.
- A narrow question gives narrow matches: "How well does CADx characterize polyps?" will score safety or cost-effectiveness papers on CADx lower. Broaden the wording if you want them.
- Do numeric and date limits (sample size, publication year) in the PubMed query, not in criteria — Jev is unreliable with numbers and dates.

## Limitations

- **A screening aid, not a replacement for human review.** The default thresholds are not calibrated on labelled data. For a systematic review, validate sensitivity against a known set of included studies, and have humans review at least the `maybe` group and a sample of `exclude`.
- Judgement uses title and abstract only. Records without an abstract are never auto-excluded.
- Titles/abstracts are sent to the TypeSafe API. **Do not send patient data or other confidential text.**
- Abstract text is untrusted input; Jev does not defend against instructions embedded in it.
- PubMed only for the built-in search; other databases go through `screen_records`.

This project is not affiliated with TypeSafe or NCBI. When using E-utilities, follow the [NCBI usage guidelines](https://www.ncbi.nlm.nih.gov/books/NBK25497/).

## 日本語

文献検索のとき、ユーザーの検索意図や CQ に合う文献かどうかを、**タイトルと抄録**から TypeSafe の **Jev** で判定する MCP サーバーです。

- `search_and_screen`: PubMed の検索式と CQ を渡すと、検索・抄録取得・判定までサーバー側で一括実行します（別途 PubMed 用のツールは不要）。抄録が LLM の会話に乗らないため、数百件でも数十秒・数円未満で処理できます。
- `screen_pmids`: 手元に PMID がある場合はこちら。
- `screen_records`: PubMed 以外（CiNii、arXiv など）の `{id, title, abstract}` を直接渡します。
- 各文献に `include` / `maybe` / `exclude` と、根拠となる確率（CQ への一致、関連度、採択・除外基準ごとの確率）を返します。判定ルールはコードで固定されており、感度優先です（採択基準を満たさないだけでは除外せず `maybe` にします）。
- 既定では `include` / `maybe` / `error` だけを、1 文献 1 行（`PMID | match | タイトル`）で返します（件数は全件分）。全確率が必要なら `detailed: true`。`return_decisions` で変更でき、`save_full_results_to` に JSON のパスを渡すと除外分を含む全結果をファイルに保存します。
- API キーは環境変数 `TYPESAFE_API_KEY` か macOS キーチェーン（サービス名 `typesafe-api-key`）から読みます。
- CQ と基準は**英語の肯定文**で渡してください（日本語で依頼すれば、呼び出し側の LLM が英訳して渡します）。数値・年の条件は PubMed の検索式側で絞るのが確実です。
- 閾値は実データで較正していません。系統的レビューで使う場合は、既知の採択文献で感度を確認し、`maybe` と `exclude` の一部は人が確認してください。患者情報などの機密テキストは送らないでください。

## License

MIT
