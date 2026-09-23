"""连环画创作审稿的领域规则测试。"""

import unittest
from datetime import date, datetime

from domain import (
    DomainError,
    License,
    OutdatedOpinionError,
    PermissionDenied,
    ReviewSystem,
    StaleVersionError,
    StateError,
)

EDITOR = {"actor": "王编辑", "role": "编辑"}
NOW = datetime(2026, 9, 22, 10, 0, 0)


def lic(scopes=("国内",), expires=None, confidential=None):
    return License(
        publication_scopes=frozenset(scopes),
        expires_at=expires,
        confidential_until=confidential,
    )


class ReviewSystemTest(unittest.TestCase):
    def setUp(self):
        self.system = ReviewSystem(clock=lambda: NOW)
        self.source = self.system.register_source(
            title="平型关战斗详报", citation="《八路军战史》第12页",
            license=lic(("国内", "海外")), **EDITOR)
        self.segment = self.system.create_segment(
            title="夜袭", text="九月二十五日拂晓, 部队进入伏击阵地。", actor="学员甲")
        self.design = self.system.create_design(
            name="连长", brief="三十岁, 左眉有疤。", actor="学员甲")
        self.page = self.system.create_page(
            title="第3页 伏击",
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲")
        self.panel = self.system.add_panel(
            self.page.id, page_version=1, index=1,
            sketch_ref="sketches/3-1.png", actor="学员甲")

    # ----- 专业签署 -----

    def test_expert_signs_only_within_own_scope(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期应为9月25日", content="与战报一致", author="李专家",
            role="党史专家")
        self.assertEqual(opinion.state, "提出")
        with self.assertRaises(PermissionDenied):
            self.system.sign_opinion(
                target_kind="page", target_id=self.page.id, scope="史实",
                stance="x", content="作家越权签史实", author="赵作家", role="作家")
        with self.assertRaises(PermissionDenied):
            self.system.sign_opinion(
                target_kind="page", target_id=self.page.id, scope="画面",
                stance="x", content="学员不能签署", author="学员甲", role="学员")

    # ----- 意见生命周期 -----

    def test_adopted_then_withdrawn_opinion_cannot_be_deleted(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="文字",
            stance="精简旁白", content="旁白过长", author="赵作家", role="作家")
        self.system.adopt_opinion(opinion.id, **EDITOR)
        self.system.withdraw_opinion(opinion.id, **EDITOR)
        self.assertEqual(self.system.opinions[opinion.id].state, "撤回")
        with self.assertRaises(PermissionDenied):
            self.system.delete_opinion(opinion.id, **EDITOR)
        self.assertIn(opinion.id, self.system.opinions)  # 记录留档

    def test_pending_opinion_can_be_deleted(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="画面",
            stance="调整构图", content="主体偏小", author="孙画家", role="画家")
        self.system.delete_opinion(opinion.id, **EDITOR)
        self.assertNotIn(opinion.id, self.system.opinions)

    # ----- 冲突会审与重大事实门禁 -----

    def _conflicting_fact_issue(self):
        first = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="依战报", author="李专家", role="党史专家")
        second = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="依回忆录", author="陈专家", role="军史专家")
        self.system.adopt_opinion(first.id, **EDITOR)
        self.system.adopt_opinion(second.id, **EDITOR)
        (issue,) = self.system.issues.values()
        return issue

    def test_conflicting_adopted_opinions_enter_joint_review(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        self.assertEqual(issue.state, "待会审")
        self.assertTrue(issue.is_major_fact)
        self.assertEqual(self.system.pages[self.page.id].status, "联合会审")

    def test_major_fact_blocks_final_draft_until_closed(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        with self.assertRaises(StateError):
            self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.system.close_issue(issue.id, decision="以战报为准: 9月25日", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.assertEqual(self.system.pages[self.page.id].status, "精稿中")

    # ----- 学员异议 -----

    def test_objection_requires_registered_evidence(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        with self.assertRaises(DomainError):
            self.system.submit_objection(
                issue.id, content="日期还有第三种说法", evidence="",
                actor="学员甲", role="学员")
        with self.assertRaises(DomainError):
            self.system.submit_objection(
                issue.id, content="依据未登记", evidence="SRC-999",
                actor="学员甲", role="学员")

    def test_grounded_objection_reopens_closed_issue(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        issue = self._conflicting_fact_issue()
        self.system.close_issue(issue.id, decision="以战报为准", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.system.submit_objection(
            issue.id, content="另一份战报记载不同", evidence=self.source.id,
            actor="学员甲", role="学员")
        self.assertEqual(self.system.issues[issue.id].state, "待会审")
        self.assertEqual(self.system.pages[self.page.id].status, "联合会审")
        with self.assertRaises(StateError):
            self.system.transition_page(self.page.id, "精稿中", **EDITOR)

    # ----- 授权、保密期、出版范围 -----

    def test_expired_license_keeps_page_undeliverable(self):
        # 许可按页面版本引用的史料修订快照判断: 页面采用问题修订后才被阻断。
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), expires=date(2026, 9, 1)),
            base_revision=1, **EDITOR)
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 2},
            actor="学员甲", base_version=1)
        blockers = self.system.page_blockers(self.page.id, scope="国内")
        self.assertTrue(any("过期" in b for b in blockers))
        self.assertFalse(self.system.deliverable(self.page.id, scope="国内"))

    def test_license_is_judged_by_the_source_revision_page_adopts(self):
        # 页面仍引用r1时, r2收紧授权不溯及既往(但依赖陈旧本身不可交付)。
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), expires=date(2020, 1, 1)),
            base_revision=1, **EDITOR)
        stale_only = self.system.page_blockers(
            self.page.id, scope="国内", on=date(2026, 9, 22))
        self.assertTrue(any("依赖陈旧" in b for b in stale_only))
        self.assertFalse(any("过期" in b for b in stale_only))
        # panel_trace 也按引用的修订返回旧引文快照。
        trace = self.system.panel_trace(self.page.id, self.panel.id)
        self.assertEqual(trace["sources"][0]["revision"], 1)
        self.assertEqual(trace["sources"][0]["latest_revision"], 2)

    def test_confidentiality_and_scope_checked_per_page_version(self):
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), confidential=date(2026, 12, 31)),
            base_revision=1, **EDITOR)
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 2},
            actor="学员甲", base_version=1)
        blockers = self.system.page_blockers(self.page.id, scope="海外")
        self.assertTrue(any("保密期" in b for b in blockers))
        self.assertTrue(any("出版范围" in b for b in blockers))
        self.assertTrue(any("r2" in b for b in blockers))  # 原因标注页面采用的修订

    def test_page_with_blockers_cannot_reach_publishable(self):
        self.system.revise_source(
            self.source.id, citation=self.source.citation,
            license=lic(("国内",), expires=date(2026, 9, 1)),
            base_revision=1, **EDITOR)
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        with self.assertRaises(StateError):
            self.system.transition_page(self.page.id, "可出版", **EDITOR)

    # ----- 依赖陈旧 -----

    def test_stale_dependency_blocks_delivery_until_page_upgraded(self):
        self.system.new_segment_version(
            self.segment.id, text="九月二十五日拂晓, 全营进入伏击阵地。",
            actor="学员甲", base_version=1)
        blockers = self.system.page_blockers(self.page.id)
        self.assertTrue(any("依赖陈旧" in b for b in blockers))
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 2},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲", base_version=1)
        self.assertEqual(self.system.page_blockers(self.page.id), [])

    # ----- 并发与乱序 -----

    def test_concurrent_edit_on_stale_base_is_rejected(self):
        self.system.new_segment_version(
            self.segment.id, text="第二版", actor="学员甲", base_version=1)
        with self.assertRaises(StaleVersionError):
            self.system.new_segment_version(
                self.segment.id, text="第三版", actor="学员乙", base_version=1)

    def test_out_of_order_opinion_is_kept_but_flagged(self):
        self.system.new_segment_version(
            self.segment.id, text="第二版", actor="学员甲", base_version=1)
        opinion = self.system.sign_opinion(
            target_kind="script", target_id=self.segment.id, scope="文字",
            stance="恢复初版措辞", content="针对v1的点评迟到",
            author="赵作家", role="作家", target_version=1)
        self.assertTrue(opinion.outdated)
        self.assertIn(opinion.id, self.system.opinions)

    # ----- 追溯 -----

    def test_panel_trace_recovers_sources_text_version_and_deciders(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="依战报", author="李专家", role="党史专家")
        self.system.adopt_opinion(opinion.id, **EDITOR)
        trace = self.system.panel_trace(self.page.id, self.panel.id)
        self.assertEqual(trace["page_version"], 1)
        self.assertEqual(trace["sources"][0]["citation"], "《八路军战史》第12页")
        self.assertEqual(trace["scripts"][0]["version"], 1)
        self.assertIn("九月二十五日", trace["scripts"][0]["text"])
        self.assertIn("王编辑", trace["deciders"])
        self.assertEqual(trace["opinions"][0]["state"], "采纳")

    # ----- 批量导出 -----

    def _publishable_page(self, title, scopes):
        source = self.system.register_source(
            title=f"{title}史料", citation="出处", license=lic(scopes), **EDITOR)
        page = self.system.create_page(
            title=title,
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={source.id: 1},
            actor="学员甲")
        self.system.transition_page(page.id, "待评审", **EDITOR)
        self.system.transition_page(page.id, "精稿中", **EDITOR)
        self.system.transition_page(page.id, "可出版", **EDITOR)
        return page

    def test_export_contains_only_currently_permitted_content(self):
        domestic = self._publishable_page("仅限国内", ("国内",))
        global_page = self._publishable_page("可海外", ("国内", "海外"))
        draft = self.system.create_page(
            title="未完工", script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1}, actor="学员甲")
        result = self.system.export_batch(scope="海外", **EDITOR)
        exported_ids = {p["page_id"] for p in result["pages"]}
        self.assertEqual(exported_ids, {global_page.id})
        self.assertIn(domestic.id, result["excluded"])
        self.assertNotIn(draft.id, exported_ids)
        self.assertNotIn(draft.id, result["excluded"])  # 非可出版页面不进入候选

    def test_export_requires_editor_role(self):
        with self.assertRaises(PermissionDenied):
            self.system.export_batch(scope="国内", actor="学员甲", role="学员")


