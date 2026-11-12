# kkk-post-notifier

Non-AI notifier that posts Kevin's new Instagram, YouTube, TikTok and Threads posts to Discord `#社群公告`.

- Runs on GitHub Actions every 5 minutes, 08:00-01:59 Asia/Taipei (`.github/workflows/notifier.yml`).
- `notifier.py` is Python 3 stdlib only. No AI.
- `state.json` holds only seen post IDs and timestamps (no secrets). Each run commits it back.
- Secrets (repo Actions secrets, never in the repo): `IG_GRAPH_TOKEN`, `THREADS_TOKEN`, `DISCORD_WEBHOOK_URL`, `GH_SECRETS_PAT` (lets the job rotate `THREADS_TOKEN` after its weekly refresh).
- Dedup: before each Discord post, the post ID is written to `state.json` and pushed. If that push fails, nothing is posted. If Discord rejects the post, the ID is un-marked so a later run retries. If Discord does not answer clearly (timeout), the ID stays marked, so a post can be missed but never sent twice.
- Manual test: Actions tab, `notifier`, Run workflow, keep `dry_run` checked.
- Message format (fixed): `🔥 貴一在 {平台} 發新{貼文/影片}了！` / short caption / `👉 {連結}`.
