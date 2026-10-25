#!/usr/bin/env bash
# Removes the legacy market-making code, the duplicated flat modules, progress notes and the nested bundle,
# so the repository contains only the consolidated DEX liquidity-provision platform.
# Run from the repository root AFTER unzipping the milestone:   bash scripts/cleanup_legacy.sh [--dry-run]
# Everything stays recoverable from git history. Delete this script once you have committed.
set -euo pipefail

if [[ ! -f app/service.py || ! -f app/api/server.py ]]; then
  echo "Run this from the repository root, after unzipping the milestone files." >&2
  exit 1
fi
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1

FILES=(
  COLLECTION.md
  DEX_POOL_DATA_SCHEMA.md
  DEX_PRODUCT_DIRECTION.md
  EVALUATION.md
  EXPERIMENTS.md
  MARKET_REPLAY.md
  MILESTONES_4_5_APPLY.md
  MILESTONE_2_APPLY.md
  MILESTONE_3_APPLY.md
  MILESTONE_6_7_APPLY.md
  OPERATIONS.md
  PAPER_TRADING.md
  PIPELINE.md
  PROGRESS_REPORT.md
  PROGRESS_REPORT_M4_M5.md
  PROGRESS_REPORT_M6_M7.md
  QUOTE_STUDY.md
  RESEARCH_AGENT.md
  WEBSOCKET_FEED.md
  app/amm_economics.py
  app/backtest.py
  app/capture_review.py
  app/dashboard.html
  app/defi_milestone.py
  app/dex_pool_advisor.py
  app/dex_pool_data.py
  app/doh.py
  app/engine.py
  app/evaluate.py
  app/experiments.py
  app/feed_timing.py
  app/fill_cost.py
  app/market_data.py
  app/network_check.py
  app/okx_public.py
  app/okx_stream.py
  app/operations.py
  app/paper.py
  app/paper_report.py
  app/paper_store.py
  app/pipeline.py
  app/pool_selector.py
  app/product_api_server.py
  app/product_service.py
  app/quote_study.py
  app/replay.py
  app/research_agent.py
  app/server.py
  app/storage.py
  app/stream_paper.py
  app/wallet_ledger.py
  examples/evaluation_plan.json
  examples/market_sample.csv
  milestone3_bundle/DEX_PRODUCT_DIRECTION.md
  milestone3_bundle/MILESTONE_3_APPLY.md
  milestone3_bundle/PROGRESS_REPORT.md
  milestone3_bundle/README.md
  milestone3_bundle/app/dex_pool_advisor.py
  milestone3_bundle/app/wallet_ledger.py
  milestone3_bundle/tests/test_dex_pool_advisor.py
  milestone3_bundle/tests/test_wallet_withdrawal.py
  requirements-websocket.txt
  tests/test_amm_economics.py
  tests/test_backtest.py
  tests/test_backtest_cli.py
  tests/test_capture_review.py
  tests/test_defi_milestone.py
  tests/test_dex_pool_advisor.py
  tests/test_dex_pool_data.py
  tests/test_doh.py
  tests/test_engine.py
  tests/test_evaluate.py
  tests/test_execution_diagnostics.py
  tests/test_experiments.py
  tests/test_feed_timing.py
  tests/test_fill_cost.py
  tests/test_market_data.py
  tests/test_network_check.py
  tests/test_operations.py
  tests/test_paper.py
  tests/test_paper_report.py
  tests/test_pipeline.py
  tests/test_product_api_server.py
  tests/test_product_service.py
  tests/test_quote_study.py
  tests/test_research_agent.py
  tests/test_storage.py
  tests/test_stream_paper.py
  tests/test_wallet_withdrawal.py
)

removed=0
for f in "${FILES[@]}"; do
  [[ -e "$f" ]] || continue
  echo "remove $f"
  if [[ $DRY -eq 0 ]]; then
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then git rm -q -f -- "$f"; else rm -f -- "$f"; fi
  fi
  removed=$((removed + 1))
done
if [[ $DRY -eq 1 ]]; then echo "dry run: $removed file(s) would be removed"; exit 0; fi
echo "removed $removed file(s)"
find . -path ./.git -prune -o -path ./.venv -prune -o -path ./data -prune -o -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -path ./.git -prune -o -path ./.venv -prune -o -path ./data -prune -o -type d -empty -delete 2>/dev/null || true