class OpinionVersionBindingTest(unittest.TestCase):
    """意见-版本绑定: 旧意见误采纳、跨版本假冲突、撤回/换版更新未结议题。"""

    def setUp(self):
        self.system = ReviewSystem(clock=lambda: NOW)
        self.source = self.system.register_source(
            title="平型关战斗详报", citation="《八路军战史》第12页",
            license=lic(("国内", "海外")), **EDITOR)
        self.segment = self.system.create_segment(
            title="夜袭", text="九月二十四日深夜出发。", actor="学员甲")
        self.design = self.system.create_design(
            name="连长", brief="三十岁, 左眉有疤。", actor="学员甲")
        self.page = self.system.create_page(
            title="第3页 伏击",
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲")

    def _sign(self, stance, *, author="李专家", role="党史专家",
              scope="史实", version=None):
        return self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope=scope,
            stance=stance, content=f"意见:{stance}", author=author, role=role,
            target_version=version)

    def _bump_page_to_v3(self):
        """模拟改到第三版: 依赖与页面各换一版, 页面最终为v3。"""
        self.system.new_segment_version(
            self.segment.id, text="九月二十五日拂晓进入阵地。",
            actor="学员甲", base_version=1)
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 2},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲", base_version=1)  # v2
        self.system.new_segment_version(
            self.segment.id, text="九月二十五日拂晓, 全营进入伏击阵地。",
            actor="学员甲", base_version=2)
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 3},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲", base_version=2)  # v3

    # ----- 旧意见误采纳 -----

    def test_old_version_opinion_cannot_be_adopted_against_new_draft(self):
        old = self._sign("日期为9月24日")  # 针对v1
        self._bump_page_to_v3()
        with self.assertRaises(OutdatedOpinionError):
            self.system.adopt_opinion(old.id, **EDITOR)
        with self.assertRaises(OutdatedOpinionError):
            self.system.reject_opinion(old.id, **EDITOR)
        self.assertEqual(self.system.opinions[old.id].state, "提出")
        self.assertTrue(self.system.opinions[old.id].outdated)

    def test_outdated_opinion_can_only_be_archived_or_reassigned(self):
        old = self._sign("日期为9月24日")
        self._bump_page_to_v3()
        # 转签到当前版本后才能采纳, 原意见保持留档。
        renewed = self.system.reassign_opinion(
            old.id, actor="李专家", role="党史专家")
        self.assertEqual(renewed.target_version, 3)
        self.assertEqual(renewed.reissued_from, old.id)
        self.assertIn(old.id, self.system.opinions)
        self.system.adopt_opinion(renewed.id, **EDITOR)
        self.assertEqual(self.system.opinions[renewed.id].state, "采纳")

    def test_reassign_must_be_same_expert_and_newer_version(self):
        old = self._sign("日期为9月24日")
        self._bump_page_to_v3()
        with self.assertRaises(PermissionDenied):
            self.system.reassign_opinion(
                old.id, actor="王编辑", role="编辑")
        with self.assertRaises(PermissionDenied):
            self.system.reassign_opinion(
                old.id, actor="陈专家", role="军史专家")
        with self.assertRaises(DomainError):
            self.system.reassign_opinion(
                old.id, actor="李专家", role="党史专家", target_version=1)

    def test_signing_freezes_dependency_snapshot(self):
        opinion = self._sign("日期为9月24日")
        self.assertEqual(opinion.dependency_snapshot["script_refs"],
                         {self.segment.id: 1})
        self.assertEqual(opinion.dependency_snapshot["source_refs"],
                         {self.source.id: 1})
        self._bump_page_to_v3()
        # 换版后旧意见快照不变。
        self.assertEqual(opinion.dependency_snapshot["script_refs"],
                         {self.segment.id: 1})

    # ----- 跨版本假冲突 -----

    def test_conflicts_hold_only_within_same_target_version(self):
        first = self._sign("日期为9月25日")
        self.system.adopt_opinion(first.id, **EDITOR)  # v1时采纳
        # 页面升v2后另一位专家针对v2给出相反结论并被采纳。
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲", base_version=1)
        second = self._sign("日期为9月24日", author="陈专家", role="军史专家")
        self.system.adopt_opinion(second.id, **EDITOR)  # 不与v1意见构成冲突
        self.assertEqual(self.system.issues, {})
        # 旧意见已随换版留档, 新意见独立存在, 页面无未结议题。
        self.assertTrue(self.system.opinions[first.id].outdated)
        self.assertEqual(
            self.system._open_issues_for_page(self.system.pages[self.page.id]), [])

    def test_same_version_conflict_still_detected_and_version_bound(self):
        first = self._sign("日期为9月25日")
        second = self._sign("日期为9月24日", author="陈专家", role="军史专家")
        self.system.adopt_opinion(first.id, **EDITOR)
        self.system.adopt_opinion(second.id, **EDITOR)
        (only,) = self.system.issues.values()
        self.assertEqual(only.target_version, 1)
        self.assertIn("v1", only.subject)
        # 第三版出现后, v1的议题在v2换版时即留档, 不再阻断新稿。
        self._bump_page_to_v3()
        self.assertEqual(only.state, "已关闭")
        self.assertEqual(only.superseded_by, 2)
        self.assertEqual(
            self.system._open_issues_for_page(self.system.pages[self.page.id]), [])

    # ----- 撤回/驳回更新未结议题 -----

    def test_withdraw_resolves_issue_when_conflict_disappears(self):
        first = self._sign("日期为9月25日")
        second = self._sign("日期为9月24日", author="陈专家", role="军史专家")
        self.system.adopt_opinion(first.id, **EDITOR)
        self.system.adopt_opinion(second.id, **EDITOR)
        (issue,) = self.system.issues.values()
        self.assertEqual(issue.state, "待会审")
        self.system.withdraw_opinion(second.id, **EDITOR)
        self.assertEqual(issue.state, "已关闭")
        self.assertIn("自动关闭", issue.decision)
        # 页面门禁随即解除。
        self.assertEqual(
            self.system._open_issues_for_page(self.system.pages[self.page.id]), [])

    def test_withdraw_one_side_keeps_issue_when_three_way_conflict(self):
        first = self._sign("日期为9月25日")
        second = self._sign("日期为9月24日", author="陈专家", role="军史专家")
        third = self._sign("日期为9月23日", author="张专家", role="党史专家")
        for opinion in (first, second, third):
            self.system.adopt_opinion(opinion.id, **EDITOR)
        (issue,) = self.system.issues.values()
        self.system.withdraw_opinion(third.id, **EDITOR)
        self.assertEqual(issue.state, "待会审")  # 25日与24日仍对立

    # ----- 换版与异议 -----

    def test_page_revision_archives_issues_and_objection_cannot_revive(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        first = self._sign("日期为9月25日")
        second = self._sign("日期为9月24日", author="陈专家", role="军史专家")
        self.system.adopt_opinion(first.id, **EDITOR)
        self.system.adopt_opinion(second.id, **EDITOR)
        (issue,) = self.system.issues.values()
        self.system.close_issue(issue.id, decision="以战报为准", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.system.new_page_version(
            self.page.id,
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲", base_version=1)
        # 对v1旧议题提异议: 留档但不重开, 新稿不被退回。
        self.system.submit_objection(
            issue.id, content="另有记载", evidence=self.source.id,
            actor="学员甲", role="学员")
        self.assertEqual(issue.state, "已关闭")
        self.assertEqual(self.system.pages[self.page.id].status, "精稿中")


class ExportSnapshotTest(unittest.TestCase):
    """导出冻结: 处理中途产生的新版本不得混入本批, 许可按导出日期与范围判断。"""

    def setUp(self):
        self.system = ReviewSystem(clock=lambda: NOW)
        self.source = self.system.register_source(
            title="战斗详报", citation="《战史》p12",
            license=lic(("国内", "海外"), expires=date(2026, 12, 31)), **EDITOR)
        self.segment = self.system.create_segment(
            title="夜袭", text="拂晓进入阵地。", actor="学员甲")
        self.design = self.system.create_design(
            name="连长", brief="三十岁。", actor="学员甲")
        self.page = self.system.create_page(
            title="第3页",
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲")
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self.system.transition_page(self.page.id, "可出版", **EDITOR)

    def _publish(self, page):
        self.system.transition_page(page.id, "待评审", **EDITOR)
        self.system.transition_page(page.id, "精稿中", **EDITOR)
        self.system.transition_page(page.id, "可出版", **EDITOR)

    def test_export_judged_on_request_date_and_scope(self):
        # 授权2026-12-31到期: 按导出日期2027-01-01判定为过期并标注r1。
        result = self.system.export_batch(
            scope="海外", on=date(2027, 1, 1), **EDITOR)
        self.assertEqual(result["pages"], [])
        reasons = result["excluded"][self.page.id]
        self.assertTrue(any("过期" in r and "r1" in r for r in reasons))

    def test_concurrent_page_revision_during_export_does_not_mix_snapshot(self):
        panel = self.system.add_panel(
            self.page.id, page_version=1, index=1,
            sketch_ref="sketches/3-1.png", actor="学员甲")
        # 导出开始后(快照已冻结)页面换版并向v1补画格: 本批仍按冻结的v1导出。
        def during(page, snapshot):
            self.assertEqual(snapshot["page_version"].version, 1)
            self.system.add_panel(
                page.id, page_version=1, index=2,
                sketch_ref="sketches/3-2-late.png", actor="学员乙")
            self.system.new_segment_version(
                self.segment.id, text="修订后的文字。",
                actor="学员乙", base_version=1)
            self.system.new_page_version(
                page.id,
                script_refs={self.segment.id: 2},
                design_refs={self.design.id: 1},
                source_refs={self.source.id: 1},
                actor="学员乙", base_version=1)

        result = self.system.export_batch(
            scope="海外", **EDITOR, _during_export=during)
        (exported,) = result["pages"]
        self.assertEqual(exported["page_version"], 1)  # 仍是冻结的v1
        self.assertEqual(exported["scripts"][0]["version"], 1)
        self.assertEqual([p["id"] for p in exported["panels"]], [panel.id])
        self.assertEqual(result["excluded"], {})
        self.assertEqual(self.system.pages[self.page.id].current.version, 2)

    def test_concurrent_source_revision_during_export_uses_frozen_license(self):
        # 导出开始后史料修订并收紧授权: 本批按页面引用的r1许可快照放行,
        # 引文也取r1快照; r2不影响本批。
        def during(page, snapshot):
            self.system.revise_source(
                self.source.id, citation="《战史》p12(撤回版)",
                license=lic(("国内",)),  # 去掉海外且无延期
                base_revision=1, **EDITOR)

        result = self.system.export_batch(
            scope="海外", **EDITOR, _during_export=during)
        (exported,) = result["pages"]
        self.assertEqual(exported["sources"][0]["revision"], 1)
        self.assertEqual(exported["sources"][0]["citation"], "《战史》p12")

        # 下一批导出: 页面引用r1已依赖陈旧, 被排除且原因标注对应版本。
        next_batch = self.system.export_batch(scope="海外", **EDITOR)
        reasons = next_batch["excluded"][self.page.id]
        self.assertTrue(any("依赖陈旧" in r and "r1" in r for r in reasons))

    def test_exclusion_reason_names_version_for_scope_mismatch(self):
        result = self.system.export_batch(scope="典藏", **EDITOR)
        reasons = result["excluded"][self.page.id]
        self.assertTrue(
            any("出版范围" in r and "r1" in r for r in reasons))

    def test_newly_publishable_page_after_snapshot_not_in_batch(self):
        second = self.system.create_page(
            title="第4页",
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
            actor="学员甲")

        def during(page, snapshot):
            self._publish(second)  # 导出处理中途才达到可出版

        result = self.system.export_batch(
            scope="海外", **EDITOR, _during_export=during)
        # 候选在开始时冻结, 第4页不混入; 且不被当作排除项。
        self.assertEqual([p["page_id"] for p in result["pages"]], [self.page.id])
        self.assertNotIn(second.id, result["excluded"])


if __name__ == "__main__":
    unittest.main()
