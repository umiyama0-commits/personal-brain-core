"""
routes/ — main.py から切り出した APIRouter 群 (★2026-05-22 Phase 2)

設計:
- main.py の `@app.get/post` decorator 直叩きを APIRouter に整理
- 各 router は独立した module で test しやすく
- shared state (app.state.brain 等) は `request.app.state` 経由でアクセス
- main.py から `app.include_router(...)` で登録

Phase 2 で切り出し:
- alignment_trial: /alignment-trial/* (5 endpoint)
- brain_api: /api/cost-investigation / /api/recent-failures
"""
