import json
import unittest

from acp.queue_codec import (
    QUEUE_ISOLATION_ADDENDUM,
    QueueMemberRequest,
    QueueProtocolViolation,
    build_member_block,
    build_queue_envelope,
    build_system_prompt,
    parse_queue_member_result,
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


class BuildMemberBlockTest(unittest.TestCase):
    def test_single_member_framing(self) -> None:
        block = build_member_block(QueueMemberRequest(member_id="solo", content="hello"))
        self.assertEqual(block, "ACP-QUEUE-ITEM: solo\nhello")


class BuildQueueEnvelopeTest(unittest.TestCase):
    def test_envelope_carries_shared_path_and_own_block_only(self) -> None:
        shared = {"model": "x", "messages": [{"role": "user", "content": "__SENTINEL__"}]}
        envelope_bytes = build_queue_envelope(
            shared, ["messages", 0, "content"],
            QueueMemberRequest(member_id="a", content="payload text"))

        envelope = json.loads(envelope_bytes)
        self.assertEqual(envelope["shared"], shared)
        self.assertEqual(envelope["content_path"], ["messages", 0, "content"])
        self.assertEqual(envelope["member_block"], "ACP-QUEUE-ITEM: a\npayload text")
        self.assertIn("member_join", envelope)
        self.assertIn("{member_count}", envelope["count_template"])

    def test_does_not_mutate_caller_shared_dict(self) -> None:
        shared = {"content": "__SENTINEL__"}
        build_queue_envelope(shared, ["content"], QueueMemberRequest(member_id="a", content="x"))
        self.assertEqual(shared, {"content": "__SENTINEL__"})


class ParseQueueMemberResultHappyPathTest(unittest.TestCase):
    def test_single_pass_member(self) -> None:
        text = "ACP-QUEUE-ITEM: solo\nACP-MODE: PASS"
        result = parse_queue_member_result(text, "solo")
        self.assertEqual(result.member_id, "solo")
        self.assertEqual(result.mode, "PASS")
        self.assertEqual(result.output, "")

    def test_single_compact_member_with_output(self) -> None:
        text = "ACP-QUEUE-ITEM: solo\nACP-MODE: COMPACT\n\ncompacted text"
        result = parse_queue_member_result(text, "solo")
        self.assertEqual(result.mode, "COMPACT")
        self.assertEqual(result.output, "compacted text")

    def test_extracts_own_block_ignoring_unknown_coalesced_members(self) -> None:
        # §9: a caller only knows its own member id -- other members
        # coalesced into the same physical response are foreign to it,
        # and must not affect parsing its own result.
        text = (
            "ACP-QUEUE-ITEM: someone-elses-id\nACP-MODE: COMPRESS\n\ncapsule\n\n"
            "ACP-QUEUE-ITEM: mine\nACP-MODE: PASS"
        )
        result = parse_queue_member_result(text, "mine")
        self.assertEqual(result.mode, "PASS")
        self.assertEqual(result.output, "")

    def test_own_block_first_still_parses_correctly(self) -> None:
        text = (
            "ACP-QUEUE-ITEM: mine\nACP-MODE: COMPACT\n\nshrunk\n\n"
            "ACP-QUEUE-ITEM: someone-elses-id\nACP-MODE: PASS"
        )
        result = parse_queue_member_result(text, "mine")
        self.assertEqual(result.mode, "COMPACT")
        self.assertEqual(result.output, "shrunk")


class ParseQueueMemberResultViolationTest(unittest.TestCase):
    def test_no_item_blocks_is_violation(self) -> None:
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_member_result("just some text with no framing", "solo")

    def test_missing_mode_line_is_violation(self) -> None:
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_member_result("ACP-QUEUE-ITEM: solo\nno mode line here", "solo")

    def test_own_id_missing_from_response_is_violation(self) -> None:
        text = "ACP-QUEUE-ITEM: someone-else\nACP-MODE: PASS"
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_member_result(text, "mine")

    def test_duplicate_own_id_is_violation(self) -> None:
        text = (
            "ACP-QUEUE-ITEM: solo\nACP-MODE: PASS\n\n"
            "ACP-QUEUE-ITEM: solo\nACP-MODE: PASS"
        )
        with self.assertRaises(QueueProtocolViolation):
            parse_queue_member_result(text, "solo")


if __name__ == "__main__":
    unittest.main()
