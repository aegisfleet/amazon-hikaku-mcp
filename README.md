# amazon-hikaku-mcp

[amazon-hikaku.com](https://www.amazon-hikaku.com/)（ポちのAmazon商品徹底比較.com）の調査データを、AIアシスタントから検索・参照できるようにするリモートMCPサーバー。

- エンドポイント: `https://mcp.amazon-hikaku.com/mcp`（Streamable HTTP、ステートレス、認証なし・読み取り専用）
- データ源: サイトが公開しているJSON（index.json / products-summary.json / deals/index.json / data/categorygroups.json / data/brandgroups.json、任意で mcp-products.json）をメモリにキャッシュし、TTL 60分で更新
- 設計の詳細: [DESIGN.md](DESIGN.md)
- 稼働状況: 2026-11-22 デプロイ済み（Cloud Run リビジョン amazon-hikaku-mcp-00002-wdz、ドメインマッピング＋管理証明書 Ready）。当面のデータ源は index+summary 結合（`mcp-products.json` はサイト側 prebuild への追加後に自動で優先利用される）

## ツール

| ツール | 概要 |
|---|---|
| `search_products` | キーワード／カテゴリ／ブランド／スコア／価格で商品検索 |
| `get_product` | ASINで詳細（スペック・在庫・調査日・スコア根拠） |
| `compare_products` | 2〜5件の比較 |
| `list_categories` | 親→子カテゴリ階層と件数 |
| `list_brands` | ブランド検索（表記揺れグループ対応） |
| `get_deals` | セール商品（割引率順） |
| `get_bargain` | 目標金額以下の買い合わせ候補 |
| `get_recommendations` | スコア90点以上の高評価商品 |

## ローカル実行

```bash
uv sync
uv run amazon-hikaku-mcp        # http://127.0.0.1:8080/mcp
uv run pytest tests/            # オフライン単体テスト
uv run python scripts/smoke.py  # 起動中サーバーへのJSON-RPCスモーク
```

## デプロイ

Cloud Run（プロジェクト `amazon-product-article`、リージョン `asia-northeast1`）へ Dockerfile でデプロイする。手順とコスト設計は DESIGN.md §7〜§9 を参照。
