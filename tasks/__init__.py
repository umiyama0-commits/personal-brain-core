"""
tasks/ — main.py から切り出した background tasks (★2026-05-22 Phase 4)

main.py の lifespan() で asyncio.create_task() で起動される async functions を整理。

Phase 4 で切り出し:
- self_improve: 自己改善ループ + state file 永続化
- (Phase 5 以降: watchers / scheduled daily-batch 系)
"""
