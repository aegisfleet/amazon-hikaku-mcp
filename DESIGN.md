# amazon-hikaku リモートMCP 設計案

- 作成日: 2026-11-22（v1.1 改訂: 同日、第三者レビューを実データ・現行コードで検証し反映）
- ステータス: 設計案（未実装。実装着手には別途承認が必要）
- 改訂履歴:
  - v1.1 (2026-11-22): カテゴリ正本の訂正（taxonomy-v2 と生成物の公開取得）、index.json の brand 欠落と結合方針、score_rationale の MCP専用JSON化、プロトコル世代混在への検証項目、ツールアノテーション、検索正規化、キャッシュのシングルフライト化、ドメインマッピング遅延の代替案、§13 へのレビュー推奨の反映
- 公開エンドポイント（予定）: `https://mcp.amazon-hikaku.com/mcp`
- 目的: llms-full.txt で狙った「AIアシスタントが amazon-hikaku.com の調査データを参照し、出典として引用する」役割を、巨大な静的ファイルの丸読みから、必要な分だけ返すMCPツール群に置き換える。

---

## 1. 背景と置き換えの対応

llms-full.txt の現行方式は、AIに products-summary.json や index.json を丸ごと取得させる前提になっている。実測（2026-11-22）ではファイルサイズが llms-full.txt の記載より育っている。

| データ | llms-full.txt の記載 | 実測 (2026-11-22) | 備考 |
|---|---|---|---|
| products-summary.json | 約1.7MB | **約4.0MB / 7,467件** | 軽量サマリー（9フィールド） |
| index.json（ルート） | 約9.3MB | **約13.4MB / 7,467件** | 全商品の詳細（スペック・要約・在庫・調査日つき） |
| deals/index.json | （記載なし） | 約1.1MB / 671件 | 全件在庫あり、平均32.9%引き |
| bargain/index.json | 参照先として案内 | 約12.2MB / 7,467件 | 全商品入り。2,000円以下は2,382件、3,500円以下は4,030件 |

この方式の問題:

- AI側の取得タイムアウト・コンテキスト浪費（数MBのJSONを毎回読む）
- 「カテゴリで絞る」「価格上限で探す」といった絞り込みがAI側の力作業になる
- データ鮮度や出典URLの付け方をサーバー側で統制できない

MCP化すると、サーバー側がデータを保持・検索し、条件に合う数件だけを出典URLつきで返せる。llms-full.txt に書かれた参照ガイド（スコア80点以上を優先、詳細レポートURLを出典として案内、等）は、ツールの説明文とサーバーの instructions に移植する。

## 2. データ源の実測結果

### 2.1 採用するデータ源

| ソース | 用途 | 主なフィールド |
|---|---|---|
| `https://www.amazon-hikaku.com/index.json` | 商品検索・詳細・比較の主データ | asin / title / categories / summary / specs（テキスト・JSON両形式）/ price・price_value / score / image / availability・in_stock / last_investigated / parent_asin / savings_percentage / affiliate_url / permalink |
| `https://www.amazon-hikaku.com/products-summary.json` | 起動時フォールバック・軽量検索 | asin / title / brand / category / price / score / description / url / image |
| `https://www.amazon-hikaku.com/deals/index.json` | セール検索専用 | 上記＋ savingsPercentage / dealBadge / priceRaw / lastInvestigated（フィールド名が camelCase で他と異なる点に注意） |
| `https://www.amazon-hikaku.com/data/categorygroups.json` | 親→子カテゴリ階層 | 27親カテゴリ × { slug, categories[] }（約18KB、実測200で取得可能） |
| `https://www.amazon-hikaku.com/data/brandgroups.json` | ブランド表記揺れの正規化 | 315グループ × { slug, matcher（正規表現） }（約62KB、実測200で取得可能） |

- カテゴリは子カテゴリ **165種**、ブランドは **3,024種**（summary から実測集計）。カテゴリ／ブランドの一覧は主データからサーバー側で集計して返す（`/categories/index.json` は404）。
- **【v1.1 訂正】カテゴリの正本は `data/taxonomy-v2.json`**（2026-11-12 のカテゴリ再設計で導入。正本自体は非公開で `/data/taxonomy-v2.json` は404）。`src/scripts/enhance-categories.ts` が `pnpm run prebuild:hugo` 時に categorygroups / brandgroups を `static/data/` へ自動生成して公開しているため、MCP側は**手動同梱ではなく公開JSONをフェッチ**する。これで taxonomy 更新に自動追随できる。
- **【v1.1 追加】`index.json` には `brand` フィールドが無い**（全7,467件で欠落を実測確認。`layouts/index.json` が出力していない）。ブランド絞り込み・一覧は、v1 では `products-summary.json` と ASIN で結合して補う。恒久対応は §2.2 の MCP専用JSONに brand を含めること。

