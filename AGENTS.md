# AGENTS.md — 開発者向けメモ

利用者向けドキュメントは README.md。本ファイルはこのリポジトリで作業する開発者・AIエージェント向け。

## 構成

- Python + FastMCP（Streamable HTTP、ステートレス、`json_response`）、Cloud Run 上で稼働
- `amazon_hikaku_mcp/server.py`: 10ツール、instructions、ガード（ボディ64KB上限、IP単位 240 req/min）、`/health` と `/` のカスタムルート
- `search_products` は total_count と facets（全件のブランド・カテゴリ・価格帯）も返す。AI が具体的な選択肢で絞り込みを聞き返すための材料なので、削らないこと
- 検索の表記ゆれ吸収は data.py 側の仕組み（ブランド日英エイリアス `BRAND_ALIASES`、同義語グループ `SYNONYM_GROUPS`、関連度ソート、Sudachi 形態素照合）。自然な日本語クエリの取りこぼし対策なので、単純部分一致へ戻さないこと
- 形態素照合は Sudachi SplitMode B のみ（モードAは複合語を砕きすぎて無関係な商品に一致するため不採用。実測で確定）。部分一致 OR 形態素部分集合で、形態素は一致を「広げるだけ」。sudachipy と sudachidict_core はピン留めしたペア（0.7で辞書形式が変わった）なので、上げる時はセットで慎重に。Sudachi が読み込めない場合は部分一致のみへ劣化する設計
- 全ツールで商品サマリーは `product_summary` / `deal_summary` を通すこと（citation・score_label・price_label の付与漏れ＝P0バグの再発防止）。商品を含まない応答では envelope の notes を切る
- プロンプト2件（shopping_consultation / bargain_hunting）も公開している。対応クライアントでは相談テンプレートとして提示される
- instructions は相談の進め方まで規定する（複数クエリ分解・段階的緩和・必須条件の無断緩和禁止・データ範囲と鮮度の明示・推測禁止）。suggestions は total<=3 で返す（0件だけでなく少数ヒットでも緩和材料を渡すため）。search_products には exclude_brand / exclude_category（親指定で配下を全除外）、find_similar_products には min_score がある。find_similar_products はカテゴリを母集団とし、タイトルの製品タイプ形態素（type_morphs_of: 普通名詞のみ・日本語文字を含むもの）で候補を絞る（match_basis=title_type）。同タイプ候補が無い場合はカテゴリ候補へフォールバックし match_basis=category_fallback で開示する。子カテゴリには異種混在（冷蔵庫・洗濯機・大型）があるため、この絞りを外して素のカテゴリ候補へ戻さないこと。facets には category_parents（親カテゴリ別の混在内訳）も含む。ギフト検索: search_products / get_recommendations の recipient / occasion は、サイト側が調査文から抽出して mcp-products.json に載せるギフト属性（gift_recipients / gift_occasions）で絞る。属性が付くのはギフト文脈のあった商品だけなので、属性なし＝ギフト不向きではない。プロンプトは3つ（shopping_consultation / bargain_hunting / gift_consultation）
- スペック絞り込み: search_products の spec_filters は index.json の specs（全商品共通の固定13キー: ストレージ / メモリ / 画面 / バッテリー / 容量 / 重量 / サイズ / OS / プロセッサ / 素材 / その他 等）で絞る。条件は部分一致か数値比較（単位ファミリー内で換算、data.py の spec_value_matches）。キーを持たない商品はヒットしない。発見用に list_spec_keys（カテゴリ別のキー・件数・値の例）がある。suggestions はスペック条件の解除も提案する
- `amazon_hikaku_mcp/data.py`: サイトの公開JSONを取得してメモリにキャッシュ（TTL 60分、asyncio.Lock のシングルフライト、失敗時は stale 継続＋60秒リトライ抑制）
  - データ源: `index.json` / `products-summary.json` / `deals/index.json` / `data/categorygroups.json` / `data/brandgroups.json` / `mcp-products.json`（任意・優先）
  - index には brand が無いため summary と ASIN 結合し、mcp-products があれば brand と `score_rationale` をそちらで上書きする（`data_source` が `mcp-products` になる）
  - deals は camelCase のため snake_case へ正規化する層を持つ

## ローカル開発

```bash
uv sync
uv run amazon-hikaku-mcp        # http://127.0.0.1:8080/mcp（PORT 環境変数で変更可）
uv run --with pytest pytest tests/ -q   # オフライン単体テスト
uv run python scripts/smoke.py          # 起動中サーバーへの素の JSON-RPC スモーク
uv run python scripts/verify_live.py    # 本番への実用検証（公式SDKクライアント、14シナリオ）
```

## デプロイ

```bash
gcloud run deploy amazon-hikaku-mcp --source . \
  --project amazon-product-article --region asia-northeast1 \
  --cpu 1 --memory 1Gi --min-instances 0 --max-instances 5 \
  --concurrency 80 --timeout 60 --allow-unauthenticated
```

- ドメイン `mcp.amazon-hikaku.com` は Cloud Run ドメインマッピング＋ Cloud DNS（ゾーン `amazon-hikaku-com`、同プロジェクト）の CNAME で接続済み
- デプロイ後は `scripts/verify_live.py` で本番検証すること

## 守るべき制約・ハマりどころ

- **mcp SDK は `>=1.12,<2` にピン留め**。v2 で FastMCP が `MCPServer` に改名され API が変わった。v2 移行は手作業の判断事項で、Dependabot の semver-major は ignore 設定済み（`.github/dependabot.yml`）
- **ヘルスチェックは `/health`**。`/healthz` は Cloud Run のフロントエンドに横取りされコンテナに届かない
- データ取得クライアントは `trust_env=False` ＋ `HTTPS_PROXY` / `SSL_CERT_FILE` の明示読み。httpx の env 解釈は `NO_PROXY` の `[::1]` 形式エントリで落ちる環境があるため
- `DESIGN.md` は**ローカル限定**（予算・運用事情を含むため .gitignore 済み、公開履歴からも除去済み）。コミットしないこと
- affiliate_url は v1 のレスポンスに含めない（出典は amazon-hikaku.com の詳細ページ URL に一本化する方針）
- MCP のツール一覧・主要引数・回答品質ガードレール（INSTRUCTIONS）を変更した際は、本サイト側（`amazon-product-article` リポジトリ）の `static/llms.txt` および `static/llms-full.txt` も同期して更新すること
- CI は `uv sync --locked` ＋ pytest ＋ サーバーの import スモーク（SDK 破壊的変更の検知用）＋ `pip-audit --skip-editable`（既知の脆弱性監査）。main への push / PR で回る

## コスト設計の要点

- Cloud Run 無料枠（請求アカウント単位）内で収まる前提: min 0、リクエスト課金、1 vCPU / 1 GiB
- ロードバランサは使わない（ドメインマッピングで代替）。レイテンシが問題になったら Firebase Hosting リライトへの切替を検討
