"""Quick smoke test for うみやまAI tone after prompt changes."""
import sys
sys.path.insert(0, '/app')
import asyncio, os, httpx
from brain_wiki import BrainWiki


async def main():
    url = os.getenv('LITELLM_URL', 'http://litellm:4000')
    key = os.getenv('LITELLM_MASTER_KEY', '')
    async with httpx.AsyncClient(timeout=60) as http:
        brain = BrainWiki(http, url, key)
        prompts = [
            ('TASK-REQUEST (should DEFLECT, not write the deck)',
             '次回の店舗向け定例会のプレゼン資料、20ページぐらいで作って。テーマは新人離職対策。'),
            ('BUSINESS M2 (sharp / concrete)',
             '店長のやる気がなさそうなんだけど、どう接したらいいかな'),
            ('LIFE M2 (warm tone, not sharp)',
             '最近キャリアに迷ってる。今の仕事続けるべきか転職すべきか分からなくなってきた。'),
            ('DATA M1 (immediate numbers)',
             '今日の全店売上と客数は?'),
            ('M3 DECISIVE (clear direction, decision back to user)',
             'AかBで明日までに決めないといけない。海山ならどっち?Aは安定路線、Bは攻めるけどリスクあり。'),
        ]
        for label, q in prompts:
            print('=' * 80)
            print(f'[{label}]')
            print(f'Q: {q}')
            try:
                r = await brain.clone_respond_public(q, [], 'fast-gpt')
            except Exception as e:
                r = f'(ERROR: {type(e).__name__}: {e})'
            print(f'A: {r}')
            print()


if __name__ == '__main__':
    asyncio.run(main())
