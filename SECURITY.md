# セキュリティポリシー (Security Policy)

`amazon-hikaku-mcp`（[ポちのAmazon商品徹底比較.com](https://www.amazon-hikaku.com/) 公式リモートMCPサーバー）のセキュリティ設計と脆弱性報告手順について説明します。

## サポート対象バージョン (Supported Versions)

本プロジェクトは `https://mcp.amazon-hikaku.com/mcp` にてホスティングされている最新デプロイ（`main` ブランチ）を継続的にサポート・更新しています。

| バージョン / ブランチ | サポート状況 |
| --- | --- |
| `main`（本番稼働中） | :white_check_mark: 対応 |
| 過去のコミット | :x: 最新版へ更新されます |

## セキュリティ設計と安全策 (Security Architecture)

ユーザーがChatGPT・Claude・Gemini等のAIクライアントに安心して接続できるよう、本MCPサーバーは以下の原則に基づいて設計・運用されています。

1. **完全読み取り専用（副作用なし）**
   - 公開している全9ツールは検索・比較・参照のみを行い、MCP標準の `ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)` を明示しています。外部データの書き換えや購入・決済等の操作は一切行いません。
2. **認証不要・個人情報の非保持（ステートレス設計）**
   - Streamable HTTP のステートレスモード（`stateless_http=True`）で稼働しており、APIキー・OAuthトークン・ユーザーアカウント情報・会話セッション履歴を保持しません。
3. **データソースの限定（公開JSONのみ参照）**
   - サーバーが参照するのは `https://www.amazon-hikaku.com` 上に静的配信されている公開商品調査データ（JSON）のみです（1時間キャッシュ、シングルフライト更新）。任意の外部URLへのリクエスト機能（SSRFリスク）は持ちません。
4. **過負荷・悪用防止ガード（Abuse Guards）**
   - リクエストボディ上限（`MAX_BODY_BYTES = 64KB`、Content-Length検査およびストリーム実測の両方で制限）と、クライアントIP単位のレート制限（`240 req/min`、追跡IPテーブル上限付き）をミドルウェアで適用しています。
   - HTTPレスポンスには `X-Content-Type-Options: nosniff` や `Cache-Control: no-store` 等のセキュリティヘッダーを付与しています。
5. **コンテナとサプライチェーンの保護**
   - Dockerコンテナは `uv.lock` に基づく決定論的ビルド（`--frozen`）で依存関係を固定し、非特権ユーザー（non-root）で実行しています。
   - GitHub Actions CI および Dependabot により、依存パッケージの既知の脆弱性（`pip-audit`）とアップデートを継続的に監視しています。

## 脆弱性の報告方法 (Reporting a Vulnerability)

セキュリティ上の問題や脆弱性を発見された場合は、公開Issueではなく、以下の手順でご報告ください。

- **報告窓口**: 本リポジトリの [GitHub Security Advisories（Report a vulnerability）](https://github.com/aegisfleet/amazon-hikaku-mcp/security/advisories/new) から非公開でご報告ください。
- **対応目安**: 報告受領後、原則として72時間以内に内容を確認し、再現確認および修正・デプロイの対応状況を返信いたします。