### 2.2 データの制約と拡張提案

- **スコア根拠（長所・注意点）は公開JSONに含まれていない。** 記事の front matter には `hero.score_rationale`（plus/minus）があり詳細ページでは表示されるが、`layouts/index.json` の出力 dict には含まれていない。
  - v1 では get_product は要約＋スペック＋スコアまで返し、根拠は詳細ページURLへ誘導する。
  - **【v1.1 変更】拡張は index.json への追加ではなく、MCP専用JSONの新設で行う。** 全7,467件分の rationale を既存の index.json（13.4MB、フロントエンド検索も使用）へ足すと肥大化と表示側への影響があるため、`prebuild:hugo` の生成ステップ（既存の generate-products-summary.ts と同列）に MCP専用の軽量JSON（例: `static/mcp-products.json`、specs_html を省き brand と score_rationale を含む）を追加し、MCPはそちらを主データにする。サイト側変更として実装フェーズで別途承認を得る。
- 価格は調査時点の参考価格であり、リアルタイム価格ではない。各ツールの応答に `last_investigated` とデータ取得時刻を必ず含め、instructions でも「最新価格は詳細ページ／Amazonで確認」と明記する。
- 在庫なし・価格0の商品（summary で87件）が混在する。検索ツールのデフォルトでは除外する（SNS運用で確立した「price 0・null・欠落は除外」のルールと揃える）。

## 3. 全体構成

```
MCPクライアント (ChatGPT / Claude / Muse 等)
        │  HTTPS (Streamable HTTP, JSON-RPC)
        ▼
https://mcp.amazon-hikaku.com/mcp
        │  Cloud Run ドメインマッピング (証明書は自動発行)
        ▼
Cloud Run サービス「amazon-hikaku-mcp」
  プロジェクト: amazon-product-article / リージョン: asia-northeast1
  Python + FastMCP (ステートレス), メモリ内キャッシュ
        │  起動時 + TTL 60分で条件付きGET (ETag / Last-Modified)
        ▼
www.amazon-hikaku.com の公開JSON (GitHub Pages)
  index.json / products-summary.json / deals/index.json
```

設計判断:

- **ホスティングは Cloud Run、プロジェクトは `amazon-product-article` に確定できる。** 実測で Cloud DNS ゾーン `amazon-hikaku-com`（public）が同プロジェクトにあることを確認した。DNSレコード追加とサービス配置が1プロジェクトで完結する。なお現時点で同プロジェクトでは `run.googleapis.com` が未有効化なので、実装時に有効化する（Cloud DNS API は有効化済み）。
- ロードバランサは使わない（月約$18.25で予算アラート2,000円を単独で超えるため）。ドメインマッピングは Preview 扱い・レイテンシの留保つきだが、本用途では許容する。
  - **【v1.1 追加】遅延リスクの明記と代替案**: Google Cloud 公式の Known issues で、カスタムドメイン経由の高レイテンシが asia-northeast1 と us-east4 で特に顕著とされている（TLS 1.0/1.1 を無効化できない制約もある）。採用判断は維持するが、実測で遅延が目立つ場合の切替先として **Firebase Hosting の Cloud Run リライト（無料枠あり）** を第一代替に控える。Cloudflare 経由は §（前回検討）のとおり無料プランでは独自サブドメインを向けられないため代替にしない。
- データは DB を持たず、公開JSONのメモリ内キャッシュのみ。サイト側の Hugo ビルド／日次調査がそのままデータ更新になる（既存の運用フローを一切変えない）。
- 認証なし・読み取り専用で公開する。書き込み系ツールは作らない。

## 4. MCP仕様

