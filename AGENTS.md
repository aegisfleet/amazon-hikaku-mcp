# AGENTS.md — 開発者向けメモ

利用者向けドキュメントは README.md。本ファイルはこのリポジトリで作業する開発者・AIエージェント向け。

## 構成

- Python + FastMCP（Streamable HTTP、ステートレス、`json_response`）、Cloud Run 上で稼働
- `amazon_hikaku_mcp/server.py`: 9ツール、instructions、ガード（ボディ64KB上限、IP単位 240 req/min）、`/health` と `/` のカスタムルート
- `search_products` は total_count と facets（全件のブランド・カテゴリ・価格帯）も返す。AI が具体的な選択肢で絞り込みを聞き返すための材料なので、削らないこと
- 検索の表記ゆれ吸収は data.py 側の仕組み（ブランド日英エイリアス `BRAND_ALIASES`、同義語グループ `SYNONYM_GROUPS`、関連度ソート）。自然な日本語クエリの取りこぼし対策なので、単純部分一致へ戻さないこと
- 全ツールで商品サマリーは `product_summary` / `deal_summary` を通すこと（citation・score_label・price_label の付与漏れ＝P0バグの再発防止）。商品を含まない応答では envelope の notes を切る
- プロンプト2件（shopping_consultation / bargain_hunting）も公開している。対応クライアントでは相談テンプレートとして提示される
- instructions は相談の進め方まで規定する（複数クエリ分解・段階的緩和・必須条件の無断緩和禁止・データ範囲と鮮度の明示・推測禁止）。suggestions は total<=3 で返す（0件だけでなく少数ヒットでも緩和材料を渡すため）。search_products には exclude_brand、find_similar_products には min_score がある
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
- CI は pytest ＋ サーバーの import スモーク（SDK 破壊的変更の検知用）。main への push で回る

## コスト設計の要点

- Cloud Run 無料枠（請求アカウント単位）内で収まる前提: min 0、リクエスト課金、1 vCPU / 1 GiB
- ロードバランサは使わない（ドメインマッピングで代替）。レイテンシが問題になったら Firebase Hosting リライトへの切替を検討
