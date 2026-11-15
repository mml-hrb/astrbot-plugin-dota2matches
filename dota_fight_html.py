#!/usr/bin/env python3
"""通用团战渲染管线：把「任一场录像 + 任一窗口」变成一份可交互 HTML。

用法（全部参数化，不为任何具体场景写特例）：

    python3 dota_fight_html.py --match 9080184871 --from 1240 --to 1275 --out fight.html

输入（服务器上由 Fine.java / Dump.class 产出的标准产物）：
    <work>/<match>/fine.csv      0.1 秒英雄状态（t,tick,pid,name,hp,maxHp,mp,maxMp,lvl,alive,x,y）
    <work>/<match>/combat.jsonl  战斗日志（ABILITY/DAMAGE/MODIFIER/DEATH/HEAL/ITEM）

⚠️ **t0 / t1 是哪条时间轴？** —— **战斗日志时间（combat）**，不是比赛时间。

``build`` 内部把 fine 的 t 用 ``t_combat = t_fine + OFF`` 平移到 combat 轴之后，
位置查表、combat 事件扫描、击杀统计**全部**在这条轴上做。所以窗口参数必须
也是 combat 轴。

插件侧拿到的 ``fight["start"] / ["end"]`` 是**比赛时间**，两者差一个常量：

    t_combat = t_match + offsets["combatlog_to_match"]

调用方（``main.py``）负责换算，见 :meth:`_build_fight_html` 里那段注释。
**弄错这一步的后果是「窗口整体偏移十几秒」**，页面能正常打开、英雄也在动，
只是画的是另一段时间 —— 这种错不会报错，只会让复盘张冠李戴。

关键设计
--------
1. **时间轴只有一条**。combat 的 t 与 fine 的 t 差一个常量 OFF，
   用「死亡时刻序列投票」自动标定，不写死、不靠猜（之前踩过用释放间隔反推 CD 的坑）。
2. **本体识别**。同一 playerID 会同时存在本体 + 幻象/克隆，幻象 hp=0 且 alive=0；
   只保留 alive==1 且 hp>0 的行作为本体。
3. **窗口由参数决定**。脚本不假设任何英雄、任何团战位置。
4. **零判断**。只输出「谁在什么时候做了什么、谁掉了多少血」，不下「该不该」。
5. **坐标系是实测的**。DOTA2 逻辑坐标 0–32768，但官方小地图底图只画
   [WORLD_MIN, WORLD_MAX] 这一块（实心区域），且 **y 轴要翻转**（世界向上、画布向下）。
   这两点用全场英雄轨迹热力图叠图验证过：按 8192–24576 且翻转，轨迹正好压在兵线上。
   之前「位置不对」就是少了 y 翻转 + 把 0–32768 当成了底图范围。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections import defaultdict

TICK_RATE = 30.0

# 官方小地图底图覆盖的世界坐标范围（正方形）。实测值，见模块 docstring。
WORLD_MIN = 8192.0
WORLD_MAX = 24576.0

HERE = os.path.dirname(os.path.abspath(__file__))
#: 底图与头像内联资源。**放在独立子目录**而不是插件根目录：
#: 624 KB 的图片/JSON 混在 .py 旁边会让 `ls` 很难读，也容易被误当成模块。
ASSETS = os.path.join(HERE, "fight_assets")


# ------------------------------------------------------------------ 读输入
def read_fine(path):
    """返回 (series, lvl_of)。

    series {slug: {t: (hp, maxhp, mp, x, y)}}，只含「本体活着」的行。
    lvl_of {slug: 等级}

    ⚠ 三个坑，都是实测踩出来的：

    1. 同一 playerID 会同时存在 本体 + 幻象 + 分身 + 尸体残留。
       幻象/分身 hp=0 或 alive=0，必须滤掉，否则血量会串。
       （本场 Bane 有 13088 条本体行、21157 条 hp=0/alive=0 行。）

    2. **等级不能取「最后一行」**。齐天大圣「七十二变」的分身与本体共用同一个
       playerID，分身固定 1 级 538 血，在 csv 里与本体逐帧交错写入。
       取最后一行会得到 1 级。正确做法：取「maxHp 最大的那条」——
       分身血量上限远低于本体，这个判据与英雄名无关，对任何分身/幻象都成立。

    3. 键必须转成 slug 并在此统一。旧代码在别处用 npc 原名查，导致等级全 1。
    """
    series = defaultdict(dict)
    lvl_of = {}
    best_hp = {}  # slug -> 该英雄见过的最大 maxHp，用于识别本体
    lvl_raw = {}  # slug -> (maxHp, lvl) 取 maxHp 最大者的等级
    for line in open(path, encoding="utf-8"):
        p = line.rstrip("\n").split(",")
        if len(p) < 12:
            continue
        try:
            t = float(p[0])
            name = p[3]
            hp = int(p[4])
            maxhp = int(p[5])
            mp = int(p[6])
            lvl = int(p[8])
            alive = int(p[9])
            x = float(p[10])
            y = float(p[11].strip())  # 末列可能带换行符
        except ValueError:
            continue
        slug = hero_slug(name)
        if alive != 1 or hp <= 0:
            continue  # 幻象 / 分身 / 尸体残留
        series[slug][round(t, 2)] = (hp, maxhp, mp, x, y)
        # 本体判据：maxHp 最大。分身/幻象的 maxHp 明显更小，不会被选中。
        if maxhp >= best_hp.get(slug, -1):
            best_hp[slug] = maxhp
            lvl_raw[slug] = lvl
    for slug, lv in lvl_raw.items():
        lvl_of[slug] = lv
    return series, lvl_of


def hero_deaths(combat, idx, off):
    """{slug: [死亡时刻(combat 轴)...]}，只取英雄本体。

    combat 里的 DEATH 同时包含小兵、野怪、守卫、召唤物，必须按玩家英雄过滤。
    另外同一秒内可能重复记录，去重。
    """
    out = defaultdict(set)
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_DEATH":
            continue
        tgt = norm_npc(e.get("tgt"))
        if tgt in idx:
            out[tgt].add(round(t, 2))
    return {k: sorted(v) for k, v in out.items()}


def read_combat(path):
    """读战斗日志，返回 [(t, entry)]，按 t 升序。"""
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append((float(e.get("t", 0)), e))
    out.sort(key=lambda x: x[0])
    return out


# ------------------------------------------------------------------ 时间标定
def calibrate(fine_path, combat):
    """用死亡序列投票求 OFF：t_combat = t_fine + OFF。

    为什么不用「第一条事件」对齐：两条时间轴起点不同，起点差没有意义。
    为什么不用「采样间隔变大」判死：本体行消失后下一帧可能有别的实例接上，
    间隔不一定变大，实测会给出 7 票的错误解（正确解有 36 票）。
    正解：直接看「这一帧这个英雄还有没有本体行」，消失即为死亡时刻。
    """
    alive_at = defaultdict(set)
    for line in open(fine_path, encoding="utf-8"):
        p = line.rstrip("\n").split(",")
        if len(p) < 12:
            continue
        try:
            t = round(float(p[0]), 1)
            hp = int(p[4])
            al = int(p[9])
        except ValueError:
            continue
        if al == 1 and hp > 0:
            alive_at[t].add(p[3])

    ts = sorted(alive_at)
    if not ts:
        return 0.0, 0
    appeared = set()
    for t in ts:
        appeared |= alive_at[t]

    fine_deaths = defaultdict(list)
    for i in range(1, len(ts)):
        prev, now = alive_at[ts[i - 1]], alive_at[ts[i]]
        for name in appeared:
            if name in prev and name not in now:
                fine_deaths[name].append(ts[i])

    combat_deaths = defaultdict(list)
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_DEATH":
            continue
        tgt = str(e.get("tgt") or "")
        if tgt.startswith("npc_dota_hero_"):
            combat_deaths[tgt[len("npc_dota_hero_") :]].append(round(t, 1))

    votes = defaultdict(int)
    for name, ds in fine_deaths.items():
        hero = hero_slug(name)
        for a in ds:
            for b in combat_deaths.get(hero, []):
                # t_combat = t_fine + OFF  =>  OFF = t_combat - t_fine
                votes[round(b - a, 1)] += 1
    if not votes:
        return 0.0, 0
    best, cnt = max(votes.items(), key=lambda kv: kv[1])
    # 用最优偏移校验一次：至少要有 3 条吻合才算标定成功
    return best, cnt


def hero_slug(name):
    n = name.replace("CDOTA_Unit_Hero_", "")
    aliases = {"Void_Spirit": "void_spirit", "QueenOfPain": "queenofpain"}
    if n in aliases:
        return aliases[n]
    special = {
        "FacelessVoid": "faceless_void",
        "Skywrath_Mage": "skywrath_mage",
        "Earthshaker": "earthshaker",
        "Windrunner": "windrunner",
        "DoomBringer": "doom_bringer",
        "DrowRanger": "drow_ranger",
        "EmberSpirit": "ember_spirit",
        "StormSpirit": "storm_spirit",
        "EarthSpirit": "earth_spirit",
        "VoidSpirit": "void_spirit",
        "Wisp": "wisp",
        "Obsidian_Destroyer": "obsidian_destroyer",
        "PhantomAssassin": "phantom_assassin",
        "VengefulSpirit": "vengefulspirit",
        "Ogre_Magi": "ogre_magi",
        "MonkeyKing": "monkey_king",
        "Nyx_Assassin": "nyx_assassin",
        "ChaosKnight": "chaos_knight",
        "Magnataur": "magnataur",
        "Treant": "treant",
        "Furion": "furion",
        "Natures_Prophet": "natures_prophet",
        "Lone_Druid": "lone_druid",
        "Brewmaster": "brewmaster",
        "Shadow_Demon": "shadow_demon",
        "Shadow_Shaman": "shadow_shaman",
        "Witch_Doctor": "witch_doctor",
        "Winter_Wyvern": "winter_wyvern",
        "Centaur": "centaur",
        "Timbersaw": "timbersaw",
        "Bounty_Hunter": "bounty_hunter",
        "AntiMage": "anti_mage",
        "Templar_Assassin": "templar_assassin",
        "Troll_Warlord": "troll_warlord",
        "Legion_Commander": "legion_commander",
        "Death_Prophet": "death_prophet",
        "Night_Stalker": "night_stalker",
        "Sand_King": "sand_king",
        "Spirit_Breaker": "spirit_breaker",
        "LifeStealer": "lifestealer",
        "CrystalMaiden": "crystal_maiden",
        "AncientApparition": "ancient_apparition",
        "DarkWillow": "dark_willow",
        "Primal_Beast": "primal_beast",
        "Arc_Warden": "arc_warden",
        "Elder_Titan": "elder_titan",
        "Rattletrap": "rattletrap",
        "Daemon": "daemon",
        "Morphling": "morphling",
        "PhantomLancer": "phantom_lancer",
        "Silencer": "silencer",
    }
    if n in special:
        return special[n]
    # CamelCase -> snake（连续大写如 "CK" 不会被拆成 c_k）
    out = []
    for i, ch in enumerate(n):
        if ch.isupper() and i > 0 and not n[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def norm_npc(s):
    """combat 里的实体名 -> 统一 slug。不是英雄返回 None。"""
    s = str(s or "")
    if s.startswith("npc_dota_hero_"):
        return s[len("npc_dota_hero_") :]
    return None


CN = {
    # 常见出场英雄（按 CN 社区习惯叫法）
    "invoker": "祈求者",
    "bane": "祸乱之源",
    "faceless_void": "虚空假面",
    "slark": "斯拉克",
    "sniper": "矮人狙击手",
    "windrunner": "风行者",
    "rubick": "拉比克",
    "disruptor": "干扰者",
    "earthshaker": "撼地者",
    "skywrath_mage": "天怒法师",
    "juggernaut": "主宰",
    "lich": "巫妖",
    "lion": "莱恩",
    "lina": "莉娜",
    "pudge": "帕吉",
    "zuus": "宙斯",
    "crystal_maiden": "水晶室女",
    "nevermore": "影魔",
    "tidehunter": "潮汐猎人",
    "jakiro": "杰奇洛",
    "monkey_king": "齐天大圣",
    "obsidian_destroyer": "殁境神蚀者",
    "ogre_magi": "食人魔魔法师",
    "phantom_assassin": "幻影刺客",
    "vengefulspirit": "复仇之魂",
    "axe": "斧王",
    "puck": "帕克",
    "storm_spirit": "风暴之灵",
    "ember_spirit": "灰烬之灵",
    "earth_spirit": "大地之灵",
    "void_spirit": "虚无之灵",
    "morphling": "变体精灵",
    "phantom_lancer": "幻影长矛手",
    "anti_mage": "敌法师",
    "drow_ranger": "卓尔游侠",
    "mirana": "米拉娜",
    "moon_fang": "月之女祭司",
    "templar_assassin": "圣堂刺客",
    "tusk": "巨牙海民",
    "ursa": "乌尔萨",
    "wisp": "精灵",
    "io": "精灵",
    "spectre": "幽鬼",
    "medusa": "美杜莎",
    "troll_warlord": "巨魔战将",
    "sven": "斯温",
    "tiny": "小小",
    "bristleback": "钢背兽",
    "legion_commander": "军团指挥官",
    "abaddon": "亚巴顿",
    "death_prophet": "死亡先知",
    "doom_bringer": "末日使者",
    "night_stalker": "夜魔",
    "chaos_knight": "混沌骑士",
    "sand_king": "沙王",
    "nyx_assassin": "司夜刺客",
    "slardar": "斯拉达",
    "spirit_breaker": "裂魂人",
    "magnataur": "马格纳斯",
    "lifestealer": "噬魂鬼",
    "weaver": "编织者",
    "clinkz": "克林克兹",
    "broodmother": "育母蜘蛛",
    "venomancer": "剧毒术士",
    "enchantress": "魅惑魔女",
    "chen": "陈",
    "furion": "自然先知",
    "natures_prophet": "自然先知",
    "treant": "树精卫士",
    "lone_druid": "德鲁伊",
    "brewmaster": "酒仙",
    "huskar": "哈斯卡",
    "dazzle": "戴泽",
    "oracle": "神谕者",
    "shadow_demon": "暗影恶魔",
    "shadow_shaman": "暗影萨满",
    "witch_doctor": "巫医",
    "necrophos": "死灵法师",
    "warlock": "术士",
    "winter_wyvern": "寒冬飞龙",
    "phoenix": "凤凰",
    "kunkka": "昆卡",
    "gyrocopter": "矮人直升机",
    "alchemist": "炼金术士",
    "rattletrap": "发条技师",
    "clockwerk": "发条技师",
    "batrider": "蝙蝠骑士",
    "beastmaster": "兽王",
    "elder_titan": "上古巨神",
    "centaur": "半人马战行者",
    "shredder": "伐木机",
    "timbersaw": "伐木机",
    "bounty_hunter": "赏金猎人",
    "riki": "力丸",
    "silencer": "沉默术士",
    "undying": "不朽尸王",
    "visage": "维萨吉",
    "arc_warden": "天穹守望者",
    "pangolier": "石鳞剑士",
    "dark_willow": "邪影芳灵",
    "grimstroke": "天涯墨客",
    "mars": "玛尔斯",
    "snapfire": "电炎绝手",
    "void_spirit2": "虚无之灵",
    "hoodwink": "森海飞霞",
    "dawnbreaker": "破晓辰星",
    "marci": "玛西",
    "primal_beast": "獸",
    "muerta": "琼英碧灵",
    "ringmaster": "守夜牧师",
    "kez": "凯兹",
}


def cn_of(slug):
    if slug in CN:
        return CN[slug]
    # 兜底：把 snake_case 变成首字母大写，至少不是一串下划线
    return slug.replace("_", " ").title()


# ------------------------------------------------------------------ 技能名
SPELL_CN = {
    "invoker_tornado": "龙卷风",
    "invoker_alacrity": "灵动迅捷",
    "invoker_forge_spirit": "召唤锻造精灵",
    "invoker_ghost_walk": "幽灵漫步",
    "invoker_cold_snap": "冰霜新星",
    "invoker_chaos_meteor": "混沌陨石",
    "invoker_sun_strike": "阳炎冲击",
    "invoker_emp": "电磁脉冲",
    "invoker_deafening_blast": "超震声波",
    "invoker_ice_wall": "冰墙",
    "bane_fiends_grip": "祸乱大招",
    "bane_brain_sap": "蚀脑",
    "bane_enfeeble": "虚弱",
    "bane_nightmare": "噩梦",
    "faceless_void_chronosphere": "时间结界",
    "faceless_void_time_walk": "时间漫游",
    "faceless_void_time_dilation": "时间膨胀",
    "slark_pounce": "突袭",
    "slark_dark_pact": "黑暗契约",
    "slark_shadow_dance": "暗影之舞",
    "slark_essence_shift": "精华转移",
    "sniper_assassinate": "暗杀",
    "sniper_shrapnel": "散弹",
    "windrunner_shackleshot": "束缚击",
    "windrunner_powershot": "强力击",
    "windrunner_windrun": "风行",
    "windrunner_focusfire": "集中火力",
    "rubick_telekinesis": "心灵遥控",
    "rubick_fade_bolt": "衰退之光",
    "rubick_spell_steal": "技能窃取",
    "disruptor_thunder_strike": "雷霆之击",
    "disruptor_kinetic_field": "动能立场",
    "disruptor_static_storm": "静态风暴",
    "disruptor_glimpse": "瞥视",
    "earthshaker_fissure": "沟壑",
    "earthshaker_enchant_totem": "强化图腾",
    "earthshaker_echo_slam": "回音击",
    "earthshaker_aftershock": "余震",
    "skywrath_mage_arcane_bolt": "秘法闪电",
    "skywrath_mage_concussive_shot": "震荡射击",
    "skywrath_mage_ancient_seal": "上古封印",
    "skywrath_mage_mystic_flare": "神秘之耀",
    # 杰奇洛
    "jakiro_dual_breath": "双生龙息",
    "jakiro_ice_path": "冰封路径",
    "jakiro_macropyre": "地狱烈焰",
    "jakiro_liquid_fire": "液态火焰",
    "jakiro_liquid_ice": "液态冰霜",
    # 主宰
    "juggernaut_blade_fury": "剑刃风暴",
    "juggernaut_healing_ward": "治疗守卫",
    "juggernaut_omni_slash": "无敌斩",
    "juggernaut_blade_dance": "剑舞",
    "juggernaut_swift_slash": "迅疾斩",
    # 巫妖
    "lich_frost_nova": "霜冻新星",
    "lich_frost_shield": "霜冻护甲",
    "lich_sinister_gaze": "邪恶凝视",
    "lich_chain_frost": "连环霜冻",
    "lich_ice_spire": "寒冰尖塔",
    # 齐天大圣
    "monkey_king_boundless_strike": "无影拳",
    "monkey_king_tree_dance": "上树",
    "monkey_king_primal_spring": "原始弹跳",
    "monkey_king_wukongs_command": "齐天法阵",
    "monkey_king_mischief": "七十二变",
    # 殁境神蚀者
    "obsidian_destroyer_arcane_orb": "奥术光球",
    "obsidian_destroyer_astral_imprisonment": "星体禁锢",
    "obsidian_destroyer_objurgation": "侵蚀",
    "obsidian_destroyer_sanity_eclipse": "理智蚀灭",
    # 食人魔魔法师
    "ogre_magi_fireblast": "火焰爆轰",
    "ogre_magi_ignite": "引燃",
    "ogre_magi_bloodlust": "嗜血术",
    "ogre_magi_unrefined_fireblast": "未精炼火焰爆轰",
    "ogre_magi_smash": "重击",
    "ogre_magi_multicast": "多重施法",
    # 幻影刺客
    "phantom_assassin_stifling_dagger": "窒息之刃",
    "phantom_assassin_phantom_strike": "幻影突袭",
    "phantom_assassin_blur": "模糊",
    "phantom_assassin_coup_de_grace": "致命一击",
    # 帕吉
    "pudge_meat_hook": "肉钩",
    "pudge_rot": "腐烂",
    "pudge_flesh_heap": "血肉堆叠",
    "pudge_dismember": "肢解",
    # 复仇之魂
    "vengefulspirit_magic_missile": "魔法箭",
    "vengefulspirit_wave_of_terror": "恐怖波动",
    "vengefulspirit_nether_swap": "移形换位",
    # 拉比克补充
    "rubick_telekinesis_land": "心灵遥控落地",
    # 矮人狙击手补充
    "sniper_take_aim": "瞄准",
}

ITEM_CN = {
    # 主动道具
    "item_black_king_bar": "黑皇杖",
    "item_orchid": "紫怨",
    "item_sheepstick": "羊刀",
    "item_blink": "跳刀",
    "item_cyclone": "吹风",
    "item_wind_waker": "风之杖",
    "item_ghost": "幽魂权杖",
    "item_ultimate_scepter": "神杖",
    "item_refresher": "刷新球",
    "item_lotus_orb": "莲花",
    "item_glimmer_cape": "微光披风",
    "item_guardian_greaves": "卫士胫甲",
    "item_rod_of_atos": "阿托斯",
    "item_spirit_vessel": "灵魂之瓮",
    "item_dust": "显影之尘",
    "item_smoke_of_deceit": "诡计之雾",
    "item_invis_sword": "隐刀",
    "item_silver_edge": "白银之锋",
    "item_magic_wand": "魔杖",
    "item_mjollnir": "雷神之锤",
    "item_arcane_boots": "奥术鞋",
    "item_tpscroll": "回城卷轴",
    "item_ward_dispenser": "眼位器",
    "item_madstone_bundle": "疯狂之石包",
    "item_medallion_of_courage": "勇气勋章",
    "item_solar_crest": "烈日勋章",
    "item_nullifier": "否决",
    "item_bloodthorn": "血棘",
    "item_heavens_halberd": "天堂之戟",
    "item_shivas_guard": "希瓦的守护",
    "item_assault": "强袭胸甲",
    "item_crimson_guard": "赤红甲",
    "item_pipe": "笛子",
    "item_mekansm": "梅肯",
    "item_hand_of_midas": "点金手",
    "item_bfury": "狂战斧",
    "item_desolator": "黯灭",
    "item_greater_crit": "大炮",
    "item_force_staff": "推推杖",
    "item_ethereal_blade": "虚灵刀",
    "item_dagon": "达贡",
    "item_aeon_disk": "永恒之盘",
    "item_hurricane_pike": "飓风长戟",
    "item_diffusal_blade": "散失",
    "item_skadi": "冰眼",
    "item_heart": "龙心",
    "item_satanic": "撒旦",
    "item_mask_of_madness": "疯脸",
    # 常见被动/杂项
    "item_ring_of_basilius": "圣殿指环",
    "item_boots_of_elves": "敏捷之靴",
    "item_ring_of_health": "治疗指环",
    "item_void_stone": "虚无宝石",
    "item_stout_shield": "圆盾",
    "item_quelling_blade": "补刀斧",
}

# 引擎里有些「技能」其实是地图机关/天赋触发，翻成可读说法
MISC_CN = {
    "ability_capture": "占领前哨",
    "ability_lamp_use": "点亮灯笼",
    "ability_pluck_famango": "摘芒果",
}


# 引擎 modifier 名 -> 中文。这些是「挂在人身上的状态」，不是施法本身。
MOD_CN = {
    "modifier_item_spirit_vessel_damage": "灵魂之瓮",
    "modifier_windrunner_shackle_shot": "束缚击",
    "modifier_windrunner_shackleshot": "束缚击",
    "modifier_bane_fiends_grip": "祸乱大招",
    "modifier_invoker_tornado": "龙卷风",
    "modifier_slark_pounce_leash": "突袭束缚",
    "modifier_disruptor_static_storm": "静态风暴",
    "modifier_disruptor_kinetic_field": "动能立场",
    "modifier_earthshaker_fissure": "沟壑",
    "modifier_faceless_void_chronosphere": "时间结界",
    "modifier_skywrath_mage_ancient_seal": "上古封印",
    "modifier_orchid_malevolence_debuff": "紫怨沉默",
    "modifier_black_king_bar_immune": "黑皇杖",
    "modifier_slark_saltwater_shiv_effect": "咸水刀",
    "modifier_slark_essence_shift_debuff_counter": "精华转移",
    "modifier_bane_enfeeble_effect": "虚弱",
}


# 兜底映射：技能名前缀 -> 英雄 slug（用于「英雄 · 技能名」这种可读形式）
_HERO_PREFIX = {
    "jakiro": "jakiro",
    "juggernaut": "juggernaut",
    "lich": "lich",
    "monkey_king": "monkey_king",
    "obsidian_destroyer": "obsidian_destroyer",
    "ogre_magi": "ogre_magi",
    "phantom_assassin": "phantom_assassin",
    "pudge": "pudge",
    "vengefulspirit": "vengefulspirit",
    "invoker": "invoker",
    "bane": "bane",
    "faceless_void": "faceless_void",
    "slark": "slark",
    "sniper": "sniper",
    "windrunner": "windrunner",
    "rubick": "rubick",
    "disruptor": "disruptor",
    "earthshaker": "earthshaker",
    "skywrath_mage": "skywrath_mage",
}


# ------------------------------------------------------------------ 名字翻译
# 设计：**不追求穷举字典**。引擎里的技能/modifier 名有 400+ 个且随版本增长，
# 手写映射必然漏（实测两场就有 236/422 没覆盖）。
# 改成「结构化泛化」：任何名字都能拆成 [英雄前缀][技能核心][效果后缀]，
# 三段各查一张小表，拼出可读中文。表里没有的部分才退回英文，
# 但至少会带英雄中文名，读者能定位是谁的技能。
#
# 例：
#   modifier_jakiro_dual_breath_slow  -> 杰奇洛 · 双生龙息·减速
#   modifier_lich_frostnova_slow      -> 巫妖 · 霜冻新星·减速
#   faceless_void_time_walk_reverse   -> 虚空假面 · 时间漫游·回退

# 技能核心名表：key 是「剥掉英雄前缀与效果后缀」之后的技能短名。
# 这张表是**按两场真实录像里实际出现过的实体**补的，不是凭记忆抄全家桶 ——
# 抄全家桶既抄不完也会写错。新增英雄时，build_fight 会把未识别的组合打到 stderr，
# 照那个清单往上补即可。
SKILL_CORE_CN = {
    # 祸乱之源
    "brain_sap": "蚀脑",
    "enfeeble": "虚弱",
    "fiends_grip": "祸乱大招",
    "fiends_grip_cast": "祸乱大招·施法",
    "nightmare": "噩梦",
    "ichor_of_nyctasha": "奈克塔莎之血",
    "melting_strike": "熔解打击",
    "invoke_bonuses": "祈唤·加成",
    "time_lock": "时间锁定",
    # 干扰者
    "glimpse": "瞥视",
    "kinetic_field": "动能立场",
    "static_storm": "静态风暴",
    "thunder_strike": "雷霆之击",
    # 撼地者
    "echo_slam": "回音击",
    "enchant_totem": "强化图腾",
    "enchant_totem_body": "强化图腾·本体",
    "fissure": "沟壑",
    # 虚空假面
    "chronosphere": "时间结界",
    "time_dilation": "时间膨胀",
    "time_dilation_distortion_aura_applicator": "时间膨胀·光环",
    "time_walk": "时间漫游",
    "time_walk_reverse": "时间漫游·回退",
    "time_walk_shardbuff": "时间漫游·魔晶",
    "timelock": "时间锁定",
    "timelock_basic_proc_marker": "时间锁定·普攻",
    "timelock_timewalk_proc_marker": "时间锁定·漫游",
    "arcana_damage": "至宝伤害",
    "arcana_kill": "至宝击杀",
    # 祈求者
    "alacrity": "灵动迅捷",
    "chaos_meteor": "混沌陨石",
    "chaos_meteor_land": "混沌陨石·落地",
    "cold_snap": "冰霜新星",
    "deafening_blast": "超震声波",
    "deafening_blast_disarm": "超震声波·缴械",
    "deafening_blast_knockback": "超震声波·击退",
    "emp": "电磁脉冲",
    "exort": "火（Exort）",
    "exort_instance": "火（Exort）",
    "forge_spirit": "召唤锻造精灵",
    "ghost_walk": "幽灵漫步",
    "ghost_walk_enemy": "幽灵漫步·减速",
    "ghost_walk_self": "幽灵漫步",
    "ice_wall": "冰墙",
    "ice_wall_slow": "冰墙·减速",
    "invoke": "祈唤（Invoke）",
    "quas": "冰（Quas）",
    "quas_instance": "冰（Quas）",
    "sun_strike": "阳炎冲击",
    "sun_strike_cataclysm": "阳炎冲击·灾变",
    "tornado": "龙卷风",
    "wex": "雷（Wex）",
    "wex_instance": "雷（Wex）",
    # 杰奇洛
    "dual_breath": "双生龙息",
    "ice_path": "冰封路径",
    "liquid_fire": "液态火焰",
    "liquid_ice": "液态冰霜",
    "macropyre": "地狱烈焰",
    "macropyre_ice_edge": "地狱烈焰·冰缘",
    "double_trouble": "双重麻烦",
    # 主宰
    "blade_fury": "剑刃风暴",
    "bladeform": "剑刃风暴",
    "healing_ward": "治疗守卫",
    "healing_ward_heal": "治疗守卫·治疗",
    "omni_slash": "无敌斩",
    "omnislash": "无敌斩",
    "swift_slash": "迅疾斩",
    # 巫妖
    "chain_frost": "连环霜冻",
    "chainfrost": "连环霜冻",
    "chain_frost_ondeath": "连环霜冻·死亡触发",
    "chainfrost_frostbound": "连环霜冻·冰缚",
    "frost_nova": "霜冻新星",
    "frostnova": "霜冻新星",
    "frost_shield": "霜冻护甲",
    "ice_spire": "寒冰尖塔",
    "sinister_gaze": "邪恶凝视",
    "death_charge": "死亡充能",
    # 齐天大圣
    "arc_to_ground": "落地冲击",
    "bounce_perch": "树上弹跳",
    "boundless_strike": "无影拳",
    "boundless_strike_crit": "无影拳·暴击",
    "boundless_strike_shard_movement": "无影拳·魔晶位移",
    "fur_army_bonus": "猴兵加成",
    "mischief": "七十二变",
    "primal_spring": "原始弹跳",
    "primal_spring_early": "原始弹跳·提前",
    "quadruple_tap": "四连击",
    "quadruple_tap_bonuses": "四连击·加成",
    "spring": "弹跳",
    "tree_dance": "上树",
    "untransform": "解除变形",
    "wukongs_command": "齐天法阵",
    # 殁境神蚀者
    "astral_imprisonment": "星体禁锢",
    "equilibrium_barrier": "平衡屏障",
    "equilibrium_debuff": "平衡减益",
    "objurgation": "侵蚀",
    "objurgation_cd": "侵蚀·冷却",
    "sanity_eclipse": "理智蚀灭",
    # 食人魔魔法师
    "bloodlust": "嗜血术",
    "fireblast": "火焰爆轰",
    "fireblast_multicast": "火焰爆轰·多重施法",
    "ignite": "引燃",
    "ignite_multicast": "引燃·多重施法",
    "smash": "重击",
    "unrefined_fireblast": "未精炼火焰爆轰",
    # 幻影刺客
    "blur": "模糊",
    "blur_active": "模糊·主动",
    "mark_of_death": "死亡印记",
    "phantom_strike": "幻影突袭",
    "stifling_dagger": "窒息之刃",
    "stiflingdagger": "窒息之刃",
    # 帕吉
    "dismember": "肢解",
    "flesh_heap": "血肉堆叠",
    "flesh_heap_block": "血肉堆叠·格挡",
    "innate_graft_flesh": "生肉嫁接",
    "meat_hook": "肉钩",
    "meat_hook_reveal": "肉钩·显形",
    "rot": "腐烂",
    # 拉比克
    "fade_bolt": "衰退之光",
    "spell_steal": "技能窃取",
    "telekinesis": "心灵遥控",
    "telekinesis_land": "心灵遥控·落地",
    "curiosity_from_heroes_tracker": "好奇·计数",
    # 天怒法师
    "ancient_seal": "上古封印",
    "arcane_bolt": "秘法闪电",
    "arcane_bolt_lifesteal": "秘法闪电·吸血",
    "concussive_shot": "震荡射击",
    "mystic_flare": "神秘之耀",
    "shield_barrier": "护盾屏障",
    # 斯拉克
    "dark_pact": "黑暗契约",
    "depth_shroud": "深海帷幕",
    "essence_shift": "精华转移",
    "essence_shift_debuff": "精华转移·减益",
    "pounce": "突袭",
    "saltwater_shiv": "咸水刀",
    "shadow_dance": "暗影之舞",
    "shadow_dance_passive_regen": "暗影之舞·回复",
    # 矮人狙击手
    "assassinate": "暗杀",
    "assassinate_trigger": "暗杀·触发",
    "concussive_grenade": "震荡手雷",
    "headshot": "爆头",
    "shrapnel": "散弹",
    "take_aim": "瞄准",
    "take_aim_bonus": "瞄准·加成",
    # 复仇之魂
    "command": "命令光环",
    "command_aura": "命令光环",
    "hybrid_xp_earn": "混血经验",
    "magic_missile": "魔法箭",
    "nether_swap": "移形换位",
    "nether_swap_damage_reduction": "移形换位·减伤",
    "spellslinger_restore": "咒术师·回复",  # 复仇之魂魔晶被动
    "retribution_tracker": "报应·计数",
    "wave_of_terror": "恐怖波动",
    # 风行者
    "focusfire": "集中火力",
    "gale_force": "疾风之力",
    "powershot": "强力击",
    "shackle_shot": "束缚击",
    "shackleshot": "束缚击",
    "tailwind": "顺风",
    "windrun": "风行",
}

# 物品名（按实际出现过 + 常见主动道具）。
# modifier_item_xxx_yyy 会先剥出 item_xxx 再查这张表，所以表里只写物品本体。
ITEM_CORE_CN = {
    "power_treads": "动力鞋",
    "phase_boots": "相位鞋",
    "arcane_boots": "秘法鞋",
    "tranquil_boots": "静谧之鞋",
    "guardian_greaves": "卫士胫甲",
    "blade_mail": "刃甲",
    "manta": "分身斧",
    "gungir": "群鸦之戒",
    "swift_blink": "迅捷闪烁",
    "blink": "跳刀",
    "bloodthorn": "血棘",
    "orchid": "紫怨",
    "devastator": "毁灭之刃",
    "gungnir": "群鸦之戒",
    "crellas_crozier": "克瑞拉魂匣",
    "diffusal_blade": "散失",
    "dustofappearance": "显影之尘",
    "ethereal_blade": "虚灵刀",
    "forcestaff": "推推杖",
    "force_staff": "推推杖",
    "giant_maul": "巨人之槌",
    "glimmer_cape": "微光披风",
    "hydras_breath": "九头蛇之息",
    "lotus_orb": "莲花",
    "nullifier": "否决",
    "ring_of_basilius": "圣殿指环",
    "stormcrafter": "唤雷者",
    "ultimate_scepter": "神杖",
    "witch_blade": "巫术之刃",
    "orb_of_frost": "霜冻之球",
    "searing_signet": "灼烧印记",
    "black_king_bar": "黑皇杖",
    "sheepstick": "羊刀",
    "cyclone": "吹风",
    "wind_waker": "风之杖",
    "ghost": "幽魂权杖",
    "refresher": "刷新球",
    "rod_of_atos": "阿托斯",
    "spirit_vessel": "灵魂之瓮",
    "invis_sword": "隐刀",
    "silver_edge": "白银之锋",
    "magic_wand": "魔杖",
    "mjollnir": "雷神之锤",
    "heavens_halberd": "天堂之戟",
    "shivas_guard": "希瓦的守护",
    "assault": "强袭胸甲",
    "crimson_guard": "赤红甲",
    "pipe": "笛子",
    "mekansm": "梅肯",
    "hand_of_midas": "点金手",
    "bfury": "狂战斧",
    "desolator": "黯灭",
    "greater_crit": "大炮",
    "aeon_disk": "永恒之盘",
    "hurricane_pike": "飓风长戟",
    "skadi": "冰眼",
    "heart": "龙心",
    "satanic": "撒旦",
    "mask_of_madness": "疯脸",
    "tango": "树之祭祀",
    "clarity": "净化药水",
    "flask": "治疗药膏",
    "faerie_fire": "仙灵之火",
    "enchanted_mango": "魔法芒果",
    "magic_stick": "魔杖",
    "urn_of_shadows": "灵魂之戒",
    "moon_shard": "月之碎片",
    "gem": "宝石",
    "ward_observer": "观察守卫",
    "ward_sentry": "侦查守卫",
    "tpscroll": "回城卷轴",
    "madstone_bundle": "疯狂之石包",
    "essence_ring": "精华之戒",
    "fallen_sky": "坠落之天",
    "famango": "法芒果",
    "great_famango": "大法芒果",
    "greater_famango": "特大法芒果",
    "blood_grenade": "血榴弹",
    "roshans_banner": "肉山旗帜",
    "stonefeather_satchel": "石羽挎包",
    "shadow_amulet": "暗影护符",
    "mana_draught": "法力药剂",
    "abyssal_blade": "深渊之刃",
    "idol_of_screeauk": "尖啸神像",
    "polliwog_charm": "蝌蚪护符",
    "tango_single": "树之祭祀",
    "sentry_wards": "侦查守卫",
    "observer_wards": "观察守卫",
    "invisibility_edge": "隐刀",
    "gunpowder_gauntlets": "火药护手",
    "conjurers_catalyst": "咒术催化剂",
}

# 效果后缀 -> 中文。按长度降序匹配，避免 _slow 抢在 _attack_slow 前面。
SUFFIX_CN = [
    ("_attack_slow", "攻击减速"),
    ("_move_slow", "移速降低"),
    ("_magic_amplification", "法术加深"),
    ("_damage_amplification", "受伤加深"),
    ("_invulnerable", "无敌"),
    ("_invulnerability", "无敌"),
    ("_projectile", "弹道"),
    ("_selfbuff", "自身增益"),
    ("_thinker", ""),
    ("_counter", ""),
    ("_router", ""),
    ("_distortion_aura", "扭曲光环"),
    ("_aura", "光环"),
    ("_debuff_counter", "减益层数"),
    ("_counter", "层数"),
    ("_linger", "残留"),
    ("_freeze", "冻结"),
    ("_burn", "灼烧"),
    ("_reflect", "反伤"),
    ("_movespeed", "移速"),
    ("_active", "主动"),
    ("_ethereal", "虚灵"),
    ("_fade", "渐隐"),
    ("_poison", "中毒"),
    ("_mute", "禁言"),
    ("_state", "状态"),
    ("_noheal", "禁疗"),
    ("_instance", ""),
    ("_weakening", "削弱"),
    ("_crit", "暴击"),
    ("_basic_proc_marker", ""),
    ("_proc_marker", ""),
    ("_marker", ""),
    ("_windwalk", "疾风步"),
    ("_static", "静电场"),
    ("_bonuses", "加成"),
    ("_slow", "减速"),
    ("_stun", "眩晕"),
    ("_root", "缠绕"),
    ("_silence", "沉默"),
    ("_prison", "禁锢"),
    ("_leash", "束缚"),
    ("_pull", "拖拉"),
    ("_debuff", "减益"),
    ("_buff", "增益"),
    ("_effect", ""),
    ("_damage", "伤害"),
    ("_immunity", "免疫"),
    ("_transform", "变形"),
    ("_bounce", "弹跳"),
]

# 通用状态名（不带英雄前缀的那些）
GENERIC_CN = {
    # 注意：写在这里的整名必须在**剥后缀之前**命中（见 spell_cn 第 3a 步）。
    # 所以遇到「XX_restore / XX_buff」这种后缀本身就是词根一部分的名字，
    # 一定要加到这张表里 —— 否则 _restore 会被当后缀剥掉，查不到。
    "spellslinger_restore": "咒术师·回复",  # 复仇之魂魔晶被动
    "stunned": "眩晕",
    "bashed": "击晕",
    "silence": "沉默",
    "silenced": "沉默",
    "rooted": "缠绕",
    "hexed": "变形",
    "fear": "恐惧",
    "taunted": "嘲讽",
    "broken": "破坏",
    "slowed": "减速",
    "frozen": "冰冻",
    "invisible": "隐身",
    "invulnerable": "无敌",
    "illusion": "幻象",
    "teleporting": "传送中",
    "disarmed": "缴械",
    "muted": "禁言",
    "berserk": "狂暴",
    "fountain_invulnerability": "泉水无敌",
    "fountain_aura_buff": "泉水回复",
    "tower_aura_bonus": "防御塔光环",
    "path_movespeed": "路径加速",
    "creep_bonus": "小兵增益",
    "phase_boots_active": "相位",
    "guardian_greaves_aura": "卫士光环",
    "ghost_state": "幽魂状态",
    "knockback": "击退",
}

# 英雄别名：引擎里英雄前缀偶尔和 slug 不一致
HERO_PREFIX_ALIAS = {
    "nevermore": "shadow_fiend",
    "zuus": "zuus",
    "furion": "natures_prophet",
    "windrunner": "windrunner",
    "magnataur": "magnataur",
    "necrolyte": "necrolyte",
    "shredder": "shredder",
    "rattletrap": "rattletrap",
    "obsidian_destroyer": "obsidian_destroyer",
    "life_stealer": "life_stealer",
    "doom_bringer": "doom_bringer",
    "skeleton_king": "skeleton_king",
    "queenofpain": "queenofpain",
    "vengefulspirit": "vengefulspirit",
    "antimage": "antimage",
}

# 实体名里出现频率极高的「非英雄前缀」，要单独认，否则会被当成英雄名
NONHERO_PREFIX = {
    "courier": "信使",
    "tower": "防御塔",
    "fountain": "泉水",
    "creep": "小兵",
    "roshan": "肉山",
    "neutral": "野怪",
    "ancient_rock_golem": "远古岩石傀儡",
    "forged_spirit": "锻造精灵",
}

_HERO_KEYS = sorted(
    set(list(CN) + list(HERO_PREFIX_ALIAS) + list(_HERO_PREFIX)), key=len, reverse=True
)


def _split_suffix(core):
    """剥掉末尾的效果后缀，返回 (核心, 效果中文)。"""
    for suf, cn in SUFFIX_CN:
        if core.endswith(suf):
            return core[: -len(suf)], cn
    return core, ""


def _hero_of(core):
    """从 core 开头识别英雄前缀。返回 (slug, 剩余)。"""
    for pre in _HERO_KEYS:
        if core == pre:
            return pre, ""
        if core.startswith(pre + "_"):
            return pre, core[len(pre) + 1 :]
    return None, core


def _words_cn(rest):
    """剩下的英文单词：优先查技能表，查不到就保留原词（不硬翻）。"""
    if not rest:
        return ""
    if rest in SKILL_CORE_CN:
        return SKILL_CORE_CN[rest]
    if rest in SPELL_CN:
        return SPELL_CN[rest]
    # 多词技能名：把空格/下划线拼接后逐段前缀试（melting strike -> melting_strike）
    norm = rest.replace(" ", "_")
    if norm in SKILL_CORE_CN:
        return SKILL_CORE_CN[norm]
    # 技能名本身带下划线，且后面还挂了效果词时（coup_de_grace_crit），
    # 逐个剥掉末尾一段再查，能命中 SPELL_CN / SKILL_CORE_CN 里更长的键。
    # 用「最长可命中」原则，避免把 coup_de_grace 误切成 coup。
    segs = norm.split("_")
    for cut in range(len(segs) - 1, 0, -1):
        cand = "_".join(segs[:cut])
        if cand in SPELL_CN:
            return SPELL_CN[cand]
        if cand in SKILL_CORE_CN:
            return SKILL_CORE_CN[cand]
    # 技能表里常用下划线变体，逐个片段试
    for pre in _HERO_KEYS:
        if rest.startswith(pre + "_"):
            tail = rest[len(pre) + 1 :]
            if tail in SKILL_CORE_CN:
                return SKILL_CORE_CN[tail]
            if tail in SPELL_CN:
                return SPELL_CN[tail]
    return rest.replace("_", " ")


try:
    with open(os.path.join(HERE, "dota_zh_data.json"), encoding="utf-8") as _zh_file:
        _LOCAL_ABILITIES = json.load(_zh_file).get("abilities", {})
except (OSError, ValueError):
    _LOCAL_ABILITIES = {}


def spell_cn(inf):
    """引擎实体名 -> 可读中文。永远返回非空字符串。"""
    s = str(inf or "")
    if not s:
        return ""

    # 1) 精确命中几张手工表（最准）
    for tbl in (SPELL_CN, ITEM_CN, MOD_CN, MISC_CN):
        if s in tbl:
            return tbl[s]

    if s in _LOCAL_ABILITIES:
        return _LOCAL_ABILITIES[s]

    # 2) 物品
    if s.startswith("item_"):
        core = s[5:]
        if core in ITEM_CN:
            return ITEM_CN["item_" + core]
        if core in ITEM_CORE_CN:
            return ITEM_CORE_CN[core]
        # 「XX之X」结构的常见后缀
        if core.endswith("_boots"):
            return {
                "power_treads": "动力鞋",
                "phase_boots": "相位鞋",
                "arcane_boots": "秘法鞋",
                "tranquil_boots": "静谧之鞋",
                "guardian_greaves": "卫士胫甲",
            }.get(core, "鞋子")
        if core == "tpscroll":
            return "回城卷轴"
        if core.startswith("ward_observer"):
            return "观察守卫"
        if core.startswith("ward_sentry"):
            return "侦查守卫"
        if core == "ward_dispenser":
            return "眼位器"
        return core.replace("_", " ")

    # 3) 去掉 modifier_ 前缀后统一处理
    core = s[9:] if s.startswith("modifier_") else s

    # 3a) **先查完整名**再剥后缀。
    #     fountain_aura_buff / path_movespeed 这类整名就在通用表里，
    #     先剥 _buff 会得到 fountain_aura，反而查不到。
    if core in GENERIC_CN:
        return GENERIC_CN[core]

    # 少数技能名不带英雄前缀（invoke_bonuses），在剥后缀**之前**认，
    # 否则 _bonuses 一剥就查不到了。
    SPECIAL = {"invoke_bonuses": ("invoker", "祈唤·加成")}
    if core in SPECIAL:
        h, name = SPECIAL[core]
        return CN.get(h, h) + " · " + name

    core, tail_cn = _split_suffix(core)

    # 3b) modifier_item_xxx_yyy：先认出是哪个物品，再挂效果后缀
    #     （不这么做的话 item_ring_of_basilius_effect 会被当成人名翻译）
    if core.startswith("item_"):
        ic = core[5:]
        icore, itail = _split_suffix(ic)
        name = (
            ITEM_CORE_CN.get(icore)
            or ITEM_CORE_CN.get(ic)
            or ITEM_CN.get("item_" + icore)
            or ITEM_CN.get("item_" + ic)
        )
        if name:
            eff = itail or tail_cn
            return name + (("·" + eff) if eff else "")
        core = icore
        tail_cn = itail or tail_cn

    # 4) 通用状态（stunned / invisible / ...）
    if core in GENERIC_CN:
        return GENERIC_CN[core] + (("·" + tail_cn) if tail_cn else "")

    # 4b) 有些 modifier 直接用物品本名（不带 item_ 前缀），如 modifier_bloodthorn_debuff
    if core in ITEM_CORE_CN:
        return ITEM_CORE_CN[core] + (("·" + tail_cn) if tail_cn else "")

    # 5) 非英雄实体（防御塔光环、信使、召唤物等）
    for pre, cn in NONHERO_PREFIX.items():
        if core == pre or core.startswith(pre + "_"):
            rest = core[len(pre) + 1 :] if core != pre else ""
            sk = _words_cn(rest) if rest else ""
            return cn + ((" · " + sk) if sk else "") + (("·" + tail_cn) if tail_cn else "")

    # 6) 英雄技能：拆出英雄名 + 技能名 + 效果
    hero, rest = _hero_of(core)
    if hero:
        hcn = CN.get(HERO_PREFIX_ALIAS.get(hero, hero), cn_of(hero))
        # 先用「英雄_技能」整键查大表：SPELL_CN 里大量条目是全名形式
        # （phantom_assassin_coup_de_grace），只查短名会漏。
        if hero + "_" + rest in SPELL_CN:
            return SPELL_CN[hero + "_" + rest] + (("·" + tail_cn) if tail_cn else "")
        sk = _words_cn(rest)
        parts = [hcn]
        if sk:
            parts.append(sk)
        out = " · ".join(parts)
        if tail_cn:
            out += "·" + tail_cn
        return out

    # 7) 兜底：至少把下划线换成空格，保留可读性
    out = core.replace("_", " ")
    if tail_cn:
        out += "·" + tail_cn
    return out


# ------------------------------------------------------------------ 技能形态
# 目的：给每个技能一个「像什么」的形态标记，前端据此画特效。
# **不做逐技能美术** —— 那既做不完也不通用。这里只定「形态 + 颜色」两类属性：
#   形态 form：决定画什么几何图形（弹道 / 圆形范围 / 扇形 / 直线 / 光环 / 自身增益 / 落点）
#   色系 hue ：决定配色（冰蓝 / 火橙 / 雷紫 / 剧毒绿 / 暗紫 / 物理白）
# 任何新英雄只要能被 form_of() 归到某类，就有特效；归不到的落到 'burst'（小爆炸），
# 也不会没有反馈。所以这张表只需要覆盖「形态特殊」的技能，普通的不用写。
#
# form 取值：
#   bolt    指向性弹道（飞过去命中）
#   aoe     地面圆形范围（持续或瞬发）
#   cone    扇形 / 锥形（双生龙息、超震声波）
#   line    直线（沟壑、肉钩、强力击、冰封路径）
#   aura    自身周围光环（静态风暴、地狱烈焰、腐烂）
#   selfbuf 自身增益（剑刃风暴、风行、时间漫游、黑暗契约）
#   point   指定落点（阳炎冲击、混沌陨石、齐天法阵）
#   chain   弹跳（连环霜冻、雷神之锤）
#   burst   其它 / 未归类 —— 通用小爆炸
SPELL_FORM = {
    # ---- bolt：单体弹道 ----
    "arcane_bolt": ("bolt", "arcane"),
    "magic_missile": ("bolt", "arcane"),
    "fade_bolt": ("bolt", "arcane"),
    "stifling_dagger": ("bolt", "phys"),
    "brain_sap": ("bolt", "dark"),
    "assassinate": ("bolt", "phys"),
    "powershot": ("line", "phys"),
    "shackleshot": ("line", "phys"),
    "concussive_shot": ("bolt", "arcane"),
    "thunder_strike": ("point", "storm"),
    "meat_hook": ("line", "phys"),
    "objurgation": ("bolt", "void"),
    "arcane_orb": ("bolt", "void"),
    # ---- aoe：圆形范围 ----
    "shrapnel": ("aoe", "phys"),
    "mystic_flare": ("aoe", "arcane"),
    "static_storm": ("aoe", "storm"),
    "kinetic_field": ("aoe", "storm"),
    "ice_wall": ("line", "ice"),
    "ice_path": ("line", "ice"),
    "macropyre": ("line", "fire"),
    "chronosphere": ("aoe", "void"),
    "sanity_eclipse": ("aoe", "void"),
    "echo_slam": ("aoe", "phys"),
    "aftershock": ("aoe", "phys"),
    "wukongs_command": ("aoe", "phys"),
    "frost_nova": ("aoe", "ice"),
    "dual_breath": ("cone", "fire"),
    "deafening_blast": ("cone", "arcane"),
    "tornado": ("point", "storm"),
    "chaos_meteor": ("point", "fire"),
    "sun_strike": ("point", "fire"),
    "emp": ("point", "storm"),
    "light_strike_array": ("point", "storm"),
    "primal_spring": ("point", "phys"),
    "tree_dance": ("selfbuf", "nature"),
    "astral_imprisonment": ("point", "void"),
    "nether_swap": ("bolt", "arcane"),
    "fissure": ("line", "phys"),
    "blade_fury": ("aura", "phys"),
    "omni_slash": ("aura", "phys"),
    "swift_slash": ("bolt", "phys"),
    "dark_pact": ("aura", "dark"),
    "rot": ("aura", "poison"),
    "pounce": ("line", "dark"),
    "phantom_strike": ("bolt", "phys"),
    "bladeform": ("aura", "phys"),
    "gale_force": ("bolt", "wind"),
    "windrun": ("selfbuf", "wind"),
    "focusfire": ("selfbuf", "phys"),
    "time_walk": ("selfbuf", "void"),
    "time_dilation": ("aoe", "void"),
    "time_lock": ("bolt", "void"),
    "cold_snap": ("bolt", "ice"),
    "ghost_walk": ("selfbuf", "ice"),
    "alacrity": ("selfbuf", "fire"),
    "forge_spirit": ("point", "fire"),
    "invoke": ("selfbuf", "arcane"),
    "fiends_grip": ("line", "dark"),
    "nightmare": ("bolt", "dark"),
    "enfeeble": ("bolt", "dark"),
    "ancient_seal": ("bolt", "arcane"),
    "telekinesis": ("point", "arcane"),
    "spell_steal": ("bolt", "arcane"),
    "flesh_heap": ("selfbuf", "phys"),
    "dismember": ("line", "poison"),
    "wave_of_terror": ("cone", "arcane"),
    "ignite": ("bolt", "fire"),
    "fireblast": ("bolt", "fire"),
    "unrefined_fireblast": ("bolt", "fire"),
    "bloodlust": ("selfbuf", "fire"),
    "smash": ("bolt", "phys"),
    "healing_ward": ("point", "nature"),
    "boundless_strike": ("aoe", "phys"),
    "bounce_perch": ("point", "nature"),
    "mischief": ("selfbuf", "nature"),
    "sinister_gaze": ("line", "ice"),
    "chain_frost": ("chain", "ice"),
    "frost_shield": ("selfbuf", "ice"),
    "ice_spire": ("point", "ice"),
    "liquid_fire": ("aoe", "fire"),
    "liquid_ice": ("aoe", "ice"),
    "double_trouble": ("aoe", "fire"),
    "enchant_totem": ("selfbuf", "phys"),
    "take_aim": ("selfbuf", "phys"),
    "shadow_dance": ("selfbuf", "dark"),
    "essence_shift": ("selfbuf", "dark"),
    "deafening_blast_disarm": ("cone", "arcane"),
    "macropyre_ice_edge": ("line", "ice"),
    "quas": ("selfbuf", "ice"),
    "wex": ("selfbuf", "storm"),
    "exort": ("selfbuf", "fire"),
    "electromagnetic_repulsion": ("aoe", "storm"),
}
# 未在表里的技能按名字关键词猜形态（顺序敏感，先具体后笼统）
FORM_RULES = [
    ("chronosphere", ("aoe", "void")),
    ("black_hole", ("aoe", "void")),
    ("_wall", ("line", "ice")),
    ("_path", ("line", "ice")),
    ("_hook", ("line", "phys")),
    ("_arrow", ("line", "phys")),
    ("_beam", ("line", "arcane")),
    ("_ray", ("line", "arcane")),
    ("strike", ("point", "storm")),
    ("meteor", ("point", "fire")),
    ("_ward", ("point", "nature")),
    ("spawn", ("point", "nature")),
    ("_swarm", ("aoe", "poison")),
    ("_field", ("aoe", "storm")),
    ("_storm", ("aoe", "storm")),
    ("_nova", ("aoe", "ice")),
    ("nova", ("aoe", "ice")),
    ("_wave", ("cone", "arcane")),
    ("breath", ("cone", "fire")),
    ("_slam", ("aoe", "phys")),
    ("_fury", ("aura", "phys")),
    ("_pact", ("aura", "dark")),
    ("_rot", ("aura", "poison")),
    ("_shield", ("selfbuf", "ice")),
    ("_run", ("selfbuf", "wind")),
    ("_walk", ("selfbuf", "void")),
    ("_bolt", ("bolt", "arcane")),
    ("_missile", ("bolt", "arcane")),
    ("_dagger", ("bolt", "phys")),
    ("_shot", ("bolt", "phys")),
    ("_grip", ("line", "dark")),
    ("_gaze", ("line", "ice")),
    ("_hex", ("bolt", "dark")),
    ("_curse", ("bolt", "dark")),
    ("_buff", ("selfbuf", "nature")),
]
# 色系 -> (主色, 亮色, 辉光)。
#
# 亮色**必须保留饱和度**，不能用接近白的颜色。
# 前端整层是 'lighter' 加色混合，原来 phys 的亮色是 #fff4d8、storm 是 #e8d8ff ——
# 都是 90% 白，两个特效一重叠就推到纯白，画面上出现一圈傻白的甜甜圈，
# 看着像几何图形不像技能。现在亮色改成同色系的「明艳版」：
# 亮度抬高但色相不变，叠加后是同色增亮，不会失去颜色。
HUE = {
    "ice": ("#4fa8d8", "#9fe0ff", "rgba(79,168,216,.55)"),
    "fire": ("#d8641a", "#ffab4a", "rgba(216,100,26,.55)"),
    "storm": ("#8a5ad8", "#c79bff", "rgba(138,90,216,.55)"),
    "arcane": ("#22a894", "#5fe6cf", "rgba(34,168,148,.55)"),
    "dark": ("#8a3fb8", "#c47af0", "rgba(138,63,184,.55)"),
    "poison": ("#5fae22", "#a8e660", "rgba(95,174,34,.55)"),
    "phys": ("#c99a3e", "#ffd07a", "rgba(201,154,62,.50)"),
    "void": ("#4a63c8", "#9aa8ff", "rgba(74,99,200,.55)"),
    "nature": ("#3fae3f", "#8ce88c", "rgba(63,174,63,.50)"),
    "wind": ("#8cc98c", "#d6f5d6", "rgba(140,201,140,.45)"),
}


def form_of(inf):
    """技能 -> (form, hue)。永远返回可用值，未识别的落 burst/phys。"""
    s = str(inf or "")
    core = s[9:] if s.startswith("modifier_") else s
    if s.startswith("item_"):
        core = s[5:]
        # 主动道具按效果给形态
        if core in ("blade_mail", "mjollnir", "maelstrom", "stormcrafter"):
            return ("chain", "storm")
        if core in (
            "searing_signet",
            "hydras_breath",
            "bloodthorn",
            "devastator",
            "gungnir",
            "witch_blade",
            "nullifier",
        ):
            return ("bolt", "fire")
        return ("burst", "phys")
    # 剥英雄前缀
    hero, rest = _hero_of(core)
    key = rest if hero else core
    # 1) 精确表
    for cand in (core, key):
        if cand in SPELL_FORM:
            return SPELL_FORM[cand]
    # 2) 去效果后缀再查（frost_nova_slow -> frost_nova）
    bare, _ = _split_suffix(key)
    if bare in SPELL_FORM:
        return SPELL_FORM[bare]
    b2, _ = _split_suffix(core)
    if b2 in SPELL_FORM:
        return SPELL_FORM[b2]
    # 3) 关键词猜
    for pat, val in FORM_RULES:
        if pat in key or pat in core:
            return val
    return ("burst", "phys")


# ------------------------------------------------------------------ 阵营
def infer_team(series, combat, idx):
    """返回 {slug: 0(天辉)/1(夜魇)}。

    优先用 combat 里的 team 字段（Dump 会带 player_slot 的话）—— 实测没有。
    退而用「互相伤害」的二分：队友之间不会互相造成伤害，所以敌人图是两个团（极少数误伤除外）。
    再用出生点定谁是天辉：天辉泉水在 (≈9600,≈9600)，夜魇在 (≈23300,≈22800)，
    比较 x+y 即可，这个判据不受地图朝向影响。
    """
    enemy = defaultdict(set)
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_DAMAGE":
            continue
        a = norm_npc(e.get("atk"))
        b = norm_npc(e.get("tgt"))
        if a and b and a != b and a in idx and b in idx:
            enemy[a].add(b)
            enemy[b].add(a)

    allh = set(idx)
    if not enemy:
        # 完全没有战斗日志：按出生点硬分（左下一半 / 右上一半）
        out = {}
        for h in allh:
            seq = series[h]
            if not seq:
                out[h] = 0
                continue
            x, y = seq[min(seq)][3], seq[min(seq)][4]
            out[h] = 0 if (x + y) < 2 * 16384 else 1
        return out

    # 以「敌人最多」的人为种子做二分图着色
    seed = max(allh, key=lambda s: len(enemy.get(s, ())))
    color = {seed: 0}
    stack = [seed]
    while stack:
        cur = stack.pop()
        for nb in enemy.get(cur, ()):
            if nb not in color:
                color[nb] = 1 - color[cur]
                stack.append(nb)
    for h in allh:  # 孤立点（整场没打过人）走出生点判据
        if h not in color:
            seq = series[h]
            x, y = (seq[min(seq)][3], seq[min(seq)][4]) if seq else (0, 0)
            color[h] = 0 if (x + y) < 2 * 16384 else 1

    # 出生点校验：天辉一组的总 x+y 应该更小；不是就整组取反
    def birth_sum(g):
        tot, n = 0.0, 0
        for h in allh:
            if color[h] != g:
                continue
            seq = series[h]
            if not seq:
                continue
            t = min(seq)
            tot += seq[t][3] + seq[t][4]
            n += 1
        return tot / max(1, n)

    if birth_sum(0) > birth_sum(1):
        color = {h: 1 - c for h, c in color.items()}
    return color


# ------------------------------------------------------------------ 资源内联
def data_uri(path, mime):
    with open(path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()


def load_assets(heroes):
    """底图 + 本窗口出场英雄的头像，全部内联成 data URI。

    ⚠ 为什么要内联：外链头像在无头截图/离线打开时会因为没加载完而画成空块，
    看着像渲染 bug，其实是时序问题（这条踩过两次）。
    头像先由 mkavatar.js 裁成 72×72 webp，127 张合计约 385KB，可接受。
    """
    out = {"map": None, "avatars": {}}
    mp = os.path.join(ASSETS, "map.jpg")
    if os.path.exists(mp):
        out["map"] = data_uri(mp, "image/jpeg")

    ap = os.path.join(ASSETS, "avatars72.json")
    if os.path.exists(ap):
        with open(ap, encoding="utf-8") as fh:
            table = json.load(fh)
        for h in heroes:
            if h in table:
                out["avatars"][h] = "data:image/webp;base64," + table[h]
    return out


# ------------------------------------------------------------------ 主流程
class FightRenderError(Exception):
    """渲染失败（缺输入、数据不足以画一帧、模板里找不到标记）。

    单独一个异常类型，是为了让插件侧能把「这一波画不出来」和
    「代码 bug」分开处理：前者应当回退成文本复盘（或如实告知），
    后者应当带着栈进日志。
    """


def script_json(data):
    """JSON 嵌入 script 时防止文本中的闭合标签逃逸。"""
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def attach_commentary(path, commentary):
    """复用已渲染帧，模型返回后只替换内嵌 JSON，不再重读整场录像。"""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    prefix = "const DATA = "
    start = html.index(prefix) + len(prefix)
    data, length = json.JSONDecoder().raw_decode(html[start:])
    data["commentary"] = commentary
    html = html[:start] + script_json(data) + html[start + length :]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


def build(
    match,
    t0,
    t1,
    work_root,
    out_path,
    *,
    commentary=None,
    evidence_out=None,
    match_clock_offset=0.0,
):
    """把一场比赛的一个时间窗渲染成单文件 HTML，返回统计信息。

    Args:
        match: 比赛 ID（字符串），``work_root`` 下的子目录名。
        t0 / t1: 窗口起止秒，**战斗日志时间轴**（= 比赛时间 + ``combatlog_to_match``）。
        work_root: 含 ``<match>/fine.csv`` 与 ``<match>/combat.jsonl`` 的目录。
            **也接受直接给到比赛目录本身**（即以 ``fine.csv`` 结尾的那一层），
            见下面 ``d`` 的推导 —— 这两种传法都踩过，与其让人记，不如都认。
        out_path: 输出 HTML 路径。

    Returns:
        ``{"heroes": n, "spells": n, "items": n, "kills": n, "mods": n,
        "fx": n, "frames": n, "avatar": "10/10", "has_map": bool,
        "untranslated": [原始键...], "size": 字节数, "calibrate": (偏移, 票数)}``

    Raises:
        FightRenderError: 输入缺失或数据不足以渲染。

    为什么不再是 `sys.exit`（原样搬过来的写法）：这个函数现在被插件在
    后台任务里调用，`sys.exit` 会直接杀掉整个 AstrBot 进程 —— 而它看起来
    只是抛了个 SystemExit，很容易被 `except Exception` 漏掉。
    """
    d = os.path.join(work_root, str(match))
    # 兼容两种传法：给「父目录」时拼上 match，给「比赛目录本身」时不拼。
    # 这个坑很隐蔽 —— 两种路径看起来都合理，报错只说「缺少 fine.csv」，
    # 而文件明明就在上一级目录里。判据是「这个目录里有没有 fine.csv」，
    # 不去猜调用方的意图。
    if not os.path.exists(os.path.join(d, "fine.csv")):
        if os.path.exists(os.path.join(work_root, "fine.csv")):
            d = str(work_root)
    fine_path = os.path.join(d, "fine.csv")
    combat_path = os.path.join(d, "combat.jsonl")
    for p in (fine_path, combat_path):
        if not os.path.exists(p):
            raise FightRenderError(f"缺少解析产物：{p}")

    fine, lvl_of = read_fine(fine_path)
    if not fine:
        raise FightRenderError("fine.csv 里没有可用的英雄本体记录")
    combat = read_combat(combat_path)
    off, votes = calibrate(fine_path, combat)

    # fine 的 t 换到 combat 轴
    series = {}
    for slug, seq in fine.items():
        series[slug] = {round(t + off, 2): v for t, v in seq.items()}

    heroes = sorted(series)
    idx = {h: i for i, h in enumerate(heroes)}
    team = infer_team(series, combat, idx)
    death_at = hero_deaths(combat, idx, off)

    # ---------------- 帧 ----------------
    # 位置/血量：0.1 秒一帧，取「该时刻及之前最近的一条本体记录」。
    # 存活：由 combat 的 DEATH 事件驱动。英雄死亡后，本体记录会停止更新，
    #       直到复活（买活/回泉）才恢复，所以「活着」还要额外要求
    #       「最近一条本体记录不超过 RESPAWN_MAX 秒」。
    #       这两条必须同时用：只靠死亡事件会漏掉「死亡时刻没被记录」的情况，
    #       只靠「记录太旧」会把站着不动的人（采样去重，间隔可到 10 秒）误判成尸体。
    DT = 0.1
    nf = int(round((t1 - t0) / DT)) + 1
    frames = {"dt": DT, "t0": t0, "x": {}, "y": {}, "hp": {}, "mp": {}, "al": {}, "mx": {}}
    spawn = {}
    death_log = {}
    for h in heroes:
        seq = series[h]
        ts = sorted(seq)
        died = death_at.get(h, [])
        xs, ys, hps, mps, als, mxs = ([] for _ in range(6))
        j = 0
        last = None  # 最后一次活体状态（尸体定格用）
        for i in range(nf):
            tt = round(t0 + i * DT, 2)
            while j + 1 < len(ts) and ts[j + 1] <= tt:
                j += 1
            if not ts:
                xs.append(None)
                ys.append(None)
                hps.append(0)
                mps.append(0)
                als.append(0)
                mxs.append(1)
                continue
            v = seq[ts[j]]

            # ---------------- 存活判定：只认 combat 的 DEATH 事件 ----------------
            # ⚠ 这里踩过两次坑，都记下来，别再走回头路：
            #
            # 1) 靠「上一条本体记录距今多久」判活（原 RESPAWN_MAX=12 秒）→ 错。
            #    Fine 对「状态没变化」的行会跳帧，站着不动的活人相邻两条能隔十几秒，
            #    会被判成尸体。反过来，实战中还出现过另一种更糟的情况：
            #    Fine 采样漏掉了击杀瞬间（血量从 2813 直接跳到 2769，从没归零），
            #    尸体记录一直续着，于是「死了的人」永远判活 —— 这一版就是这个问题，
            #    击杀横幅弹出来了，人还在场上满血站着。
            #
            # 2) 靠 hp=0 / alive=0 判死 → 更错。那些行绝大多数是幻象、分身、尸体残留
            #    （Bane 单场 21157 条），真死的人反而未必留得下 hp=0 的行。
            #
            # 现在：**只看 combat 的 DOTA_COMBATLOG_DEATH**。它是权威的、
            # 按玩家英雄过滤过的、且经过 norm_npc 归一的。
            # 复活则靠「死亡之后本体记录出现血量回升」来识别 —— 这不需要猜时间，
            # 直接观察数据本身。
            last_death = None
            for d in died:
                if d <= tt:
                    last_death = d
                else:
                    break

            # 死亡后是否已经复活：死亡时刻之后的记录血量重新变正，且明显高于 0
            revived = False
            if last_death is not None and ts[j] > last_death + 0.5:
                revived = v[0] > 0

            is_dead = last_death is not None and not revived

            if is_dead:
                xs.append(round(last[3]) if last else round(v[3]))
                ys.append(round(last[4]) if last else round(v[4]))
                hps.append(0)
                mps.append(0)
                mxs.append(last[1] if last else v[1])
                als.append(0)
            else:
                last = v
                xs.append(round(v[3]))
                ys.append(round(v[4]))
                hps.append(v[0])
                mps.append(v[2])
                mxs.append(v[1])
                als.append(1)
        frames["x"][h] = xs
        frames["y"][h] = ys
        frames["hp"][h] = hps
        frames["mp"][h] = mps
        frames["al"][h] = als
        frames["mx"][h] = mxs
        if ts:
            first = seq[ts[0]]
            spawn[h] = [round(first[3]), round(first[4])]

        # 日志：窗口内的阵亡（直接用 combat 的死亡事件，不再依赖帧循环里的副作用）
        for d in died:
            if t0 <= d <= t1:
                death_log.setdefault(h, []).append(d)

    if death_log:
        for h, ds in sorted(death_log.items()):
            print(
                f"  窗口内阵亡：{h} @ " + ", ".join(f"{d:.1f}" for d in sorted(set(ds))),
                file=sys.stderr,
            )

    # ---------------- 事件 ----------------
    spells, items, kills, mods = [], [], [], []
    for t, e in combat:
        if not (t0 - 2 <= t <= t1 + 2):
            continue
        typ = e.get("type")
        rel = round(t - t0, 2)
        atk = norm_npc(e.get("atk"))
        tgt = norm_npc(e.get("tgt"))
        inf = str(e.get("inf") or "")
        if typ == "DOTA_COMBATLOG_ABILITY":
            if atk in idx:
                spells.append(
                    {
                        "t": rel,
                        "c": idx[atk],
                        "n": spell_cn(inf),
                        "raw": inf,
                        "tg": [idx[tgt]] if tgt in idx else [],
                    }
                )
        elif typ == "DOTA_COMBATLOG_ITEM":
            if atk in idx:
                items.append({"t": rel, "c": idx[atk], "n": spell_cn(inf), "raw": inf})
        elif typ == "DOTA_COMBATLOG_DEATH":
            if tgt in idx:
                kills.append({"t": rel, "v": idx[tgt], "by": idx.get(atk, -1)})
        elif typ == "DOTA_COMBATLOG_MODIFIER_ADD":
            if tgt in idx:
                mods.append(
                    {
                        "t0": rel,
                        "t1": None,
                        "tg": idx[tgt],
                        "src": idx.get(atk, -1),
                        "n": spell_cn(inf),
                        "raw": inf,
                        "mdur": e.get("mdur"),
                    }
                )

    # modifier 配对：同名同目标按时间顺序先进先出
    open_mod = defaultdict(list)
    for m in mods:
        open_mod[(m["tg"], m["raw"])].append(m)
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_MODIFIER_REMOVE":
            continue
        if not (t0 - 2 <= t <= t1 + 2):
            continue
        tgt = norm_npc(e.get("tgt"))
        if tgt not in idx:
            continue
        lst = open_mod.get((idx[tgt], str(e.get("inf") or "")))
        if lst:
            lst.pop(0)["t1"] = round(t - t0, 2)

    # 控制条：只要带 stun/sil/slow 的
    ctrl = []
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_MODIFIER_ADD":
            continue
        if not (t0 - 2 <= t <= t1 + 2):
            continue
        kinds = []
        if (e.get("stun") or 0) > 0:
            kinds.append("stun")
        if (e.get("sil") or 0) > 0:
            kinds.append("sil")
        if (e.get("slow") or 0) > 0:
            kinds.append("slow")
        if kinds:
            tgt = norm_npc(e.get("tgt"))
            if tgt in idx:
                ctrl.append(
                    {
                        "t": round(t - t0, 2),
                        "tg": idx[tgt],
                        "k": kinds,
                        "n": spell_cn(e.get("inf")),
                        "d": e.get("mdur"),
                    }
                )

    # ---------------- 特效 ----------------
    # 两类来源，合成统一的 fx 数组交给前端播动画：
    #   1) 技能事件（ABILITY / 带 inf 的技能伤害）-> 技能特效，形态由 form_of 决定
    #   2) inf 为空的伤害事件 -> 普攻（飞行线 + 命中闪光）
    #
    # 关键点：**同一次施放会刷出很多条伤害**。以静态风暴为例，它在 5.4 秒里
    # 每 0.3 秒对范围内每人跳一次伤害，一共 45 条；暗影契约 14 秒里 28 条。
    # 逐条画会变成「同一个技能闪 45 次」，完全糊掉。
    # 所以先把伤害按「同施法者 + 同技能 + 时间相邻」聚成**施放实例**，
    # 一个实例只画一个特效，时长 = 首末伤害间隔（持续型技能据此画成范围光柱）。
    def ITEMISH(i):
        return i.startswith("item_")

    # 施放实例聚合窗口：间隔小于这个值就算同一次施放。持续伤害的最小跳间隔约 0.3s，
    # 而同一技能的最短 CD 也在 1s 以上，取 0.9s 能把「一次施放的多跳」与
    # 「两次独立施放」干净地分开。
    JOIN_GAP = 0.9

    casts = defaultdict(list)  # (caster, inf) -> [(t, target, val), ...]
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_DAMAGE":
            continue
        if not (t0 - 3 <= t <= t1 + 3):
            continue
        atk = norm_npc(e.get("atk"))
        inf = str(e.get("inf") or "")
        if atk not in idx or not inf:
            continue
        tgt = norm_npc(e.get("tgt"))
        casts[(idx[atk], inf)].append(
            (t, idx.get(tgt, -1) if tgt in idx else -1, e.get("val") or 0)
        )

    # 每个 (施法者, 技能) 的伤害按时间排序后切成若干实例
    inst = []  # {c, inf, t0, t1, val, tgts:set, hits:n}
    for (c, inf), rows in casts.items():
        rows.sort()
        cur = None
        for t, g, v in rows:
            if cur is not None and t - cur["t1"] <= JOIN_GAP:
                cur["t1"] = t
                cur["val"] += v
                cur["hits"] += 1
                if g >= 0:
                    cur["tgts"].add(g)
            else:
                if cur is not None:
                    inst.append(cur)
                cur = {
                    "c": c,
                    "inf": inf,
                    "t0": t,
                    "t1": t,
                    "val": v,
                    "hits": 1,
                    "tgts": set([g] if g >= 0 else []),
                }
        if cur is not None:
            inst.append(cur)

    # ABILITY 事件补两类东西：
    #   a) 施法瞬间（很多技能伤害有延迟，起手用它更符合直觉）
    #   b) 完全没伤害的动作（控制、位移、召唤、buff）
    cast_at = defaultdict(list)
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_ABILITY":
            continue
        atk = norm_npc(e.get("atk"))
        inf = str(e.get("inf") or "")
        if atk in idx and inf:
            cast_at[(idx[atk], inf)].append(t)

    # 把每个实例的起手时间对齐到最近的 ABILITY（1.2 秒内），拿不到就用首个伤害时刻
    for x in inst:
        lst = cast_at.get((x["c"], x["inf"]))
        if not lst:
            continue
        best = None
        for ct in lst:
            if ct <= x["t0"] + 0.5 and x["t0"] - ct <= 1.2:
                best = ct
        if best is not None:
            x["t0"] = best

    fx = []

    # ---- 实例 -> 特效 ----
    for x in inst:
        form, hue = form_of(x["inf"])
        caster = x["c"]
        g = sorted(x["tgts"])[0] if x["tgts"] else -1
        dur = round(x["t1"] - x["t0"], 2)
        # 持续型：多跳且跨度 > 1s，或者形态本来就是范围/光环/直线，给一个像样的时长
        if x["hits"] >= 3 and dur >= 1.0:
            pass  # 用实测跨度
        elif form in ("aoe", "aura", "line", "cone") and dur < 1.0:
            dur = 1.2  # 单帧范围技能也给个可见时长
        else:
            dur = max(dur, 0)
        fx.append(
            {
                "k": "sk",
                "t": round(x["t0"] - t0, 2),
                "c": caster,
                "g": g,
                "v": x["val"],
                "f": form,
                "h": hue,
                "n": spell_cn(x["inf"]),
                "d": dur,
                "r": len(x["tgts"]),
            }
        )

    # ---- 纯 ABILITY（无伤害）：控制、位移、召唤、增益 ----
    # 这些也得画，否则「时间结界」「祸乱大招」这种关键动作在地图上看不到。
    SKIP_ABILITY = (
        "courier_",
        "ability_",
        "invoker_quas",
        "invoker_wex",
        "invoker_exort",
        "invoker_invoke",
        "_instance",
    )
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_ABILITY":
            continue
        if not (t0 - 0.5 <= t <= t1 + 0.5):
            continue
        atk = norm_npc(e.get("atk"))
        inf = str(e.get("inf") or "")
        if atk not in idx or not inf:
            continue
        if any(p in inf for p in SKIP_ABILITY):
            # 切球（Quas/Wex/Exort）是祈唤者的内部动作，没有可视效果；
            # courier_/ability_ 是非战斗动作。都不画。
            continue
        # 同施法者、同名技能在 1.2 秒内已由伤害实例画过 → 跳过
        already = False
        for y in fx:
            if y["k"] == "sk" and y["c"] == idx[atk] and abs(y["t"] - (t - t0)) < 1.2:
                already = True
                break
        if already:
            continue
        form, hue = form_of(inf)
        tgt = norm_npc(e.get("tgt"))
        fx.append(
            {
                "k": "sk",
                "t": round(t - t0, 2),
                "c": idx[atk],
                "g": idx[tgt] if tgt in idx else -1,
                "v": 0,
                "f": form,
                "h": hue,
                "n": spell_cn(inf),
                "d": 0.9 if form in ("aoe", "aura", "line", "cone") else 0,
                "r": 1 if tgt in idx else 0,
            }
        )

    # ---- 普攻 ----
    # 同一次攻击在日志里会拆成 2~3 条（同一瞬间、同一攻方同一目标），
    # 按 0.15 秒归并，否则一发普攻闪三次。伤害值累加，数字才准。
    aa_bucket = {}
    for t, e in combat:
        if e.get("type") != "DOTA_COMBATLOG_DAMAGE":
            continue
        if not (t0 - 0.5 <= t <= t1 + 0.5):
            continue
        if e.get("inf"):
            continue  # 有 inf 的是技能，已在上面处理
        atk = norm_npc(e.get("atk"))
        tgt = norm_npc(e.get("tgt"))
        if atk not in idx or tgt not in idx:
            continue  # 小兵 / 塔打人不画（画面会被线填满）
        key = (idx[atk], idx[tgt], round(t * 6))
        hit = aa_bucket.get(key)
        if hit is not None:
            hit["v"] += e.get("val") or 0
            continue
        item = {
            "k": "aa",
            "t": round(t - t0, 2),
            "c": idx[atk],
            "g": idx[tgt],
            "v": e.get("val") or 0,
            "f": "bolt",
            "h": "phys",
            "n": "",
            "d": 0,
            "r": 1,
        }
        aa_bucket[key] = item
        fx.append(item)

    fx.sort(key=lambda x: x["t"])

    # ---------------- 英雄元信息 ----------------
    hero_meta = []
    for h in heroes:
        seq = series[h]
        near = [seq[t] for t in seq if t0 <= t <= t1]
        # 窗口内中心点；窗口内没数据就用整场均值兜底
        pool = near if near else list(seq.values())
        cx = sum(v[3] for v in pool) / max(1, len(pool))
        cy = sum(v[4] for v in pool) / max(1, len(pool))
        mx = max((v[1] for v in seq.values()), default=1)  # 全场最大血量
        hero_meta.append(
            {
                "slug": h,
                "n": cn_of(h),
                "t": team.get(h, 0),
                "cx": round(cx),
                "cy": round(cy),
                "maxHp": mx,
                "lvl": lvl_of.get(h, 1),
                "spawn": spawn.get(h, [0, 0]),
            }
        )

    assets = load_assets(heroes)
    data = {
        "meta": {
            "match": match,
            "t0": t0,
            "t1": t1,
            "dur": round(t1 - t0, 1),
            "off": off,
            "clock0": t0 - match_clock_offset,
            "world": [WORLD_MIN, WORLD_MAX],
        },
        "heroes": hero_meta,
        "frames": frames,
        "spells": sorted(spells, key=lambda s: s["t"]),
        "items": sorted(items, key=lambda s: s["t"]),
        "kills": sorted(kills, key=lambda s: s["t"]),
        "mods": sorted(mods, key=lambda s: s["t0"]),
        "ctrl": sorted(ctrl, key=lambda s: s["t"]),
        "fx": fx,
        "hue": HUE,
        "map": assets["map"],
        "avatars": assets["avatars"],
    }

    if evidence_out is not None or commentary is not None:
        try:
            from . import dota_fight_commentary as commentary_module
        except ImportError:
            import dota_fight_commentary as commentary_module
        pack = commentary_module.evidence(data, combat, t0, t1, norm_npc, spell_cn)
        if evidence_out is not None:
            evidence_out.update(pack)
        data["commentary"] = commentary or commentary_module.fallback(pack)

    tpl = os.path.join(HERE, "fight_assets", "fight_tpl.html")
    with open(tpl, encoding="utf-8") as fh:
        html = fh.read()
    # 模板里没有这个标记时 `replace` 会**静默什么都不做**，产出一份
    # 数据块为空的页面 —— 打开是白屏，而调用方看到的是「文件写成功了」。
    # 所以这里必须断言。
    if "/*__DATA__*/" not in html:
        raise FightRenderError("模板里找不到 /*__DATA__*/ 标记，可能是 fight_tpl.html 版本不匹配")
    html = html.replace("/*__DATA__*/", "const DATA = " + script_json(data) + ";")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    # 把「没翻成中文」的实体列出来，方便下次遇到同英雄时照单补 SKILL_CORE_CN。
    # **不算错误**：兜底会保留英文原词并带上英雄中文名，仍然可读。
    untrans = sorted(
        {s["raw"] for s in spells + items + mods if s.get("raw") and _has_ascii(spell_cn(s["raw"]))}
    )
    return {
        "heroes": len(heroes),
        "spells": len(spells),
        "items": len(items),
        "kills": len(kills),
        "mods": len(mods),
        "fx": len(fx),
        "frames": nf,
        "avatar": f"{len(assets['avatars'])}/{len(heroes)}",
        "has_map": bool(assets["map"]),
        "untranslated": untrans,
        "size": os.path.getsize(out_path),
        "calibrate": (off, votes),
    }


def _has_ascii(s):
    """判断字符串里是否还有「裸的」英文词（说明没翻干净）。

    允许 ABC（括号内标注）这种形式 —— 人名/技能名保留英文原名作对照是合理的，
    所以把括号里的内容先剥掉再判断。
    """
    import re

    t = re.sub(r"[（(][^）)]*[）)]", "", str(s))
    return bool(re.search(r"[A-Za-z]{2,}", t))


def main():
    ap = argparse.ArgumentParser(description="把一场比赛的一个时间窗渲染成单文件团战回放 HTML")
    ap.add_argument("--match", required=True)
    ap.add_argument(
        "--from",
        dest="t0",
        type=float,
        required=True,
        help="窗口起，战斗日志时间轴（= 比赛时间 + combatlog_to_match）",
    )
    ap.add_argument("--to", dest="t1", type=float, required=True, help="窗口止，同上")
    ap.add_argument("--work", default="/opt/astrbot/data/work/tracework")
    ap.add_argument("--out", default="fight.html")
    a = ap.parse_args()
    try:
        info = build(a.match, a.t0, a.t1, a.work, a.out)
    except FightRenderError as e:
        print(f"渲染失败：{e}", file=sys.stderr)
        raise SystemExit(1) from e
    off, votes = info["calibrate"]
    print(
        f"写出 {a.out}：{info['heroes']} 英雄, {info['spells']} 施法, "
        f"{info['items']} 物品, {info['kills']} 击杀, {info['mods']} 状态, "
        f"{info['fx']} 特效, {info['frames']} 帧, "
        f"底图={'有' if info['has_map'] else '缺'}, "
        f"头像={info['avatar']}, "
        f"时间标定 {off:+.1f}s（{votes} 票）, "
        f"{info['size'] / 1024:.0f} KB",
        file=sys.stderr,
    )
    if info["untranslated"]:
        print(
            f"  未翻译实体 {len(info['untranslated'])} 个（可补 SKILL_CORE_CN）：", file=sys.stderr
        )
        for u in info["untranslated"][:40]:
            print(f"    {u}", file=sys.stderr)
        if len(info["untranslated"]) > 40:
            print(f"    … 另有 {len(info['untranslated']) - 40} 个", file=sys.stderr)


if __name__ == "__main__":
    main()