- トランスポート: **Streamable HTTP**、ステートレスモード（セッション保持なし。長時間接続は課金時間になるため、応答は通常JSONで返す設定にする）
- **【v1.1 追加】プロトコル世代の混在への配慮**: クライアント側には initialize ハンドシェイク＋セッション前提の世代と、セッション廃止のネイティブ・ステートレス世代が混在している（第三者レビュー指摘。具体的な版数・日付は実装時に公式仕様で再確認する）。FastMCP の `stateless_http=True, json_response=True` を基本とし、§12 の検証で「両世代クライアントからの接続」「Origin ヘッダ検証と CORS の挙動」を必須項目にする。未知の protocolVersion を即拒否しない寛容な受理を心がける。
- エンドポイント: `POST /mcp`（GET はヘルスチェック兼用に `/` と `/healthz` を用意）
- サーバー情報: name `amazon-hikaku`, version はサイトのデータ更新日と独立に管理
- instructions（llms-full.txt の参照ガイドを移植。要旨）:
  - 独自スコア（0〜100）の高い商品を優先する。目安は80点以上
  - 回答では各商品の詳細レポートURL（`https://www.amazon-hikaku.com/{asin}/`）を出典として必ず示す
  - 価格は調査時点の参考価格。最新価格・在庫は詳細ページで確認するよう案内する
  - 候補は原則5〜8件に絞る
- 応答形式: 構造化JSON（機械可読）を正とし、併せて人間可読のテキスト要約を返す。各商品には title / asin / score / price / brand / category / description / image / detail_url を含める（llms-full.txt の出力フォーマット例と互換の並び）

## 5. ツール設計（v1: 8ツール）

共通仕様（v1.1 で明記）:

- **アノテーション**: 全ツール読み取り専用のため、各ツールに `readOnlyHint=True` / `idempotentHint=True` / `destructiveHint=False` を付与し、クライアント側の確認プロンプトを減らす。
- **検索の正規化**: `query` は NFKC 正規化（全角／半角・大文字小文字の統一）＋空白分割の AND 検索とする。
- **カテゴリ展開**: `category` 引数は子カテゴリ名だけでなく、親カテゴリ名やスラッグ（例: `pc-workstation`）が渡されても、categorygroups.json で配下の子カテゴリへ自動展開する。
- **ブランド正規化**: `brand` フィルタと `list_brands` は brandgroups.json の matcher（正規表現）を併用し、英語／カタカナ等の表記揺れを取りこぼさない。生ブランド名は products-summary.json との ASIN 結合で得る（§2.1）。
- **出力スキーマ統一**: deals の camelCase フィールドは読み込み層で snake_case に正規化し、全ツールが共通の商品サマリー構造（asin / title / brand / category / price / score / description / image / detail_url / last_investigated）を返す。

| ツール | 入力 | 動作とデータ源 |
|---|---|---|
| `search_products` | query（任意）, category, brand, min_score（既定80）, max_price, in_stock_only（既定true）, sort（score/price）, limit（既定8, 最大20） | index.json をメモリ検索。タイトル・カテゴリ・ブランド・要約を対象にキーワード照合し、スコア降順で返す |
| `get_product` | asin | index.json から1件の詳細（要約・スペック・価格推移以外の全項目・調査日）。スコア根拠は v1 では詳細URLへ誘導 |
| `compare_products` | asins（2〜5件） | 比較表向けに、価格・スコア・主要スペックを揃えた構造で返す |
| `list_categories` | （なし） | 親カテゴリ→子カテゴリ（165種）の階層と件数。categorygroups.json（公開フェッチ）＋実件数集計 |
| `list_brands` | query（部分一致検索）, limit | ブランド3,024種は全件返却せず検索式にする。各ブランドの件数つき |
| `get_deals` | category（任意）, min_savings_percentage, limit | deals/index.json（671件）から割引率・スコアで選定 |
| `get_bargain` | target_amount（2000/3500 等の買い合わせ目標額）, category（任意）, limit | price ≤ target_amount をスコア降順に。送料無料ライン調整の用途 |
| `get_recommendations` | category（任意）, limit | score 上位（目安90点以上）から選定する search_products のプリセット |

v1.1 候補: `get_product` へのスコア根拠追加（サイト側 index.json 拡張が前提）、買い物調査プロンプトの prompts 提供、llms ガイド全文の resources 提供。

## 6. データ更新まわり

