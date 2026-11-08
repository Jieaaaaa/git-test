import json
import unittest

from fact_ledger import parse_groups, render_groups, split_passages, table_shape_errors


class PassageTests(unittest.TestCase):
    def test_partitions_preserve_all_characters_and_order(self):
        sources = ("", "x", "标题\n" + "甲公司取得12万元。乙公司未获批；仅适用淡水。\n" * 90,
                   "Opening. " + "The west unit was rejected; the east unit costs 12.5 units.\n" * 100,
                   "x" * 100000)
        for source in sources:
            with self.subTest(length=len(source)):
                pieces = split_passages(source)
                self.assertEqual("".join(pieces), source)
                self.assertLessEqual(len(pieces), 12)
                if source:
                    self.assertTrue(all(pieces))

    def test_prefers_nearby_sentence_boundaries(self):
        source = "甲" * 48 + "。" + "乙" * 50 + "。" + "丙" * 49 + "。"
        pieces = split_passages(source, target_chars=50)
        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(piece.endswith("。") for piece in pieces))

    def test_long_sentence_uses_balanced_hard_boundaries(self):
        pieces = split_passages("x" * 100001, target_chars=700, max_sections=12)
        self.assertEqual(len(pieces), 12)
        self.assertLessEqual(max(map(len, pieces)) - min(map(len, pieces)), 1)
        self.assertEqual(len("".join(pieces)), 100001)

    def test_prefers_paragraphs_to_avoid_migrating_entity_context(self):
        paragraphs = ["Station %s. %s\n\n" % (letter, "A fact. " * 55) for letter in "ABCDEFGH"]
        pieces = split_passages("".join(paragraphs), target_chars=600)
        self.assertEqual("".join(pieces), "".join(paragraphs))
        self.assertTrue(all(piece.endswith("\n\n") for piece in pieces))

    def test_bad_partition_settings_are_rejected(self):
        for source, target, maximum in ((None, 700, 12), ("text", 0, 12), ("text", 700, 0),
                                         ("text", True, 12), ("text", 700, 1.5)):
            with self.subTest(target=target, maximum=maximum):
                with self.assertRaises(ValueError):
                    split_passages(source, target, maximum)


class LedgerTests(unittest.TestCase):
    def test_parses_closed_json_fence_and_normalizes_only_whitespace(self):
        response = json.dumps({"groups": [{"subject": " 甲研究站\n第一组 ", "facts": [
            "2025年5月7日\t获批61万元", " 未获\n扩建许可；仅适用低盐淡水 "]}]}, ensure_ascii=False)
        expected = [("甲研究站 第一组", ["2025年5月7日 获批61万元", "未获 扩建许可；仅适用低盐淡水"])]
        for wrapped in (response, "```json\n" + response + "\n```", "```\n" + response + "\n```"):
            self.assertEqual(parse_groups(wrapped), expected)

    def test_parser_rejects_malformed_or_empty_structures(self):
        invalid = ("[]", "null", "{}", '{"groups":[]}', '{"groups":"text"}',
                   '{"groups":[[]]}', '{"groups":[{"subject":" ","facts":["fact"]}]}',
                   '{"groups":[{"subject":"A","facts":[]}]}',
                   '{"groups":[{"subject":"A","facts":"fact"}]}',
                   '{"groups":[{"subject":"A","facts":[" "]}]}',
                   '{"groups":[{"subject":"A","facts":[12]}]}',
                   '{"groups":[{"subject":true,"facts":["fact"]}]}',
                   '```json\n{"groups":[]}', '```python\n{"groups":[]}\n```',
                   '{"groups":[{"subject":"A","facts":["fact"]}],"extra":NaN}',
                   '{"groups":[{"subject":"A","subject":"B","facts":["fact"]}]}')
        for response in invalid:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    parse_groups(response)

    def test_parser_bounds_group_and_fact_counts(self):
        for value in ({"groups": [{"subject": "A", "facts": ["fact"]}] * 129},
                      {"groups": [{"subject": "A", "facts": ["fact"] * 129}]}):
            with self.assertRaises(ValueError):
                parse_groups(json.dumps(value))
        groups = parse_groups(json.dumps({"groups": [{"subject": "A", "facts": ["fact"] * 128}] * 128}))
        self.assertEqual(len(groups), 128)
        self.assertEqual(len(groups[0][1]), 128)

    def test_render_merges_exact_subjects_without_changing_facts(self):
        groups = [("甲站", ["获批61万元", "水温高于31摄氏度且工程师签字才启备用泵"]),
                  ("乙站", ["获批67万元", "未获扩建许可"]),
                  ("甲站", ["获批61万元", "水温高于31摄氏度但未签字不得启备用泵"]),
                  ("甲站实验组", ["获批61万元"])]
        self.assertEqual(render_groups(groups),
                         "甲站: 获批61万元; 水温高于31摄氏度且工程师签字才启备用泵; "
                         "水温高于31摄氏度但未签字不得启备用泵\n"
                         "乙站: 获批67万元; 未获扩建许可\n甲站实验组: 获批61万元")

    def test_render_does_not_deduplicate_across_subjects_or_near_matches(self):
        groups = [("Agency A", ["12 units", "12.0 units", "not approved"]),
                  ("Agency B", ["12 units", "not approved"])]
        result = render_groups(groups)
        self.assertEqual(result.count("12 units"), 2)
        self.assertIn("12.0 units", result)
        self.assertEqual(result.count("not approved"), 2)
        self.assertEqual(render_groups([]), "")

    def test_bad_render_fields_are_rejected(self):
        for groups in ([("", ["fact"])], [("A", [])], [("A", "fact")], [("A", [None])]):
            with self.assertRaises(ValueError):
                render_groups(groups)


