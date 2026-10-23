import importlib.util
import argparse
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = (Path(__file__).resolve().parents[2] / "skills" / "requesting-slack-bot-review") / "scripts" / "watch_review.py"
SPEC = importlib.util.spec_from_file_location("watch_review", MODULE_PATH)
watch_review = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = watch_review
SPEC.loader.exec_module(watch_review)


def snapshot(
    *, slack=(), reviews=(), comments=(), threads=(), ledger="github_review",
    remote_head=None, artifact_state="OPEN", merge_commit_sha=None,
    merged_at=None, closed_at=None, required_github_state=None,
):
    normalized_reviews = []
    for index, review in enumerate(reviews):
        item = dict(review)
        item.setdefault("html_url", f"https://example.test/reviews/{index}")
        normalized_reviews.append(item)
    result = {
        "request": {
            "head": "0425a4c55575abcdef",
            "expected_ledger": ledger,
            "acceptable_verdicts": ["approved", "with_findings"],
            "slack_reviewer": "U_REVIEWER",
            "requester_slack_id": "U_ME",
            "github_reviewer": "review-bot",
            "remote_head": remote_head,
            "artifact_state": artifact_state,
            "merge_commit_sha": merge_commit_sha,
            "merged_at": merged_at,
            "closed_at": closed_at,
        },
        "slack_messages": list(slack),
        "github_reviews": normalized_reviews,
        "github_comments": list(comments),
        "review_threads": list(threads),
    }
    if required_github_state is not None:
        result["request"]["required_github_review_state"] = required_github_state
    return result