- 起動時に index.json（約13.4MB）と deals、categorygroups、brandgroups を取得し、不要フィールド（specs_html 等）を落とした軽量レコードに変換してメモリ保持する。想定メモリは変換後で数百MB以内に収まる見込みだが、実装時に実測して確定する。
- **【v1.1 追加】取得は gzip 前提＋並列化**: GitHub Pages からの取得は `Accept-Encoding: gzip`（httpx の既定動作）を使い、各ファイルを `asyncio.gather` で並列取得してコールドスタートを短縮する。
- TTL 60分。期限切れ後の初回リクエストで条件付きGETを行い、未更新なら再パースしない。取得失敗時は古いキャッシュで継続し、応答の `data_stale` で明示する。
- **【v1.1 追加】同時再取得（Thundering Herd）対策を必須とする**: TTL 切れの瞬間に同時実行80のリクエストが一斉に再取得・再パースするとメモリが跳ねて 1GiB 上限で OOM になり得る。更新処理は `asyncio.Lock` によるシングルフライト（1本だけが取得し、他は待つか古いキャッシュを即返す Stale-While-Revalidate）で実装する。
- 応答には常に `data_fetched_at`（サーバー取得時刻）と商品ごとの `last_investigated` を含める。

## 7. Cloud Run 構成

| 項目 | 設定 |
|---|---|
| プロジェクト | amazon-product-article |
| リージョン | asia-northeast1（東京、Tier 1料金、ドメインマッピング対応） |
| サービス名 | amazon-hikaku-mcp |
| CPU / メモリ | 1 vCPU / 1 GiB（JSON保持のため 512MiB ではなく 1GiB を初期値とし、実測で下げられるか確認） |
| 課金モード | リクエスト課金（デフォルト）。CPU常時割り当ては使わない |
| インスタンス | min 0 / max 5（暴走時の請求頭打ち） |
| 同時実行 | 80 / タイムアウト 60秒 |
| 公開 | 未認証アクセス許可（公開・読み取り専用MCPのため） |
| 実装 | Python 3.13 + FastMCP、コンテナは Artifact Registry（0.5GBまで無料の範囲） |
| 有効化が必要なAPI | run.googleapis.com / artifactregistry.googleapis.com / cloudbuild.googleapis.com（いずれも同プロジェクトで未有効化の状態から） |

## 8. DNS・ドメイン手順（実装時）

1. ドメイン所有権の確認: amazon-hikaku.com は Search Console で所有権確認済み（ユーザー実施）。ドメインマッピング作成時に同じ Google アカウントの所有権が使えるか実装時に確認する。
2. `gcloud beta run domain-mappings create --service amazon-hikaku-mcp --domain mcp.amazon-hikaku.com --region asia-northeast1 --project amazon-product-article`
3. 返ってきたレコード（CNAME → `ghs.googlehosted.com`）を Cloud DNS ゾーン `amazon-hikaku-com` に追加する。
4. 証明書は自動発行（DNS反映後、目安15分〜24時間）。発行完了を `domain-mappings describe` で確認する。
5. 既存レコード（www、apex、検証用TXT等）には一切触れない。

## 9. コスト試算とガード（2026-11-22 試算の要約）

前提: 1 vCPU / 512MiB〜1GiB、リクエスト課金、min 0、平均0.5秒/回、応答10KB、$1≒155円。無料枠は請求アカウント単位で月 180,000 vCPU秒 / 360,000 GiB秒 / 200万リクエスト。

| トラフィック | 月額目安 |
|---|---|
| 100回/日 | 実質0円（無料枠の0.8%） |
| 1,000回/日 | 実質0円（同8.3%、egress数円のみ） |
| 10,000回/日 | 約56円（同83%、ほぼ egress 分） |
| 100,000回/日 | 約5,700円（非現実的な水準） |

ガード:

- min instances は 0 を維持（1にするとアイドルだけで月約1,500円）
- max instances 5 で上限を固定
- 既存の月額予算アラート2,000円と日次費用監視（gcp-cost-monitor）がそのまま効く
- Cloud DNS は既存ゾーンへのレコード追加のみで増分ほぼゼロ

注記（v1.1）:

- 無料枠は請求アカウント単位の合算であり、同一請求アカウント内の他プロジェクト（cloud-execution-environment 等）が将来 Cloud Run を使い始めると取り分は減る。現時点では他プロジェクトの Cloud Run 利用は無い。
- コールドスタート時は JSON 取得・パース分だけ 0.5秒/回 の前提より CPU 秒を多く消費する。上記シナリオはアクセスの大半がキャッシュヒットである前提の見積もりで、早朝の初回アクセス等では上振れる。

## 10. セキュリティ・運用上の注意

