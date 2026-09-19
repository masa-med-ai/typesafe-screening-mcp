---
name: literature-screening
description: "PubMed文献を検索し、ユーザーのクエリやCQ（クリニカルクエスチョン）に合う文献かをタイトル・抄録から TypeSafe Jev で一括判定するスキル（typesafe-screening MCP を使用）。「このCQに合う文献を探して」「文献をスクリーニングして」「一次スクリーニング」「タイトル・抄録スクリーニング」「検索結果から関係ある論文だけ選んで」「include/excludeを判定して」「スクリーニング結果をExcelに」等、数十〜数千件の文献をCQに照らしてふるい分ける要求で必ず使う。1〜2件の論文を読むだけなら不要。"
---

# 文献スクリーニング（typesafe-screening MCP）

検索・抄録取得・判定は MCP サーバー側で完結する。このスキルの役割は、**Jev に渡す言葉を正しく作ること**と、**結果を取りこぼしなく扱うこと**。

## 前提

`mcp__typesafe-screening__search_and_screen` が使えること（遅延ロードなら ToolSearch で読み込む）。見つからなければ、MCP が未登録か API キー未設定。リポジトリの README のセットアップ手順を案内して止まる。

## 手順

### 1. CQ を Jev 用の 1 文にする

Jev は英語で最も正確で、**書かれた文字どおり**に読む。

- ユーザーの CQ を、**それだけで意味が通る英語の 1 文**にする。略語は初出で展開する（例: computer-aided diagnosis (CADx)）。
- 否定・二重否定・"only" "without" を避ける。「Detection ではなく鑑別」は、"not detection" ではなく "characterize ... such as predicting histology" と肯定形で書く。
- CQ の幅がそのまま採択の幅になる。"How well does X perform" と書くと、X の安全性・費用対効果・実装研究は match が下がる。それらも欲しければ "evaluate the use of X" のように広く書く。
- 作った英文はユーザーに見せる（結果の解釈に必要）。

### 2. 検索式は広めに作る

判定は安い（300 件で約 20 秒・数円未満）ので、感度優先で広く検索し、絞り込みは Jev に任せる。

- **年・言語・数値の条件は検索式に入れる**（Jev は数値と日付が苦手）。例: `("2021/01/01"[dp] : "3000"[dp])`
- 研究デザインの絞り込み（prospective、RCT 等）を検索式に入れると、抄録に明記のない研究を落とす。入れるなら同義語を OR で広げる。
- 実行後、`total_hits` と `screened` を比べる。差があれば `max_results` を上げる（上限 5000）か、検索式を見直す。`query_translation` で PubMed の解釈も確認する。

### 3. 基準の置き方

| 種類 | 効き方 | 向いているもの |
|---|---|---|
| `exclusion_criteria` | 0.7 以上で**即 exclude** | 抄録から確実に分かるもの: レビュー、メタ解析、エディトリアル、プロトコル、症例報告 |
| `inclusion_criteria` | 満たさないと include → **maybe に格下げ**（除外はしない） | 抄録に書かれないことがあるもの: 前向き、RCT、対象集団 |

- どちらも**短い英語の肯定文**で書く（"The study is a randomized controlled trial"）。
- 迷ったら inclusion に置く。exclusion に置いた基準の誤判定は、そのまま取りこぼしになる。
- 基準は 1 つに 1 条件。複数条件を 1 文に詰めない。

### 4. 実行

`search_and_screen` を呼ぶ。**`save_full_results_to` は常に指定する**（除外分を含む全結果が残る。PRISMA の記録と Excel 化に必要）。

- 保存先はユーザー指定の場所。指定がなければ聞くか、一時領域に置いてその旨を伝える。ファイル名は `YYYYMMDD_<topic>_screening.json`。既存ファイルは上書きされずエラーになるので、別名にする。
- PMID が手元にあるなら `screen_pmids`、PubMed 外の文献（CiNii、arXiv 等）は `screen_records`。
- `detailed` は既定（false）のままでよい。全確率は保存ファイルにある。

### 5. 結果を報告する

- 件数（include / maybe / exclude / error）、使った検索式と英語 CQ、基準を先に示す。
- **include**: タイトルから明らかに CQ と違うものがあれば「要確認」として挙げる。
- **maybe**: 理由で 2 群に分けて示す。
  - 「inclusion criterion not evident」= トピックは合うが基準が抄録から読み取れない
  - match が中間（0.3〜0.7）= CQ との関係が部分的。ここに拾うべき文献が混じりやすい
- **exclude は中身を見ていない**ことを明記する。取りこぼし確認は、Excel の Exclude シートを Match 降順に並べ、上位（トピックは合うが除外基準で落ちたもの）から見るのが効率的だと伝える。
- error があれば再実行を提案する。

### 6. Excel にする（求められたとき）

```sh
uv run <このスキルのディレクトリ>/scripts/results_to_xlsx.py <full_results.json> <output.xlsx>
```

Summary / Include / Maybe / Exclude のシートに、著者・年・雑誌・Publication Type・PubMed リンク・各確率と、手入力用の Human decision / Notes 列が入る。既存ファイルは上書きしない。Publication Type 列は、Jev の「レビュー」判定と PubMed の公式タグの突き合わせに使える。

## 必ず伝える限界

- 閾値（0.7 / 0.3）は実データで較正していない。系統的レビューに使うなら、既知の採択文献セットで感度を確認し、maybe 全件と exclude の一部は人が確認する。
- 判定はタイトルと抄録のみ。抄録のない文献は自動除外されず maybe になる。
- タイトル・抄録は TypeSafe の API に送信される。患者情報や未公開原稿などの機密テキストを `screen_records` に渡さない。
