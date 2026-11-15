"""离线验证解说证据、时间协议与 HTML 数据安全边界。"""
import json
import pathlib
import tempfile
import unittest

import dota_fight_commentary as commentary
import dota_fight_html as renderer


class CommentaryTests(unittest.TestCase):
    def test_full_window_keeps_late_events_without_aura_noise(self):
        data = {'heroes': [{'slug': 'axe', 'n': '斧王', 't': 1}]}
        events = [(1, {'type': 'DOTA_COMBATLOG_MODIFIER_ADD', 'tgt': 'npc_dota_hero_axe', 'inf': 'aura'})] * 1000
        events += [(49, {'type': 'DOTA_COMBATLOG_DEATH', 'tgt': 'npc_dota_hero_axe'})]
        pack = commentary.evidence(data, events, 0, 50, renderer.norm_npc, renderer.spell_cn)
        self.assertEqual(len(pack['events']), 1)
        self.assertEqual(pack['events'][0][:2], [49, '死亡'])

    def test_summon_damage_and_protection_lifecycle(self):
        data = {'heroes': [{'slug': 'warlock', 'n': '术士', 't': 0}, {'slug': 'axe', 'n': '斧王', 't': 1}]}
        events = [
            (1, {'type': 'DOTA_COMBATLOG_MODIFIER_ADD', 'tgt': 'npc_dota_hero_axe', 'inf': 'modifier_black_king_bar', 'mdur': 8}),
            (4, {'type': 'DOTA_COMBATLOG_DAMAGE', 'atk': 'npc_dota_warlock_golem', 'src': 'npc_dota_hero_warlock', 'tgt': 'npc_dota_hero_axe', 'val': 200}),
            (5, {'type': 'DOTA_COMBATLOG_MODIFIER_REMOVE', 'tgt': 'npc_dota_hero_axe', 'inf': 'modifier_black_king_bar'}),
        ]
        pack = commentary.evidence(data, events, 3, 10, renderer.norm_npc, renderer.spell_cn)
        self.assertEqual(pack['damage_3s'][0][1:], ['术士', '斧王', 200])
        self.assertEqual(pack['initial_protection'][0]['hero'], '斧王')
        self.assertEqual(pack['events'][0][1], '状态结束')

    def test_schema_rejects_absolute_or_nonfinite_clock(self):
        obj = {'title': '测试', 'cues': [{'t': 2, 'title': '关键', 'text': '动作'}],
               'conclusion': {k: '评价' for k in ('verdict', 'reason', 'focus', 'improvement')}}
        self.assertIsNotNone(commentary.parse(json.dumps(obj), 30))
        for invalid in (1500, -1, float('nan')):
            obj['cues'][0]['t'] = invalid
            self.assertIsNone(commentary.parse(json.dumps(obj), 30))

    def test_attach_does_not_allow_script_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            p = pathlib.Path(folder) / 'sample.html'
            p.write_text('<script>const DATA = {"meta":{}};</script>', encoding='utf8')
            renderer.attach_commentary(p, {'title': '</script><script>alert(1)</script>'})
            html = p.read_text(encoding='utf8')
            self.assertEqual(html.count('</script>'), 1)
            data, _ = json.JSONDecoder().raw_decode(html.split('const DATA = ', 1)[1])
            self.assertEqual(data['commentary']['title'], '</script><script>alert(1)</script>')

    def test_fine_entities_match_combat_names(self):
        self.assertEqual(renderer.hero_slug('CDOTA_Unit_Hero_Void_Spirit'), renderer.norm_npc('npc_dota_hero_void_spirit'))
        self.assertEqual(renderer.hero_slug('CDOTA_Unit_Hero_QueenOfPain'), renderer.norm_npc('npc_dota_hero_queenofpain'))


if __name__ == '__main__':
    unittest.main()
