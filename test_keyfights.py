import asyncio
import unittest

import dota_keyfights


class KeyfightEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_original_candidate_order_and_full_blocks(self):
        cands = [
            {"index": 4, "start": 10, "end": 20, "pick_reason": "关键"},
            {"index": 2, "start": 30, "end": 40, "pick_reason": "高光"},
        ]
        seen = []

        async def build(c):
            seen.append(c["index"])
            return "完整窗口 " + str(c["index"])

        out = await dota_keyfights.collect(cands, build)
        self.assertEqual(seen, [4, 2])
        self.assertIn("原始第 4 波", out)
        self.assertIn("原始第 2 波", out)
        self.assertIn("完整窗口 4", out)
        self.assertIn("完整窗口 2", out)

    async def test_budget_never_slices_a_window(self):
        async def build(_):
            return "x" * 100

        out = await dota_keyfights.collect(
            [{"index": 1, "start": 0, "end": 10}], build, budget=1
        )
        self.assertNotIn("x" * 20, out)
        self.assertIn("没有截取前半段", out)

    async def test_cumulative_budget_replaces_later_windows_whole(self):
        async def build(candidate):
            return "a" * candidate["size"]

        out = await dota_keyfights.collect(
            [
                {"index": 1, "start": 0, "end": 10, "size": 100},
                {"index": 2, "start": 11, "end": 20, "size": 100},
                {"index": 3, "start": 21, "end": 30, "size": 100},
            ],
            build,
            budget=len(dota_keyfights.HEADER) + 250,
        )
        self.assertIn("a" * 100, out)
        self.assertEqual(out.count("没有截取前半段"), 1)
        self.assertIn("完整窗口超过剩余输入预算", out)

    async def test_empty_and_failed_evidence_are_not_zero_contribution(self):
        async def empty(_):
            return ""

        async def failed(_):
            raise RuntimeError("export failed")

        cands = [{"index": 1, "start": 1, "end": 2}, {"index": 2, "start": 3, "end": 4}]
        out = await dota_keyfights.collect(cands, lambda c: empty(c) if c["index"] == 1 else failed(c))
        self.assertIn("不能据此认定无人参战", out)
        self.assertIn("录像证据不可用（RuntimeError）", out)

    async def test_cancellation_propagates(self):
        async def cancel(_):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await dota_keyfights.collect([{"index": 1, "start": 1, "end": 2}], cancel)


if __name__ == "__main__":
    unittest.main()