class TableShapeTests(unittest.TestCase):
    def test_detects_extra_column_and_merged_temperature_duration(self):
        header = "|".join("field%d" % index for index in range(16))
        extra_row = "|".join("value%d" % index for index in range(17))
        text = render_groups([(header, [extra_row]),
                              ("站点|温度℃|持续分钟|许可", ["白石|31℃连续11分钟|工程师签字"])])
        self.assertEqual(table_shape_errors(text), [
            "line 1 row 1: expected 16 columns from subject, got 17",
            "line 2 row 1: expected 4 columns from subject, got 3"])

    def test_normal_pipe_rows_and_prose_notes_are_allowed(self):
        text = render_groups([("站点|温度℃|持续分钟|许可", [
            "白石|31|11|工程师签字", "青田|32|12|工程师签字", "仅限低盐淡水，不外推海水"]),
            ("|Station|Temperature|", ["|White|31℃|", "|Blue|32℃|"])])
        self.assertEqual(table_shape_errors(text), [])

    def test_shared_scope_header_and_colons_do_not_change_column_count(self):
        text = render_groups([("淡水试验（白石至东浦共用单位）站点|日期|阈值℃|持续分钟", [
            "白石|2025-1-5 08:30|31|11", "东浦|2025-8-12 09:40|38|18"]),
            ("Station: notes", ["Temperature: 31℃; duration: 11 minutes"]),
            ("Station|Condition", ["White|engineer: signed"])])
        self.assertEqual(table_shape_errors(text), [])

    def test_plain_prose_or_rows_without_pipe_schema_are_not_tables(self):
        sources = ("", None, "甲站: 水温31℃，连续11分钟后才可启动。",
                   "甲站: 温度|31|时间|11", "甲|乙: 甲获批，乙未获批。",
                   "甲|乙|丙，没有 subject 与 facts 分隔符",
                   "This is prose: A|B is an alternative, not a column schema.")
        for text in sources:
            with self.subTest(text=text):
                self.assertEqual(table_shape_errors(text), [])

    def test_reports_row_positions_and_limits_feedback(self):
        text = "Header|Unit|Amount: ordinary note; A|12; B|13|14|15\n" + "A|B: C|D|E\n" * 20
        errors = table_shape_errors(text)
        self.assertEqual(errors[:3], [
            "line 1 row 2: expected 3 columns from subject, got 2",
            "line 1 row 3: expected 3 columns from subject, got 4",
            "line 2 row 1: expected 2 columns from subject, got 3"])
        self.assertEqual(len(errors), 8)


if __name__ == "__main__":
    unittest.main()
