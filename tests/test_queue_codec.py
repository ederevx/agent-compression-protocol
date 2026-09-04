import unittest

from acp.queue_codec import (
    QUEUE_ISOLATION_ADDENDUM,
    QueueMemberRequest,
    QueueProtocolViolation,
    build_member_train,
    build_system_prompt,
    parse_queue_response,
)


class BuildSystemPromptTest(unittest.TestCase):
    def test_addendum_is_appended_after_base_prompt(self) -> None:
        prompt = build_system_prompt("base instructions")
        self.assertTrue(prompt.startswith("base instructions"))
        self.assertIn(QUEUE_ISOLATION_ADDENDUM, prompt)

    def test_base_prompt_trailing_whitespace_is_normalized(self) -> None:
        prompt = build_system_prompt("base instructions   \n\n")
        self.assertEqual(
            prompt, "base instructions\n\n" + QUEUE_ISOLATION_ADDENDUM + "\n"
        )


class BuildMemberTrainTest(unittest.TestCase):
    def test_single_member_framing(self) -> None:
        train = build_member_train([QueueMemberRequest(member_id="solo", content="hello")])
        self.assertEqual(
            train, "ACP-QUEUE-ITEM: solo\nhello\n\nACP-QUEUE-MEMBER-COUNT: 1"
        )

    def test_multiple_members_framing_and_order(self) -> None:
        train = build_member_train(
            [
                QueueMemberRequest(member_id="a", content="first"),
                QueueMemberRequest(member_id="b", content="second"),
            ]
        )
        self.assertEqual(
            train,
            "ACP-QUEUE-ITEM: a\nfirst\n\n"
            "ACP-QUEUE-ITEM: b\nsecond\n\n"
            "ACP-QUEUE-MEMBER-COUNT: 2",
        )

    def test_empty_member_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_member_train([])


class ParseQueueResponseHappyPathTest(unittest.TestCase):
    def test_single_pass_member(self) -> None:
        text = "ACP-QUEUE-ITEM: solo\nACP-MODE: PASS"
        results = parse_queue_response(text, ["solo"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].member_id, "solo")
        self.assertEqual(results[0].mode, "PASS")
        self.assertEqual(results[0].output, "")

    def test_single_compact_member_with_output(self) -> None:
        text = "ACP-QUEUE-ITEM: solo\nACP-MODE: COMPACT\n\ncompacted text"
        results = parse_queue_response(text, ["solo"])
        self.assertEqual(results[0].mode, "COMPACT")
        self.assertEqual(results[0].output, "compacted text")

    def test_multiple_members_any_order_in_response(self) -> None:
        text = (
            "ACP-QUEUE-ITEM: b\nACP-MODE: COMPRESS\n\ncapsule b\n\n"
            "ACP-QUEUE-ITEM: a\nACP-MODE: PASS"
        )
        results = parse_queue_response(text, ["a", "b"])
        by_id = {result.member_id: result for result in results}
        self.assertEqual(by_id["a"].mode, "PASS")
        self.assertEqual(by_id["b"].mode, "COMPRESS")
        self.assertEqual(by_id["b"].output, "capsule b")


class ParseQueueResponseViolationTest(unittest.TestCase):
    def test_no_item_blocks_is_violation(self) -> None:
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_response("just some text with no framing", ["solo"])

    def test_missing_mode_line_is_violation(self) -> None:
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_response("ACP-QUEUE-ITEM: solo\nno mode line here", ["solo"])

    def test_duplicate_item_ids_is_violation(self) -> None:
        text = (
            "ACP-QUEUE-ITEM: solo\nACP-MODE: PASS\n\n"
            "ACP-QUEUE-ITEM: solo\nACP-MODE: PASS"
        )
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_response(text, ["solo"])

    def test_missing_expected_member_is_violation(self) -> None:
        text = "ACP-QUEUE-ITEM: a\nACP-MODE: PASS"
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_response(text, ["a", "b"])

    def test_unexpected_extra_member_is_violation(self) -> None:
        text = (
            "ACP-QUEUE-ITEM: a\nACP-MODE: PASS\n\n"
            "ACP-QUEUE-ITEM: invented\nACP-MODE: PASS"
        )
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_response(text, ["a"])


if __name__ == "__main__":
    unittest.main()