class ReviewStateTest(unittest.TestCase):
    def test_progress_is_acknowledged_not_delivered(self):
        result = watch_review.classify(
            snapshot(slack=[{"user": "U_REVIEWER", "text": ":hourglass: working… 2m"}])
        )
        self.assertEqual((result.transport_state, result.review_verdict, result.terminal), ("acknowledged", "none", False))

    def test_stale_recap_does_not_deliver_current_head(self):
        result = watch_review.classify(
            snapshot(slack=[{"user": "U_REVIEWER", "text": "Review @ 04f42560e: approved"}])
        )
        self.assertEqual(result.transport_state, "stale")

    def test_own_sync_message_is_excluded(self):
        result = watch_review.classify(
            snapshot(slack=[{"user": "U_ME", "text": "Fixed at 0425a4c55; re-review"}])
        )
        self.assertEqual(result.transport_state, "silent")

    def test_slack_result_is_nonterminal_when_contract_requires_github_review(self):
        result = watch_review.classify(
            snapshot(slack=[{"user": "U_REVIEWER", "text": "0425a4c55 approved; no blockers"}])
        )
        self.assertEqual((result.transport_state, result.review_verdict, result.terminal), ("acknowledged", "approved", False))

    def test_missing_ledger_defaults_to_github_and_rejects_slack_completion(self):
        data = snapshot(slack=[{
            "user": "U_REVIEWER",
            "text": "0425a4c55 approved; no blockers",
        }])
        del data["request"]["expected_ledger"]
        result = watch_review.classify(data)
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("acknowledged", "approved", False),
        )

    def test_current_head_commented_review_body_reports_findings(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "P1 pagination finding",
            }])
        )
        self.assertEqual((result.transport_state, result.review_verdict, result.terminal), ("delivered", "with_findings", True))

    def test_prose_derived_verdict_names_the_phrase_that_drove_it(self):
        # 2026-09-16, upstream-project PR #6720: secondary-reviewer 提交了一条零发现的
        # COMMENTED 评审("✅ Review result: APPROVE"),但正文里一句
        # "remains a documented growth risk, with no demonstrated current
        # blocker" 让 FINDING_RE 命中 "blocker",判成 with_findings。
        # 没有结构化尾巴时判定靠散文,读的人应该能一眼看到是哪句话触发的,
        # 而不是只看到一个 with_findings 就去空翻一遍评审找不存在的发现。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": (
                    "Review result: APPROVE. Unbounded compaction remains a "
                    "documented growth risk, with no demonstrated current blocker."
                ),
                "html_url": "https://example.test/review/9",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNotNone(result.verdict_evidence)
        self.assertIn("blocker", result.verdict_evidence)

    def test_slack_delivered_prose_verdict_also_names_its_phrase(self):
        # Codex P2 (#52 discussion_r4023877292):Slack 消息走的是同一套散文
        # 启发式,证据却只在 GitHub COMMENTED 分支上填,于是同样一句
        # "no demonstrated current blocker" 从 Slack 过来时仍然是个不透明的
        # with_findings。
        result = watch_review.classify(
            snapshot(
                ledger="slack",
                slack=[{
                    "user": "U_REVIEWER",
                    "text": (
                        "Review @ 0425a4c55575abcdef: approve. Compaction remains a "
                        "documented growth risk, with no demonstrated current blocker."
                    ),
                    "permalink": "https://slack.test/p1",
                }],
            )
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNotNone(result.verdict_evidence)
        self.assertIn("blocker", result.verdict_evidence)

    def test_zero_finding_count_alone_still_leaves_the_verdict_to_prose(self):
        # Codex P2 (#52 discussion_r4023877303):只有 ACTIONABLE_FINDINGS: 0、
        # 没有 VERDICT: 时,判定仍然来自散文。按"有结构化字段就不给证据"去抑制,
        # 恰好抑制掉了这个字段本来要诊断的那一类。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "ACTIONABLE_FINDINGS: 0\nThere is a blocker in the migration path.",
                "html_url": "https://example.test/review/11",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNotNone(result.verdict_evidence)
        self.assertIn("blocker", result.verdict_evidence)

    def test_zero_count_metadata_is_not_mistaken_for_the_evidence(self):
        # secondary-reviewer MEDIUM (#52 pullrequestreview-5220269383):FINDING_RE 的
        # `finding` 会命中 `ACTIONABLE_FINDINGS` 这个字段名本身。text_verdict
        # 在跑散文启发式之前把这段元数据剥掉了,prose_verdict_evidence 没有,
        # 于是证据指向字段名而不是真正的触发句。
        #
        # 上一版的零计数测试是假绿:它的 blocker 恰好落在 40 字符摘录窗口里。
        # 这里把 blocker 推到窗口之外。
        filler = "Everything reviewed cleanly and the paths were inspected in detail. " * 2
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": f"ACTIONABLE_FINDINGS: 0\n{filler}There is a blocker in the migration path.",
                "html_url": "https://example.test/review/12",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIn("blocker", result.verdict_evidence)
        self.assertNotIn("ACTIONABLE_FINDINGS", result.verdict_evidence)

    def test_prose_override_of_a_provider_approval_carries_its_evidence(self):
        # example-reviewer MEDIUM:这是赌注最高的一条分支——它用关键词匹配去推翻一个
        # 明确的 provider APPROVED,读的人会看到"with_findings"却对着一条
        # GitHub 记为 approved 的评审,而没有任何东西指出是哪句话。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": (
                    "Unbounded compaction remains a documented growth risk, "
                    "with no demonstrated current blocker."
                ),
                "html_url": "https://example.test/review/13",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNotNone(result.verdict_evidence)
        self.assertIn("blocker", result.verdict_evidence)

    def test_an_approved_verdict_never_carries_finding_evidence(self):
        # example-reviewer LOW:证据在全文上重推,就可能给一个 approved 判定挂上一段
        # 读起来像"假触发报告"的文字。证据只属于关键词推出来的 with_findings。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "ACTIONABLE_FINDINGS: 0\nApproved. Finding #2 from the earlier round touched this file.",
                "html_url": "https://example.test/review/14",
            }])
        )
        self.assertEqual(result.review_verdict, "approved")
        self.assertIsNone(result.verdict_evidence)

    def test_a_negated_approval_verdict_also_names_its_phrase(self):
        # example-reviewer LOW 的另一半:NEGATIVE_APPROVE_RE 推出的 with_findings 之前
        # 拿到的是 null,读的人会理解成"这次判定不涉及关键词猜测"。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "This is not approved; please revisit the migration path.",
                "html_url": "https://example.test/review/15",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNotNone(result.verdict_evidence)
        self.assertIn("not approved", result.verdict_evidence)

    def test_a_single_line_structured_trailer_is_still_structured(self):
        # 2026-09-16,upstream-project #6719:secondary-reviewer 把整条尾巴发在一行上
        # ——"VERDICT: APPROVED ACTIONABLE_FINDINGS: 0 HEAD: <sha>"。两个模式
        # 都是行锚定的(^...$ 带 re.M),所以匹配不到,判定退回散文;而散文里
        # FINDING_RE 的 `finding` 又正好命中 `ACTIONABLE_FINDINGS` 这个词。
        #
        # 结果是最糟的一种:provider 记为 APPROVED、结构化字段写着 APPROVED 和
        # 0 条发现,watcher 却报 with_findings,并把那句批准声明当成证据引用。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": (
                    "Re-review clean. No actionable findings. "
                    "VERDICT: APPROVED ACTIONABLE_FINDINGS: 0 HEAD: 0425a4c55575abcdef"
                ),
                "html_url": "https://example.test/review/16",
            }])
        )
        self.assertEqual(result.review_verdict, "approved")
        self.assertIsNone(result.verdict_evidence)

    def test_keyword_derived_findings_always_carry_evidence(self):
        # secondary-reviewer LOW (#52 pullrequestreview-5220401000):review-state-contract.md
        # 承诺的 null 语义依赖这条不变式,而重构之后不变式其实已经成立了——
        # 文档反而写反了。把它钉成可检验的,而不是散文承诺:
        #   with_findings 且证据为 None  ⟺  判定来自结构化字段(或 provider 状态)
        keyword_derived = (
            "This is not approved; revisit the migration path.",
            "ACTIONABLE_FINDINGS: 0\nApproved but there is a blocker here.",
            "Compaction remains a growth risk, with no demonstrated current blocker.",
        )
        for body in keyword_derived:
            verdict, evidence = watch_review.classify_prose(body)
            self.assertEqual(verdict, "with_findings", body)
            self.assertIsNotNone(evidence, body)

        structured = (
            "All fine. ACTIONABLE_FINDINGS: 3",
            "All fine. VERDICT: WITH_FINDINGS",
        )
        for body in structured:
            verdict, evidence = watch_review.classify_prose(body)
            self.assertEqual(verdict, "with_findings", body)
            self.assertIsNone(evidence, body)

    def test_an_inline_finding_count_alone_is_authoritative(self):
        # 同理单独钉住 STRUCTURED_FINDINGS_RE 的去锚定:只要 VERDICT 在场就会
        # 短路,所以这里只给计数。行锚定时它会退回散文,而散文里 FINDING_RE 又
        # 命中字段名本身——判定碰巧仍是 with_findings,但会带上一段假证据。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "Reviewed and everything looks clean. ACTIONABLE_FINDINGS: 3",
                "html_url": "https://example.test/review/19",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNone(result.verdict_evidence)

    def test_an_inline_verdict_field_alone_is_authoritative(self):
        # 单独钉住 STRUCTURED_VERDICT_RE 的去锚定:上一条测试里 ACTIONABLE_FINDINGS
        # 也在同一行,所以即使 VERDICT 模式仍是行锚定的,零计数那条路径也能兜住
        # ——测试会通过,而那条正则的改动其实没被覆盖。这里只给 VERDICT。
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "Everything reads fine to me. VERDICT: WITH_FINDINGS",
                "html_url": "https://example.test/review/18",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNone(result.verdict_evidence)

    def test_a_single_line_trailer_reporting_findings_is_believed(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "Looks fine to me. VERDICT: WITH_FINDINGS ACTIONABLE_FINDINGS: 2 HEAD: 0425a4c5",
                "html_url": "https://example.test/review/17",
            }])
        )
        self.assertEqual(result.review_verdict, "with_findings")
        self.assertIsNone(result.verdict_evidence)

    def test_structured_verdict_needs_no_prose_evidence(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "All good.\nVERDICT: APPROVED\nACTIONABLE_FINDINGS: 0",
                "html_url": "https://example.test/review/10",
            }])
        )
        self.assertEqual(result.review_verdict, "approved")
        self.assertIsNone(result.verdict_evidence)

    def test_current_head_approval_is_terminal(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No blockers",
                "html_url": "https://example.test/review/1",
            }])
        )
        self.assertEqual((result.transport_state, result.review_verdict, result.terminal), ("delivered", "approved", True))

    def test_exact_head_github_review_without_url_is_watcher_error(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No blockers",
                "html_url": "",
            }])
        )
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("watcher_error", "none", False),
        )
        self.assertIn("html_url", result.reason)

    def test_exact_head_approved_review_without_body_is_watcher_error(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "   ",
                "html_url": "https://example.test/review/empty",
            }])
        )
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("watcher_error", "none", False),
        )
        self.assertIn("non-empty body", result.reason)

    def test_commented_zero_finding_review_waits_for_required_approved_action(self):
        result = watch_review.classify(snapshot(
            required_github_state="APPROVED",
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "No actionable findings.",
                "html_url": "https://example.test/review/commented",
            }],
        ))
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("delivered", "approved", False),
        )
        self.assertIn("required provider state is APPROVED", result.reason)

    def test_approved_action_satisfies_required_provider_state(self):
        result = watch_review.classify(snapshot(
            required_github_state="APPROVED",
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No actionable findings.",
            }],
        ))
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("delivered", "approved", True),
        )

    def test_findings_remain_terminal_before_required_approved_action(self):
        result = watch_review.classify(snapshot(
            required_github_state="APPROVED",
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "P1 actionable finding remains.",
            }],
        ))
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("delivered", "with_findings", True),
        )

    def test_required_github_state_rejects_slack_only_direct_snapshot(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            required_github_state="APPROVED",
        ))
        self.assertEqual(result.transport_state, "watcher_error")
        self.assertIn("GitHub-capable ledger", result.reason)

    def test_required_github_state_rejects_unknown_direct_value(self):
        result = watch_review.classify(snapshot(required_github_state="COMMENTED"))
        self.assertEqual(result.transport_state, "watcher_error")
        self.assertIn("must be APPROVED", result.reason)

    def test_pr2325_exact_review_identity_and_wording_are_approved(self):
        head = "c2b99501638873990c855d4900647c331f9c520b"
        data = snapshot(reviews=[{
            "user": "example-reviewer",
            "commit_id": head,
            "state": "APPROVED",
            "body": (
                "## Final review — exact head `c2b99501638873990c855d4900647c331f9c520b`\n"
                "APPROVE. No blocking findings.\n"
                "Non-blocking follow-up: consider consolidating the onboarding guard."
            ),
            "html_url": "https://github.com/example/project/pull/2325#pullrequestreview-4998961817",
        }], remote_head=head)
        data["request"]["head"] = head
        data["request"]["github_reviewer"] = "EXAMPLE-REVIEWER"
        result = watch_review.classify(data)
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("delivered", "approved", True),
        )

    def test_structured_verdict_is_authoritative(self):
        self.assertEqual(
            watch_review.text_verdict(
                "VERDICT: APPROVED\nACTIONABLE_FINDINGS: 0\nChecked finding detection."
            ),
            "approved",
        )
        self.assertEqual(
            watch_review.text_verdict(
                "VERDICT: APPROVED\nACTIONABLE_FINDINGS: 2"
            ),
            "with_findings",
        )

    def test_zero_actionable_findings_metadata_is_not_itself_a_finding(self):
        body = (
            "APPROVE — exact HEAD: `0425a4c55575abcdef`\n"
            "A positive actionable-finding count still returns with_findings.\n"
            "Real prose findings after zero metadata remain with_findings.\n"
            "ACTIONABLE_FINDINGS: 0"
        )
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": body,
        }]))
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("delivered", "approved", True),
        )

    def test_zero_finding_count_does_not_hide_same_sentence_contradiction(self):
        self.assertEqual(
            watch_review.text_verdict(
                "APPROVE, but P1 remains\nACTIONABLE_FINDINGS: 0"
            ),
            "with_findings",
        )

    def test_zero_finding_count_preserves_negated_approval_phrases(self):
        for approval in (
            "APPROVE - no actionable findings",
            "APPROVE - no blockers",
        ):
            self.assertEqual(
                watch_review.text_verdict(
                    f"{approval}\nACTIONABLE_FINDINGS: 0"
                ),
                "approved",
                approval,
            )

    def test_approved_review_with_findings_requires_triage(self):
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": "APPROVE, but P1 finding remains",
        }]))
        self.assertEqual((result.review_verdict, result.terminal), ("with_findings", True))

    def test_approved_review_with_current_thread_requires_triage(self):
        result = watch_review.classify(snapshot(
            reviews=[{
                "user": "review-bot", "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED", "body": "APPROVE",
            }],
            threads=[{
                "user": "review-bot", "commit_id": "0425a4c55575abcdef",
                "is_resolved": False, "is_outdated": False,
            }],
        ))
        self.assertEqual(result.review_verdict, "with_findings")

    def test_unrelated_github_reviewer_cannot_satisfy_request(self):
        result = watch_review.classify(snapshot(reviews=[{
            "user": "different-reviewer",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": "No blockers",
        }]))
        self.assertEqual((result.transport_state, result.review_verdict), ("silent", "none"))

    def test_missing_github_identity_rejects_github_capable_direct_snapshot(self):
        for ledger in ("github_review", "either"):
            with self.subTest(ledger=ledger):
                data = snapshot(
                    ledger=ledger,
                    comments=[{
                        "user": "unrelated-user",
                        "body": "Review @ 0425a4c55575abcdef: approved; no blockers",
                    }],
                )
                data["request"].pop("github_reviewer")
                result = watch_review.classify(data)
                self.assertEqual(
                    (result.transport_state, result.review_verdict),
                    ("watcher_error", "none"),
                )
                self.assertIn("github_reviewer", result.reason)

    def test_negative_chinese_approval_is_a_finding(self):
        result = watch_review.classify(
            snapshot(
                ledger="slack",
                slack=[{"user": "U_REVIEWER", "text": "0425a4c55 检查未通过，有一个问题"}],
            )
        )
        self.assertEqual((result.review_verdict, result.terminal), ("with_findings", True))

    def test_no_blockers_phrase_does_not_hide_later_finding(self):
        result = watch_review.classify(
            snapshot(
                ledger="slack",
                slack=[{"user": "U_REVIEWER", "text": "0425a4c55 no blockers in A, but P1 in B"}],
            )
        )
        self.assertEqual((result.review_verdict, result.terminal), ("with_findings", True))

    def test_no_issue_and_no_finding_phrases_are_approvals(self):
        for phrase in ("no issues", "no findings"):
            result = watch_review.classify(snapshot(
                ledger="slack",
                slack=[{"user": "U_REVIEWER", "text": f"0425a4c55 approved; {phrase}"}],
            ))
            self.assertEqual((result.review_verdict, result.terminal), ("approved", True))

    def test_resolved_historical_findings_do_not_override_approval(self):
        for phrase in (
            "Prior issue is fixed; approved",
            "All findings addressed; approved",
            "No actionable issues; approved",
            "APPROVE - no actionable findings; accepted tradeoff (not a finding)",
        ):
            result = watch_review.classify(snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": phrase,
            }]))
            self.assertEqual((result.review_verdict, result.terminal), ("approved", True), phrase)

    def test_numbered_resolved_findings_and_non_finding_observations_are_approved(self):
        body = (
            "## Review result: APPROVE\n\n"
            "Both prior findings are resolved and no new actionable issue was found.\n\n"
            "Finding 1 (spec contradiction) — fixed. The requirements now agree.\n"
            "Finding 2 (missing capability evidence) — fixed and independently verified.\n\n"
            "Two non-blocking observations, neither an actionable finding."
        )
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": body,
        }]))
        self.assertEqual((result.review_verdict, result.terminal), ("approved", True))

    def test_numbered_findings_require_an_explicit_resolved_status_clause(self):
        for body in (
            "Finding 1 — fixed.",
            "Finding #2 is resolved.",
            "Issue B has been addressed.",
        ):
            with self.subTest(body=body):
                result = watch_review.classify(snapshot(reviews=[{
                    "user": "review-bot",
                    "commit_id": "0425a4c55575abcdef",
                    "state": "APPROVED",
                    "body": body,
                }]))
                self.assertEqual(
                    (result.review_verdict, result.terminal),
                    ("approved", True),
                )

    def test_resolved_numbered_finding_does_not_hide_later_active_finding(self):
        for body in (
            "Finding 1 (old issue) — fixed. "
            "Two observations, neither an actionable finding, but P1 remains.",
            "Finding 1 — not fixed.",
            "Finding 1 — only partially fixed.",
            "Finding 2 must be fixed before merge.",
            "Finding 1 hasn't been addressed.",
            "Finding 1 still needs to be fixed.",
            "Finding 2 has yet to be addressed.",
            "Findings: P1 - the retry path must be addressed before merge.",
            "Neither finding is acceptable.",
        ):
            with self.subTest(body=body):
                result = watch_review.classify(snapshot(reviews=[{
                    "user": "review-bot",
                    "commit_id": "0425a4c55575abcdef",
                    "state": "APPROVED",
                    "body": body,
                }]))
                self.assertEqual(
                    (result.review_verdict, result.terminal),
                    ("with_findings", True),
                )

    def test_resolved_historical_finding_does_not_hide_new_finding(self):
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": "Prior issue is fixed, but P1 remains in the guard",
        }]))
        self.assertEqual((result.review_verdict, result.terminal), ("with_findings", True))

    def test_explicit_approval_verdict_is_not_polluted_by_explanatory_meta_text(self):
        body = (
            "Fifth-round review. APPROVE - no actionable findings. "
            "Verified that active finding and P1/finding/issue keywords still flip to with_findings."
        )
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": body,
        }]))
        self.assertEqual((result.review_verdict, result.terminal), ("approved", True))

    def test_explicit_approval_with_same_sentence_contradiction_has_findings(self):
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": "APPROVE - no actionable findings, but P1 remains",
        }]))
        self.assertEqual((result.review_verdict, result.terminal), ("with_findings", True))

    def test_edited_ack_message_can_deliver_final_slack_verdict(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            slack=[{
                "user": "U_REVIEWER",
                "text": "0425a4c55 APPROVED; no blockers",
                "acknowledgement": True,
            }],
        ))
        self.assertEqual((result.transport_state, result.review_verdict, result.terminal), ("delivered", "approved", True))

    def test_current_head_slack_reply_with_unknown_wording_is_delivered_nonterminal(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            slack=[{
                "user": "U_REVIEWER",
                "text": "Review for 0425a4c55 is complete; details are in the attached report",
            }],
        ))
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("delivered", "no_verdict", False),
        )

    def test_current_head_progress_message_remains_acknowledgement(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            slack=[{
                "user": "U_REVIEWER",
                "text": "Reviewing 0425a4c55 now; still working",
                "acknowledgement": True,
            }],
        ))
        self.assertEqual(
            (result.transport_state, result.review_verdict, result.terminal),
            ("acknowledged", "none", False),
        )

    def test_all_numeric_short_head_is_recognized(self):
        data = snapshot(ledger="slack", slack=[{"user": "U_REVIEWER", "text": "1234567 approved"}])
        data["request"]["head"] = "123456789abcdef0"
        result = watch_review.classify(data)
        self.assertEqual((result.transport_state, result.review_verdict), ("delivered", "approved"))

    def test_commented_body_can_express_approval(self):
        result = watch_review.classify(
            snapshot(reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED",
                "body": "Review complete; no blockers found",
            }])
        )
        self.assertEqual((result.review_verdict, result.terminal), ("approved", True))

    def test_unresolved_current_thread_makes_commented_review_findings(self):
        result = watch_review.classify(
            snapshot(
                reviews=[{
                    "user": "review-bot",
                    "commit_id": "0425a4c55575abcdef",
                    "state": "COMMENTED",
                    "body": "",
                }],
                threads=[{
                    "user": "review-bot",
                    "commit_id": "0425a4c55575abcdef",
                    "is_resolved": False,
                    "is_outdated": False,
                }],
            )
        )
        self.assertEqual(result.review_verdict, "with_findings")

    def test_slack_contract_ignores_current_github_approval(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            reviews=[{
                "user": "review-bot", "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED", "body": "No blockers",
            }],
        ))
        self.assertEqual(result.transport_state, "silent")

    def test_either_contract_prefers_slack_approval_over_github_no_verdict(self):
        result = watch_review.classify(snapshot(
            ledger="either",
            reviews=[{
                "user": "review-bot", "commit_id": "0425a4c55575abcdef",
                "state": "COMMENTED", "body": "Review complete",
            }],
            slack=[{
                "user": "U_REVIEWER", "text": "0425a4c55 approved; no blockers",
            }],
        ))
        self.assertEqual((result.source, result.review_verdict, result.terminal), ("slack", "approved", True))

    def test_issue_comment_cannot_satisfy_github_review_contract(self):
        result = watch_review.classify(
            snapshot(comments=[{
                "user": "review-bot",
                "body": "Review @ 0425a4c55 approved",
                "html_url": "https://example.test/comment/1",
            }])
        )
        self.assertEqual((result.transport_state, result.review_verdict, result.terminal), ("acknowledged", "approved", False))

    def test_remote_head_change_supersedes_request(self):
        result = watch_review.classify(snapshot(remote_head="ffffffffffffffff"))
        self.assertEqual(result.transport_state, "superseded")
        self.assertEqual(watch_review.terminal_exit_code(result), 4)

    def test_merged_pr_without_result_stops_watcher(self):
        result = watch_review.classify(snapshot(
            remote_head="0425a4c55575abcdef",
            artifact_state="MERGED",
            merge_commit_sha="2b039779f6bd7d48211a6e820c60483ce54328c5",
        ))
        self.assertEqual((result.transport_state, result.terminal), ("artifact_closed", False))
        self.assertEqual(watch_review.terminal_exit_code(result), 5)

    def test_exact_head_result_wins_even_if_pr_is_already_merged(self):
        result = watch_review.classify(snapshot(
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No blocking findings",
                "submitted_at": "2026-08-22T04:37:09Z",
            }],
            artifact_state="MERGED",
            merged_at="2026-08-22T04:52:16Z",
        ))
        self.assertEqual((result.transport_state, result.review_verdict), ("delivered", "approved"))

    def test_post_merge_exact_head_review_is_rejected(self):
        result = watch_review.classify(snapshot(
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No blocking findings",
                "submitted_at": "2026-08-22T05:00:00Z",
            }],
            artifact_state="MERGED",
            merged_at="2026-08-22T04:52:16Z",
        ))
        self.assertEqual((result.transport_state, result.review_verdict), ("artifact_closed", "none"))

    def test_post_merge_update_wins_over_pre_merge_submission_timestamp(self):
        result = watch_review.classify(snapshot(
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No blocking findings",
                "submitted_at": "2026-08-22T04:00:00Z",
                "updated_at": "2026-08-22T06:00:00Z",
            }],
            artifact_state="MERGED",
            merged_at="2026-08-22T05:00:00Z",
        ))
        self.assertEqual((result.transport_state, result.review_verdict), ("artifact_closed", "none"))

    def test_malformed_update_does_not_fall_back_to_valid_submission_time(self):
        result = watch_review.classify(snapshot(
            reviews=[{
                "user": "review-bot",
                "commit_id": "0425a4c55575abcdef",
                "state": "APPROVED",
                "body": "No blocking findings",
                "submitted_at": "2026-08-22T04:00:00Z",
                "updated_at": "not-a-time",
            }],
            artifact_state="MERGED",
            merged_at="2026-08-22T05:00:00Z",
        ))
        self.assertEqual((result.transport_state, result.review_verdict), ("watcher_error", "none"))
        self.assertIn("updated_at", result.reason)

    def test_post_close_edited_slack_result_is_rejected(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            slack=[{
                "user": "U_REVIEWER",
                "text": "0425a4c55 approved; no blockers",
                "ts": "1787373000.000000",
                "edited": {"ts": "1787375000.000000"},
            }],
            artifact_state="CLOSED",
            closed_at="2026-08-22T05:00:00Z",
        ))
        self.assertEqual(result.transport_state, "artifact_closed")

    def test_malformed_edited_shape_returns_structured_watcher_error(self):
        result = watch_review.classify(snapshot(
            ledger="slack",
            slack=[{
                "user": "U_REVIEWER",
                "text": "0425a4c55 approved; no blockers",
                "ts": "1787373000.000000",
                "edited": "1787375000.000000",
            }],
            artifact_state="CLOSED",
            closed_at="2026-08-22T05:00:00Z",
        ))
        self.assertEqual((result.transport_state, result.review_verdict), ("watcher_error", "none"))
        self.assertIn("edited", result.reason)

    def test_present_edit_marker_requires_valid_nested_timestamp(self):
        for edited in (None, {}, {"ts": None}, {"ts": ""}):
            with self.subTest(edited=edited):
                result = watch_review.classify(snapshot(
                    ledger="slack",
                    slack=[{
                        "user": "U_REVIEWER",
                        "text": "0425a4c55 approved; no blockers",
                        "ts": "1787373000.000000",
                        "edited": edited,
                    }],
                    artifact_state="CLOSED",
                    closed_at="2026-08-22T05:00:00Z",
                ))
                self.assertEqual(
                    (result.transport_state, result.review_verdict),
                    ("watcher_error", "none"),
                )
                self.assertIn("edited", result.reason)

    def test_missing_artifact_state_fails_closed_at_classifier_boundary(self):
        data = snapshot(reviews=[{
            "user": "review-bot",
            "commit_id": "0425a4c55575abcdef",
            "state": "APPROVED",
            "body": "No blocking findings",
        }])
        del data["request"]["artifact_state"]
        result = watch_review.classify(data)
        self.assertEqual((result.transport_state, result.review_verdict), ("watcher_error", "none"))
        self.assertIn("artifact_state", result.reason)

    def test_unknown_artifact_state_fails_closed_at_classifier_boundary(self):
        result = watch_review.classify(snapshot(artifact_state="UNKNOWN"))
        self.assertEqual((result.transport_state, result.review_verdict), ("watcher_error", "none"))
        self.assertIn("artifact_state", result.reason)

    def test_missing_slack_reviewer_fails_closed_for_slack_ledger(self):
        data = snapshot(
            ledger="slack",
            slack=[{
                "user": "U_OTHER",
                "text": "0425a4c55 approved; no blockers",
            }],
        )
        data["request"]["slack_reviewer"] = None
        result = watch_review.classify(data)
        self.assertEqual((result.transport_state, result.review_verdict), ("watcher_error", "none"))
        self.assertIn("slack_reviewer", result.reason)

    def test_structured_commit_prefix_shorter_than_seven_is_rejected(self):
        result = watch_review.classify(snapshot(reviews=[{
            "user": "review-bot", "commit_id": "0",
            "state": "APPROVED", "body": "No blockers",
        }]))
        self.assertEqual((result.transport_state, result.review_verdict), ("silent", "none"))

    def test_one_shot_exit_codes_distinguish_nonterminal_and_superseded(self):
        acknowledged = watch_review.Result("acknowledged", "none", False)
        superseded = watch_review.Result("superseded", "none", False)
        artifact_closed = watch_review.Result("artifact_closed", "none", False)
        approved = watch_review.Result("delivered", "approved", True)
        self.assertEqual(watch_review.one_shot_exit_code(acknowledged), 1)
        self.assertEqual(watch_review.one_shot_exit_code(superseded), 4)
        self.assertEqual(watch_review.one_shot_exit_code(artifact_closed), 5)
        self.assertEqual(watch_review.one_shot_exit_code(approved), 0)

    def test_pr35_two_round_ledger_replay(self):
        first_head = "338caa8355fe1ecd723aa110d7dc2ccca6f67993"
        final_head = "5a8406fa319550979487983babb5bed68fa55af1"
        first = snapshot(reviews=[{
            "user": "review-bot", "commit_id": first_head, "state": "APPROVED",
            "body": "APPROVE. Non-blocking findings: link/copy drift; env -S is skipped.",
        }], remote_head=first_head)
        first["request"]["head"] = first_head
        first_result = watch_review.classify(first)
        self.assertEqual(first_result.review_verdict, "with_findings")

        final = snapshot(reviews=[
            {"user": "review-bot", "commit_id": first_head, "state": "APPROVED", "body": "APPROVE with findings"},
            {"user": "review-bot", "commit_id": final_head, "state": "APPROVED", "body": "APPROVE; no must-fix"},
        ], remote_head=final_head)
        final["request"]["head"] = final_head
        final_result = watch_review.classify(final)
        self.assertEqual((final_result.review_verdict, final_result.terminal), ("approved", True))

    def test_paginated_github_arrays_are_flattened(self):
        completed = mock.Mock(stdout=json.dumps([[{"id": 1}], [{"id": 2}]]))
        with mock.patch.object(watch_review.subprocess, "run", return_value=completed) as run:
            self.assertEqual(watch_review.gh_json_paginated("endpoint"), [{"id": 1}, {"id": 2}])
        self.assertIn("--paginate", run.call_args.args[0])

    def test_slack_replies_follow_next_cursor(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                return json.dumps(self.payload).encode()

        responses = [
            Response({"ok": True, "messages": [{"ts": "1"}], "response_metadata": {"next_cursor": "next"}}),
            Response({"ok": True, "messages": [{"ts": "2"}], "response_metadata": {"next_cursor": ""}}),
        ]
        with mock.patch.object(watch_review.urllib.request, "urlopen", side_effect=responses) as urlopen:
            self.assertEqual(watch_review.slack_replies("token", "channel", "thread"), [{"ts": "1"}, {"ts": "2"}])
        self.assertEqual(urlopen.call_count, 2)

    def test_review_threads_follow_graphql_cursor(self):
        page_one = {
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-2"},
                "nodes": [],
            }}}}
        }
        page_two = {
            "data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{
                    "isResolved": False,
                    "isOutdated": False,
                    "comments": {"nodes": [{
                        "author": {"login": "review-bot"},
                        "body": "finding",
                        "pullRequestReview": {"commit": {"oid": "0425a4c55575abcdef"}},
                    }]},
                }],
            }}}}
        }
        responses = [mock.Mock(stdout=json.dumps(page_one)), mock.Mock(stdout=json.dumps(page_two))]
        with mock.patch.object(watch_review.subprocess, "run", side_effect=responses) as run:
            threads = watch_review.gh_review_threads("owner/repo", 34)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["user"], "review-bot")
        self.assertIn("cursor=cursor-2", run.call_args_list[1].args[0])

    def test_github_only_live_snapshot_does_not_require_slack_token(self):
        args = argparse.Namespace(
            github_only=True,
            slack_token_env="MISSING_TEST_TOKEN",
            repo="owner/repo",
            pr=37,
            head="a" * 40,
            expected_ledger="github_review",
            acceptable_verdict=["approved", "with_findings"],
            slack_reviewer=None,
            requester_slack_id=None,
            github_reviewer="review-bot",
            channel=None,
            thread_ts=None,
        )
        pull = {
            "head": {"sha": "a" * 40}, "state": "closed",
            "merged_at": "2026-08-22T04:52:16Z", "merge_commit_sha": "b" * 40,
        }
        review = {
            "user": {"login": "review-bot"},
            "commit_id": "a" * 40,
            "state": "APPROVED",
            "body": "No blockers",
            "html_url": "https://example.test/review",
            "submitted_at": "2026-08-22T04:37:09Z",
        }
        with mock.patch.dict(watch_review.os.environ, {}, clear=True), \
             mock.patch.object(watch_review, "gh_json", return_value=pull), \
             mock.patch.object(watch_review, "gh_json_paginated", side_effect=[[review], []]), \
             mock.patch.object(watch_review, "gh_review_threads", return_value=[]), \
             mock.patch.object(watch_review, "slack_replies") as slack_replies:
            data = watch_review.live_snapshot(args)
        slack_replies.assert_not_called()
        result = watch_review.classify(data)
        self.assertEqual((result.review_verdict, result.terminal), ("approved", True))
        self.assertEqual(data["request"]["artifact_state"], "MERGED")

    def test_live_snapshot_ignores_reviews_at_or_below_provider_watermark(self):
        head = "a" * 40
        args = argparse.Namespace(
            github_only=True,
            slack_token_env="MISSING_TEST_TOKEN",
            repo="owner/repo",
            pr=37,
            head=head,
            expected_ledger="github_review",
            acceptable_verdict=["approved", "with_findings"],
            required_github_review_state="APPROVED",
            slack_reviewer=None,
            requester_slack_id=None,
            github_reviewer="review-bot",
            channel=None,
            thread_ts=None,
            after_review_id=10,
        )
        old_finding = {
            "id": 10, "user": {"login": "review-bot"}, "commit_id": head,
            "state": "COMMENTED", "body": "Rejected finding",
            "html_url": "https://example.test/reviews/old",
        }
        correction = {
            "id": 11, "user": {"login": "review-bot"}, "commit_id": head,
            "state": "APPROVED", "body": "No actionable findings.",
            "html_url": "https://example.test/reviews/new",
        }
        with mock.patch.object(
            watch_review, "gh_json", return_value={"head": {"sha": head}, "state": "open"}
        ), mock.patch.object(
            watch_review, "gh_json_paginated", side_effect=[[old_finding, correction], []]
        ), mock.patch.object(
            watch_review, "gh_review_threads", return_value=[]
        ):
            data = watch_review.live_snapshot(args)

        self.assertEqual(len(data["github_reviews"]), 1)
        self.assertEqual(data["github_reviews"][0]["body"], "No actionable findings.")
        self.assertEqual(watch_review.classify(data).review_verdict, "approved")

    def test_github_only_cli_requires_github_review_ledger(self):
        argv = [
            "watch_review.py", "--github-only", "--repo", "owner/repo",
            "--pr", "37", "--head", "a" * 40,
            "--github-reviewer", "review-bot", "--expected-ledger", "either",
        ]
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            watch_review.parse_args()

    def test_live_cli_defaults_to_github_review_ledger(self):
        argv = [
            "watch_review.py", "--repo", "owner/repo", "--pr", "37",
            "--head", "a" * 40, "--channel", "C1", "--thread-ts", "1.1",
            "--slack-reviewer", "U1", "--github-reviewer", "review-bot",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = watch_review.parse_args()
        self.assertEqual(args.expected_ledger, "github_review")

    def test_live_cli_accepts_same_head_review_watermark(self):
        argv = [
            "watch_review.py", "--github-only", "--repo", "owner/repo",
            "--pr", "37", "--head", "a" * 40,
            "--github-reviewer", "review-bot", "--after-review-id", "5061391713",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = watch_review.parse_args()
        self.assertEqual(args.after_review_id, 5061391713)

    def test_github_only_once_executes_without_slack_configuration(self):
        head = "a" * 40
        argv = [
            "watch_review.py", "--github-only", "--once",
            "--repo", "owner/repo", "--pr", "37", "--head", head,
            "--github-reviewer", "review-bot",
            "--expected-ledger", "github_review",
        ]
        review = {
            "user": {"login": "review-bot"}, "commit_id": head,
            "state": "APPROVED", "body": "No blockers",
            "html_url": "https://example.test/reviews/live",
        }
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(watch_review.os.environ, {}, clear=True), \
             mock.patch.object(watch_review, "gh_json", return_value={"head": {"sha": head}}), \
             mock.patch.object(watch_review, "gh_json_paginated", side_effect=[[review], []]), \
             mock.patch.object(watch_review, "gh_review_threads", return_value=[]), \
             mock.patch.object(watch_review, "slack_replies") as slack_replies, \
             mock.patch.object(watch_review, "emit") as emit:
            code = watch_review.main()
        self.assertEqual(code, 0)
        slack_replies.assert_not_called()
        self.assertTrue(emit.call_args.args[0].terminal)
        self.assertEqual(emit.call_args.args[0].monitoring_scope, "github_only")

    def test_github_only_nonterminal_result_declares_its_scope(self):
        head = "a" * 40
        pending = snapshot(remote_head=head)
        pending["request"]["head"] = head
        argv = [
            "watch_review.py", "--github-only", "--once",
            "--repo", "owner/repo", "--pr", "37", "--head", head,
            "--github-reviewer", "review-bot",
            "--expected-ledger", "github_review",
        ]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(
                 watch_review, "live_snapshot",
                 return_value=pending,
             ), \
             mock.patch.object(watch_review, "emit") as emit:
            code = watch_review.main()
        self.assertEqual(code, 1)
        result = emit.call_args.args[0]
        self.assertEqual(result.transport_state, "silent")
        self.assertEqual(result.monitoring_scope, "github_only")


if __name__ == "__main__":
    unittest.main()
