import unittest

from app.services.storytelling_craft import (
    build_scene_execution_schema,
    build_storytelling_craft_contract,
    normalize_scene_execution,
    scene_execution_is_complete,
)


class StorytellingCraftTests(unittest.TestCase):
    def test_contract_defines_network_absurdity_without_random_nonsense(self):
        contract = build_storytelling_craft_contract()
        text = str(contract)

        self.assertIn("网络语义", text)
        self.assertIn("答非所问", text)
        self.assertIn("人物锚点", text)
        self.assertIn("不得随机胡言乱语", text)
        self.assertIn("即时目标→阻力→策略→反制→局部变化", text)
        self.assertIn("带人物私心的误读", str(build_scene_execution_schema()))
        self.assertIn("章末优先停在尚未完成的回应", text)
        self.assertIn("称呼变化、视线停留、距离、步速", text)

    def test_scene_execution_requires_two_complete_tactic_turns(self):
        schema = build_scene_execution_schema()
        self.assertIn("ending_residual_force", schema)
        self.assertIn("pov_reaction_chain", schema)
        self.assertIn("dialogue_reaction_chain", schema)

        complete = {
            "entry_pressure": "麻烦已经发生",
            "protagonist_want": "立刻离开",
            "opposing_want": "让他留下解释",
            "tactic_turns": [
                {"actor": "甲", "tactic": "转移话题", "counterforce": "乙追问", "local_change": "借口失效"},
                {"actor": "乙", "tactic": "堵住出口", "counterforce": "甲答应谈条件", "local_change": "双方开始谈判"},
            ],
            "dialogue_pressure": {
                "surface_topic": "谁来关门",
                "hidden_stakes": "谁先承认舍不得",
                "decisive_exchange": "反问迫使甲停下",
            },
            "pov_reaction_chain": {
                "observable_detail": "乙挡住了门",
                "biased_interpretation": "甲以为乙只是想继续追责",
                "immediate_impulse": "甲想装作没听懂并离开",
                "visible_response": "甲握住门把却没有立刻拉开",
            },
            "dialogue_reaction_chain": {
                "trigger": "乙问甲是不是一定要走",
                "evasion_or_misread": "甲故意回答门有没有锁",
                "countermove": "乙直接挡住出口并重复问题",
                "local_consequence": "甲失去回避空间，答应留下谈条件",
            },
            "ending_residual_force": {
                "last_change": "甲留下",
                "reader_question": "乙到底要问什么",
                "next_chapter_first_beat": "乙开口提出问题",
            },
        }
        self.assertTrue(scene_execution_is_complete(complete))

        incomplete = normalize_scene_execution({**complete, "tactic_turns": complete["tactic_turns"][:1]})
        self.assertFalse(scene_execution_is_complete(incomplete))

        missing_reaction_chain = normalize_scene_execution(
            {**complete, "dialogue_reaction_chain": {}}
        )
        self.assertFalse(scene_execution_is_complete(missing_reaction_chain))


if __name__ == "__main__":
    unittest.main()
