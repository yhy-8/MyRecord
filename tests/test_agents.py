import unittest

from AgentRecord.agents import (
    AGENTS,
    researcher,
    research_planner,
    retrospective,
    reviewer,
)
from AgentRecord.agents.base import (
    AgentPipelineError,
    _parse_json,
    _prompt,
    cited_source_ids,
)


class AgentModuleTests(unittest.TestCase):
    def test_json_parser_accepts_one_outer_markdown_fence(self):
        self.assertEqual(
            {"markdown": "内容"},
            _parse_json('```json\n{"markdown":"内容"}\n```'),
        )

    def test_json_parser_does_not_extract_json_from_explanatory_prose(self):
        with self.assertRaisesRegex(AgentPipelineError, "JSON 无法解析"):
            _parse_json('我已经完成了：\n{"markdown":"内容"}')

    def test_json_parser_recovers_lone_trailing_delimiters(self):
        self.assertEqual(
            {"markdown": "内容"},
            _parse_json('{"markdown":"内容"}"}'),
        )

    def test_json_parser_rejects_two_concatenated_objects(self):
        with self.assertRaisesRegex(AgentPipelineError, "JSON 无法解析"):
            _parse_json('{"markdown":"第一份"}{"markdown":"第二份"}')

    def test_four_agents_have_separate_responsibilities(self):
        self.assertEqual(
            {"retrospective", "research_planner", "researcher", "reviewer"},
            set(AGENTS),
        )
        self.assertEqual(frozenset(), AGENTS["researcher"].allowed_tools)
        self.assertEqual(
            frozenset({"web_search"}), researcher.NATIVE_SEARCH_SPEC.allowed_tools
        )
        self.assertEqual(frozenset(), AGENTS["retrospective"].allowed_tools)
        self.assertTrue(AGENTS["reviewer"].can_read_raw)

    def test_retrospective_requires_structured_sources_for_each_paragraph(self):
        with self.assertRaisesRegex(AgentPipelineError, "每段必须选择"):
            retrospective.validate(
                {
                    "sections": [
                        {
                            "title": "回顾",
                            "paragraphs": [{"text": "第一段没有来源", "source_refs": []}],
                        }
                    ],
                    "profile_entries": [],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_controller_renders_grouped_record_citations(self):
        result, _ = retrospective.validate(
            {
                "sections": [
                    {
                        "title": "回顾",
                        "paragraphs": [
                            {
                                "text": "整理内容",
                                "source_refs": [
                                    "R-20260714-001",
                                    "R-20260714-002",
                                ],
                            }
                        ],
                    }
                ],
                "profile_entries": [],
            },
            allowed_source_ids={"R-20260714-001", "R-20260714-002"},
            current_source_ids={"R-20260714-001", "R-20260714-002"},
            visible_profile_ids=set(),
        )

        self.assertEqual(
            "### 回顾\n\n整理内容 [R-20260714-001, R-20260714-002]",
            result,
        )

    def test_record_range_citation_expands_for_review_context(self):
        self.assertEqual(
            {
                "R-20260707-007",
                "R-20260707-008",
                "R-20260707-009",
                "R-20260707-010",
            },
            cited_source_ids("采购过程 [R-20260707-007~010]"),
        )

    def test_profile_update_requires_current_period_evidence(self):
        with self.assertRaisesRegex(AgentPipelineError, "本周期来源"):
            retrospective.validate(
                {
                    "sections": [
                        {
                            "title": "",
                            "paragraphs": [
                                {
                                    "text": "整理内容",
                                    "source_refs": ["R-20260714-001"],
                                }
                            ],
                        }
                    ],
                    "profile_entries": [
                        {
                            "category": "viewpoint",
                            "title": "观点",
                            "statement": "一个观点",
                            "confidence": 0.8,
                            "source_refs": ["R-20260701-001"],
                            "supersedes_id": None,
                        }
                    ],
                },
                allowed_source_ids={"R-20260701-001", "R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_behavior_pattern_requires_two_distinct_records(self):
        with self.assertRaisesRegex(AgentPipelineError, "至少两条"):
            retrospective.validate(
                {
                    "sections": [
                        {
                            "title": "",
                            "paragraphs": [
                                {
                                    "text": "整理内容",
                                    "source_refs": ["R-20260714-001"],
                                }
                            ],
                        }
                    ],
                    "profile_entries": [
                        {
                            "category": "behavior_pattern",
                            "title": "行为模式",
                            "statement": "反复表现出的模式",
                            "confidence": 0.8,
                            "source_refs": ["R-20260714-001"],
                            "supersedes_id": None,
                        }
                    ],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_profile_candidate_cannot_duplicate_an_existing_profile(self):
        profile = {
            "category": "viewpoint",
            "title": "已有观点",
            "statement": "已有内容",
        }
        with self.assertRaisesRegex(AgentPipelineError, "与现有条目重复"):
            retrospective.validate(
                {
                    "sections": [
                        {
                            "title": "",
                            "paragraphs": [
                                {
                                    "text": "整理内容",
                                    "source_refs": ["R-20260714-001"],
                                }
                            ],
                        }
                    ],
                    "profile_entries": [
                        {
                            **profile,
                            "confidence": 0.8,
                            "source_refs": ["R-20260714-001"],
                            "supersedes_id": None,
                        }
                    ],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids={"entry-id"},
                visible_profiles={"entry-id": profile},
            )

    def test_one_output_cannot_repeat_the_same_profile_candidate(self):
        candidate = {
            "category": "interest",
            "title": "长期关注",
            "statement": "持续关注同一领域",
            "confidence": 0.8,
            "source_refs": ["R-20260714-001"],
            "supersedes_id": None,
        }
        with self.assertRaisesRegex(AgentPipelineError, "重复的人物画像候选"):
            retrospective.validate(
                {
                    "sections": [
                        {
                            "title": "",
                            "paragraphs": [
                                {
                                    "text": "整理内容",
                                    "source_refs": ["R-20260714-001"],
                                }
                            ],
                        }
                    ],
                    "profile_entries": [candidate, candidate],
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids=set(),
            )

    def test_research_planner_sanitizes_private_query_data(self):
        topics = research_planner.validate(
            {
                "topics": [
                    {
                        "topic_id": "Q001",
                        "title": "公开研究问题 D:/private/title.txt",
                        "query": "研究 /private/a 和 12345678",
                        "reason": "拓宽视野",
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    }
                ]
            },
            {"R-20260714-001"},
        )
        self.assertNotIn("/private", topics[0]["query"])
        self.assertNotIn("D:/private", topics[0]["title"])
        self.assertNotIn("12345678", topics[0]["query"])

    def test_research_planner_accepts_up_to_five_topics(self):
        topics = research_planner.validate(
            {
                "topics": [
                    {
                        "topic_id": f"Q{index:03d}",
                        "title": f"主题 {index}",
                        "query": f"公开查询 {index}",
                        "reason": "值得研究",
                        "origin": "news",
                        "source_refs": [],
                    }
                    for index in range(1, 6)
                ]
            },
            set(),
        )

        self.assertEqual(5, len(topics))

    def test_research_planner_rejects_more_than_five_topics(self):
        with self.assertRaisesRegex(AgentPipelineError, "一至五个"):
            research_planner.validate(
                {
                    "topics": [
                        {
                            "topic_id": f"Q{index:03d}",
                            "title": f"主题 {index}",
                            "query": f"公开查询 {index}",
                            "reason": "值得研究",
                            "origin": "news",
                            "source_refs": [],
                        }
                        for index in range(1, 7)
                    ]
                },
                set(),
            )

    def test_grounded_researcher_uses_controller_owned_evidence_ids(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "记录与研究",
                "origin": "records",
                "source_refs": ["R-20260714-001"],
            }
        ]
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "title": "权威来源",
                "url": "https://example.com/article_(one)",
                "published": "2026-07-14",
            }
        ]

        drafts = researcher.validate_grounded(
            {
                "topics": [
                    {
                        "topic_id": "Q001",
                        "status": "supported",
                        "reason": "",
                        "paragraphs": [
                            {
                                "kind": "inference",
                                "text": "外部证据说明了适用边界。",
                                "record_refs": ["R-20260714-001"],
                                "evidence_refs": ["W-Q001-001"],
                            }
                        ],
                    }
                ]
            },
            topics,
            evidence,
            {"R-20260714-001"},
        )
        rendered, sources = researcher.render_grounded(
            drafts, topics, evidence
        )

        self.assertNotIn("W-Q001-001", rendered)
        self.assertIn("[AI推断]", rendered)
        self.assertIn("### 记录与研究", rendered)
        self.assertIn("https://example.com/article_%28one%29", rendered)
        self.assertEqual(["https://example.com/article_(one)"], [s["url"] for s in sources])

    def test_native_researcher_maps_audited_urls_to_controller_ids(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "公开主题",
                "origin": "news",
                "source_refs": [],
            }
        ]
        drafts, evidence = researcher.validate_native(
            {
                "topics": [
                    {
                        "status": "supported",
                        "reason": "",
                        "paragraphs": [
                            {
                                "kind": "evidence",
                                "text": "搜索材料支持这一边界。",
                                "record_refs": [],
                                "source_urls": ["https://example.com/a?utm_source=x"],
                            }
                        ],
                    }
                ]
            },
            topics,
            [
                {
                    "title": "实际搜索结果",
                    "url": "https://example.com/a",
                    "snippet": "证据摘要",
                }
            ],
            set(),
        )

        self.assertEqual("Q001", drafts[0]["topic_id"])
        self.assertEqual(["W-Q001-001"], drafts[0]["paragraphs"][0]["evidence_refs"])
        self.assertEqual("https://example.com/a", evidence[0]["url"])

    def test_grounded_researcher_rejects_model_written_url(self):
        with self.assertRaisesRegex(AgentPipelineError, "不得自行输出 URL"):
            researcher.validate_grounded(
                {
                    "topics": [
                        {
                            "topic_id": "Q001",
                            "status": "supported",
                            "reason": "",
                            "paragraphs": [
                                {
                                    "kind": "evidence",
                                    "text": "事实 https://example.com",
                                    "record_refs": [],
                                    "evidence_refs": ["W-Q001-001"],
                                }
                            ],
                        }
                    ]
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "公开主题",
                        "origin": "news",
                        "source_refs": [],
                    }
                ],
                [
                    {
                        "source_id": "W-Q001-001",
                        "topic_id": "Q001",
                        "url": "https://example.com",
                    }
                ],
                set(),
            )

    def test_grounded_researcher_requires_evidence_for_every_topic(self):
        drafts = researcher.validate_grounded(
            {
                "topics": [
                    {
                        "topic_id": "Q001",
                        "status": "supported",
                        "reason": "",
                        "paragraphs": [
                            {
                                "kind": "evidence",
                                "text": "已覆盖。",
                                "record_refs": [],
                                "evidence_refs": ["W-Q001-001"],
                            }
                        ],
                    },
                    {
                        "topic_id": "Q002",
                        "status": "insufficient_evidence",
                        "reason": "摘要不能直接支持结论",
                        "paragraphs": [],
                    },
                ]
            },
                [
                    {
                        "topic_id": "Q001",
                        "title": "主题一",
                        "origin": "news",
                        "source_refs": [],
                    },
                    {
                        "topic_id": "Q002",
                        "title": "主题二",
                        "origin": "news",
                        "source_refs": [],
                    },
                ],
                [
                    {
                        "source_id": "W-Q001-001",
                        "topic_id": "Q001",
                        "url": "https://example.com/one",
                    },
                    {
                        "source_id": "W-Q002-001",
                        "topic_id": "Q002",
                        "url": "https://example.com/two",
                    },
                ],
                set(),
            )
        self.assertEqual("insufficient_evidence", drafts[1]["status"])

    def test_grounded_researcher_requires_one_ordered_result_per_topic(self):
        topics = [
            {
                "topic_id": "Q001",
                "title": "主题一",
                "origin": "news",
                "source_refs": [],
            },
            {
                "topic_id": "Q002",
                "title": "主题二",
                "origin": "news",
                "source_refs": [],
            },
        ]
        evidence = [
            {
                "source_id": "W-Q001-001",
                "topic_id": "Q001",
                "url": "https://example.com/one",
            },
            {
                "source_id": "W-Q002-001",
                "topic_id": "Q002",
                "url": "https://example.com/two",
            },
        ]

        with self.assertRaisesRegex(AgentPipelineError, "全部主题"):
            researcher.validate_grounded(
                {
                    "topics": [
                        {
                            "status": "insufficient_evidence",
                            "reason": "只有一个主题结果",
                            "paragraphs": [],
                        },
                    ]
                },
                topics,
                evidence,
                set(),
            )

    def test_grounded_researcher_rejects_evidence_under_wrong_topic(self):
        with self.assertRaisesRegex(AgentPipelineError, "越界外部证据"):
            researcher.validate_grounded(
                {
                    "topics": [
                        {
                            "topic_id": "Q001",
                            "status": "supported",
                            "reason": "",
                            "paragraphs": [
                                {
                                    "kind": "evidence",
                                    "text": "错误引用。",
                                    "record_refs": [],
                                    "evidence_refs": ["W-Q002-001"],
                                }
                            ],
                        },
                        {
                            "topic_id": "Q002",
                            "status": "supported",
                            "reason": "",
                            "paragraphs": [
                                {
                                    "kind": "evidence",
                                    "text": "正确引用。",
                                    "record_refs": [],
                                    "evidence_refs": ["W-Q002-001"],
                                }
                            ],
                        },
                    ]
                },
                [
                    {
                        "topic_id": "Q001",
                        "title": "主题一",
                        "origin": "news",
                        "source_refs": [],
                    },
                    {
                        "topic_id": "Q002",
                        "title": "主题二",
                        "origin": "news",
                        "source_refs": [],
                    },
                ],
                [
                    {
                        "source_id": "W-Q001-001",
                        "topic_id": "Q001",
                        "url": "https://example.com/one",
                    },
                    {
                        "source_id": "W-Q002-001",
                        "topic_id": "Q002",
                        "url": "https://example.com/two",
                    },
                ],
                set(),
            )

    def test_profile_cannot_be_superseded_twice_in_one_report(self):
        entries = [
            {
                "category": "viewpoint",
                "title": f"候选 {index}",
                "statement": "更新",
                "confidence": 0.8,
                "source_refs": ["R-20260714-001"],
                "supersedes_id": "profile-1",
            }
            for index in (1, 2)
        ]
        with self.assertRaisesRegex(AgentPipelineError, "多个候选"):
            retrospective.validate(
                {
                    "sections": [
                        {
                            "title": "",
                            "paragraphs": [
                                {
                                    "text": "整理",
                                    "source_refs": ["R-20260714-001"],
                                }
                            ],
                        }
                    ],
                    "profile_entries": entries,
                },
                allowed_source_ids={"R-20260714-001"},
                current_source_ids={"R-20260714-001"},
                visible_profile_ids={"profile-1"},
            )

    def test_reviewer_must_decide_every_profile_entry(self):
        with self.assertRaisesRegex(AgentPipelineError, "未审查全部"):
            reviewer.validate(
                {
                    "pass": True,
                    "entry_decisions": [],
                    "topic_decisions": [],
                    "unsupported_claims": [],
                    "required_changes": [],
                },
                expected_entry_ids={"p1"},
            )

    def test_rejected_profile_candidate_does_not_fail_section_by_itself(self):
        passed, decisions, topic_decisions, feedback = reviewer.validate(
            {
                "pass": True,
                "entry_decisions": [
                    {
                        "temp_id": "p1",
                        "status": "rejected",
                        "reason": "只出现一次，不值得跨周期保存",
                    }
                ],
                "topic_decisions": [],
                "unsupported_claims": [],
                "required_changes": [],
            },
            expected_entry_ids={"p1"},
        )

        self.assertTrue(passed)
        self.assertEqual({"p1": "rejected"}, decisions)
        self.assertEqual({}, topic_decisions)
        self.assertEqual([], feedback)

    def test_revision_prompt_preserves_original_request_as_prefix(self):
        original = _prompt(retrospective.SPEC, "生成", {"records": ["内容"]})
        revised = _prompt(
            retrospective.SPEC,
            "生成",
            {"records": ["内容"]},
            {
                "problems_to_fix": ["缺少引用"],
                "rejected_previous_output": {"markdown": "原稿"},
            },
        )

        shared_prefix = original.rsplit(
            "\n\n只输出一个符合契约的 JSON 对象", 1
        )[0]
        self.assertTrue(revised.startswith(shared_prefix))
        self.assertIn("缺少引用", revised)


if __name__ == "__main__":
    unittest.main()
