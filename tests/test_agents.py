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
        self.assertTrue(AGENTS["reviewer"].can_read_raw)
        self.assertFalse(AGENTS["researcher"].can_read_raw)

    def test_retrospective_requires_structured_sources_for_each_paragraph(self):
        with self.assertRaisesRegex(AgentPipelineError, "每段必须选择"):
            retrospective.validate(
                {
                    "paragraphs": [
                        {
                            "title": "回顾",
                            "text": "第一段没有来源",
                            "source_refs": [],
                        }
                    ],
                },
                allowed_source_ids={"R-20260714-001"},
            )

    def test_controller_renders_grouped_record_citations(self):
        result = retrospective.validate(
            {
                "paragraphs": [
                    {
                        "title": "回顾",
                        "text": "整理内容",
                        "source_refs": [
                            "R-20260714-001",
                            "R-20260714-002",
                        ],
                    }
                ],
            },
            allowed_source_ids={"R-20260714-001", "R-20260714-002"},
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
                        "origin": "records",
                        "source_refs": ["R-20260714-001"],
                    }
                    for index in range(1, 6)
                ]
            },
            {"R-20260714-001"},
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