- 公開・認証なしのため、ツールは全て読み取り専用とし、内部情報（Amazon Creators API の生レスポンス等）は一切扱わない。扱うのはサイトが既に公開しているJSONのみ（外部AIへの Program Content 投入注意とも整合）。
- `affiliate_url` の扱い: 公開JSONに既に含まれているが、MCP応答の主たる出典は自サイトの詳細URLとし、affiliate_url を応答に含めるかどうかは決定事項（§11）とする。含めない場合でも機能上の支障はない。
- 乱用対策: max instances 上限に加え、limit の上限（20件）と query 長の上限をサーバー側で設ける。個人情報・認証情報は扱わない。【v1.1 追加】リクエストボディサイズの上限と、IP単位の簡易レート制限も入れる。
- ログ: Cloud Run の標準ログのみ。リクエスト内容に個人情報が入る設計にしない。

## 11. 発見可能性（公開後の導線）

- llms.txt / llms-full.txt に MCP エンドポイントを追記し、「詳細な検索は MCP が推奨」と案内する（サイト側変更として別途承認）。
- サイトの `/ai/` ページに MCP の接続方法（ChatGPT / Claude 等への登録手順）を追記する候補。
- MCP Registry 等への登録は、公開後の様子を見てから判断する。

## 12. 実装ステップ（承認後）

1. リポジトリ作成（配置は §13 の決定事項）し、FastMCP で8ツールを実装。ローカルで公開JSONを使って単体テスト。
2. MCP Inspector で initialize / tools/list / tools/call を検証し、応答サイズと処理時間を実測（試算の前提 0.5秒/回との照合）。あわせて **セッション前提世代とステートレス世代の両クライアントからの /mcp 接続**、**Origin ヘッダ検証・CORS の挙動**、TTL 切れ直後の同時アクセスで再取得が1本に集約されること（シングルフライト）を確認する。
3. プロジェクト amazon-product-article で API を有効化し、Cloud Run へデプロイ（まず run.app のURLで動作確認）。
4. ドメインマッピング＋Cloud DNS レコード追加、証明書発行を待って `https://mcp.amazon-hikaku.com/mcp` で再検証。
5. 実クライアント（ChatGPT コネクタ / Claude / Muse のいずれか）から接続テストし、「カテゴリ＋予算」等の買い物調査で出典URLつき回答が返ることを確認。
6. llms.txt への追記を amazon-product-article 側へ別途提案。

所要の目安: ステップ1〜3が本体作業。DNS・証明書は待ち時間を含む。

## 13. 決定事項（実装前にポちさんの判断が要るもの）

第三者レビューの推奨は全項目で本設計の推奨と一致した（項目3は方式が MCP専用JSON に更新）。以下はまだポちさん未決定であり、実装着手時にまとめて確認する。

1. **リポジトリ配置**: 新規リポジトリ（例: aegisfleet/amazon-hikaku-mcp）か、amazon-product-article 内のサブディレクトリか。設計としては新規リポジトリを推奨（サイトの Hugo ビルドとデプロイ周期を分離できるため）。
2. **affiliate_url を応答に含めるか**: 含める（公開JSONどおり）／含めない（出典は詳細URLのみ）。無難なのは後者で開始し、必要なら後から追加。レビューも「v1 では含めない」を推奨（外部AIの回答内でアソシエイト規約に沿った表示を強制できないため）。
3. **score_rationale の MCP専用JSON化**（§2.2）: v1 と同時にやるか、後回しにするか。同時実施を推奨（引用価値が上がる。方式は index.json 拡張ではなく prebuild での専用JSON生成）。
4. **公開範囲**: 認証なし公開で確定してよいか（本設計はその前提）。レビューも同調で、補助策としてリクエストボディサイズ上限と簡易レート制限の追加を推奨。

---

## 付録: 実測値の根拠（2026-11-22）

- DNS: NS は ns-cloud-a1〜a4.googledomains.com、www は CNAME で aegisfleet.github.io、apex は GitHub Pages の A レコード（DNS over HTTPS で確認）。
- Cloud DNS ゾーン `amazon-hikaku-com`（public）は GCP プロジェクト `amazon-product-article` に存在（gcloud で確認）。同プロジェクトでは run.googleapis.com は未有効化。
- データ件数・サイズ・フィールドは公開JSONを実際に取得して集計した値。llms-full.txt のサイズ記載（1.7MB / 9.3MB）は古くなっている。
- カテゴリ165種・ブランド3,024種・スコア80点以上3,901件・価格なし87件は products-summary.json の実測集計。
