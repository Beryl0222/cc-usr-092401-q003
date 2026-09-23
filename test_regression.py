"""意见-版本绑定、同版冲突与导出快照的回归测试。

覆盖三类必须能复现的旧故障:
1. 旧版意见在页面换版后仍被采纳, 直接改变新稿状态(旧意见误采纳);
2. 旧版已采纳意见与新版意见被当作同范围对立, 产生跨版本假冲突;
3. 批量导出开始后混入处理中途产生的新版本/新许可/新议题(混合快照)。
"""

import unittest
from datetime import date, datetime

from domain import License, PermissionDenied, ReviewSystem, StateError

EDITOR = {"actor": "王编辑", "role": "编辑"}
NOW = datetime(2026, 9, 23, 10, 0, 0)


def lic(scopes=("国内", "海外"), expires=None, confidential=None):
    return License(
        publication_scopes=frozenset(scopes),
        expires_at=expires,
        confidential_until=confidential,
    )


class VersionBindingTest(unittest.TestCase):
    def setUp(self):
        self.system = ReviewSystem(clock=lambda: NOW)
        self.source = self.system.register_source(
            title="平型关战斗详报", citation="《八路军战史》第12页",
            license=lic(), **EDITOR)
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

    def _rev_page(self, **refs):
        defaults = dict(
            script_refs={self.segment.id: 1},
            design_refs={self.design.id: 1},
            source_refs={self.source.id: 1},
        )
        defaults.update(refs)
        return self.system.new_page_version(
            self.page.id, actor="学员甲",
            base_version=self.system.pages[self.page.id].current.version,
            **defaults)

    # ----- 故障一: 旧意见误采纳 -----

    def test_signature_keeps_target_version_and_dependency_snapshot(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期应为9月25日", content="与战报一致",
            author="李专家", role="党史专家")
        self.assertEqual(opinion.target_version, 1)
        self.assertFalse(opinion.outdated)
        self.assertEqual(opinion.snapshot["script_refs"], {self.segment.id: 1})
        self.assertEqual(opinion.snapshot["design_refs"], {self.design.id: 1})
        self.assertEqual(opinion.snapshot["source_refs"], {self.source.id: 1})

    def test_stale_opinion_cannot_be_adopted_onto_new_page_version(self):
        opinion = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期应为9月25日", content="第一版意见",
            author="李专家", role="党史专家")
        self._rev_page()  # 编辑部改到第二版
        self.assertEqual(self.system.pages[self.page.id].current.version, 2)

        # 过期意见只能留档, 不能直接改变新稿状态。
        with self.assertRaises(StateError) as ctx:
            self.system.adopt_opinion(opinion.id, **EDITOR)
        self.assertIn("已过期", str(ctx.exception))
        self.assertEqual(self.system.opinions[opinion.id].state, "提出")
        # 新稿不应被这条旧意见阻断。
        self.assertEqual(self.system.page_blockers(self.page.id), [])

    def test_stale_opinion_can_only_be_archived_or_countersigned(self):
        old = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期应为9月25日", content="第一版意见",
            author="李专家", role="党史专家")
        self._rev_page()
        with self.assertRaises(StateError):
            self.system.adopt_opinion(old.id, **EDITOR)

        # 转签到当前版本: 同专业专家重新签署, 旧意见留档。
        new = self.system.countersign_opinion(
            old.id, actor="李专家", role="党史专家")
        self.assertEqual(new.target_version, 2)
        self.assertEqual(new.countersigned_from, old.id)
        self.assertFalse(new.outdated)
        self.assertEqual(new.snapshot["page_version"], 2)
        self.assertEqual(self.system.opinions[old.id].state, "驳回")
        self.system.adopt_opinion(new.id, **EDITOR)
        self.assertEqual(self.system.opinions[new.id].state, "采纳")

        # 不能绕过转签目标: 只允许转签到当前版本。
        self._rev_page()
        with self.assertRaises(StateError):
            self.system.countersign_opinion(
                new.id, target_version=2, actor="李专家", role="党史专家")

    def test_countersign_requires_same_expert_scope(self):
        old = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="文字",
            stance="精简旁白", content="第一版意见",
            author="赵作家", role="作家")
        self._rev_page()
        # 转签本质是重新签署, 画家不能替作家转签文字意见。
        with self.assertRaises(PermissionDenied):
            self.system.countersign_opinion(
                old.id, actor="孙画家", role="画家")
        # 原专家可以转签, 新版本意见保持原专业范围。
        fresh = self.system.countersign_opinion(old.id)
        self.assertEqual(fresh.author, "赵作家")
        self.assertEqual(fresh.role, "作家")
        self.assertEqual(fresh.scope, "文字")

    def test_adopted_opinion_on_old_version_can_be_countersigned_forward(self):
        old = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期应为9月25日", content="第一版意见",
            author="李专家", role="党史专家")
        self.system.adopt_opinion(old.id, **EDITOR)
        self._rev_page()
        # 已采纳但绑定旧版的意见不能再改变新稿, 专家可把结论转签到当前版本,
        # 旧意见保持留档(仍不可删除), 新意见需编辑重新采纳。
        fresh = self.system.countersign_opinion(old.id)
        self.assertEqual(fresh.target_version, 2)
        self.assertEqual(fresh.state, "提出")
        self.assertEqual(self.system.opinions[old.id].state, "采纳")
        with self.assertRaises(PermissionDenied):
            self.system.delete_opinion(old.id, **EDITOR)
        self.system.adopt_opinion(fresh.id, **EDITOR)
        self.assertEqual(self.system.opinions[fresh.id].state, "采纳")
        self.assertEqual(self.system.page_blockers(self.page.id), [])

    # ----- 故障二: 跨版本假冲突 -----

    def test_old_adopted_opinion_does_not_conflict_with_new_version(self):
        first = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="依战报", author="李专家", role="党史专家")
        self.system.adopt_opinion(first.id, **EDITOR)  # v1 时采纳有效
        self._rev_page()
        second = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="第三版新意见", author="陈专家", role="军史专家")
        self.system.adopt_opinion(second.id, **EDITOR)

        # 两条意见分属 v1/v2, 不成立冲突, 页面不进会审、不被阻断。
        self.assertEqual(list(self.system.issues.values()), [])
        self.assertEqual(self.system.pages[self.page.id].status, "构思中")
        self.assertEqual(self.system.page_blockers(self.page.id), [])
        # 旧意见仍留档, 但记录绑定的是 v1。
        self.assertEqual(self.system.opinions[first.id].target_version, 1)

    def test_conflict_only_holds_within_same_version_and_scope(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        a = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="a", author="李专家", role="党史专家")
        b = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="b", author="陈专家", role="军史专家")
        c = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="画面",
            stance="构图左移", content="c", author="孙画家", role="画家")
        self.system.adopt_opinion(a.id, **EDITOR)
        self.system.adopt_opinion(b.id, **EDITOR)
        self.system.adopt_opinion(c.id, **EDITOR)  # 不同范围不参与冲突
        (issue,) = self.system.issues.values()
        self.assertEqual(issue.target_version, 1)
        self.assertEqual(set(issue.opinion_ids), {a.id, b.id})
        self.assertEqual(self.system.pages[self.page.id].status, "联合会审")

    def test_withdraw_or_reject_updates_open_issue(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        a = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="a", author="李专家", role="党史专家")
        b = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="b", author="陈专家", role="军史专家")
        self.system.adopt_opinion(a.id, **EDITOR)
        self.system.adopt_opinion(b.id, **EDITOR)
        issue = next(iter(self.system.issues.values()))

        self.system.withdraw_opinion(b.id, **EDITOR)
        self.assertEqual(issue.state, "已消解")
        self.assertIn("撤回", issue.resolution)
        # 对立消失, 页面不再被该议题阻断。
        self.assertEqual(self.system.page_blockers(self.page.id), [])
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)

    def test_page_revision_archives_old_issues_but_keeps_new_version_conflict(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        a = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="a", author="李专家", role="党史专家")
        b = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="b", author="陈专家", role="军史专家")
        self.system.adopt_opinion(a.id, **EDITOR)
        self.system.adopt_opinion(b.id, **EDITOR)
        old_issue = next(iter(self.system.issues.values()))

        # 改到第二版后, v1 争议随旧版留档, 页面可直接继续流程。
        self._rev_page()
        self.assertEqual(old_issue.state, "随版留档")
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)

        # 第二版出现新的对立意见, 成立的是新议题, 与旧议题互不干扰。
        c = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="地点为乔沟", content="c", author="李专家", role="党史专家")
        d = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="地点为白崖台", content="d", author="陈专家", role="军史专家")
        self.system.adopt_opinion(c.id, **EDITOR)
        self.system.adopt_opinion(d.id, **EDITOR)
        new_issues = [i for i in self.system.issues.values() if i.state == "待会审"]
        self.assertEqual(len(new_issues), 1)
        self.assertEqual(new_issues[0].target_version, 2)
        self.assertEqual(old_issue.state, "随版留档")
        self.assertTrue(any("事实争议" in p for p in self.system.page_blockers(self.page.id)))

    def test_dependency_issue_is_scoped_to_referenced_version(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        x = self.system.sign_opinion(
            target_kind="script", target_id=self.segment.id, scope="文字",
            stance="措辞失实", content="v1脚本争议", author="赵作家", role="作家",
            target_version=1)
        y = self.system.sign_opinion(
            target_kind="script", target_id=self.segment.id, scope="文字",
            stance="措辞属实", content="v1脚本争议", author="文学编辑", role="文学编辑",
            target_version=1)
        self.system.adopt_opinion(x.id, **EDITOR)
        self.system.adopt_opinion(y.id, **EDITOR)
        issue = next(iter(self.system.issues.values()))
        self.assertEqual(issue.target_version, 1)
        self.assertTrue(self.system._open_issues_for_page(
            self.system.pages[self.page.id]))

        # 脚本与页面都升级后, 旧版议题留档, 不再阻断新稿。
        self.system.new_segment_version(
            self.segment.id, text="九月二十五日拂晓, 全营进入伏击阵地。",
            actor="学员甲", base_version=1)
        self._rev_page(script_refs={self.segment.id: 2})
        self.assertEqual(issue.state, "随版留档")
        self.assertEqual(self.system.page_blockers(self.page.id), [])

    def test_archived_issue_cannot_be_reopened_by_objection(self):
        self.system.transition_page(self.page.id, "待评审", **EDITOR)
        a = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月25日", content="a", author="李专家", role="党史专家")
        b = self.system.sign_opinion(
            target_kind="page", target_id=self.page.id, scope="史实",
            stance="日期为9月24日", content="b", author="陈专家", role="军史专家")
        self.system.adopt_opinion(a.id, **EDITOR)
        self.system.adopt_opinion(b.id, **EDITOR)
        issue = next(iter(self.system.issues.values()))
        self.system.close_issue(issue.id, decision="以战报为准", **EDITOR)
        self.system.transition_page(self.page.id, "精稿中", **EDITOR)
        self._rev_page()
        # 已关闭的 v1 议题不随换版阻断新稿, 但旧议题的异议也不能把新稿退回会审。
        self.assertEqual(issue.state, "已关闭")
        self.assertEqual(self.system.page_blockers(self.page.id), [])
        with self.assertRaises(StateError):
            self.system.submit_objection(
                issue.id, content="旧材料新说法", evidence=self.source.id,
                actor="学员甲", role="学员")
        self.assertEqual(self.system.pages[self.page.id].status, "精稿中")


class ExportSnapshotTest(unittest.TestCase):
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
        self.system.add_panel(
            self.page.id, page_version=1, index=1,
            sketch_ref="sketches/3-1.png", actor="学员甲")
        for target in ("待评审", "精稿中", "可出版"):
            self.system.transition_page(self.page.id, target, **EDITOR)

    def _result(self, **kw):
        return self.system.export_batch(
            scope="海外", actor="王编辑", role="编辑", **kw)

    def test_license_judged_by_export_date_and_scope_with_versioned_reason(self):
        ok = self._result(on=date(2026, 9, 23))
        self.assertEqual([p["page_id"] for p in ok["pages"]], [self.page.id])
        self.assertEqual(ok["exported_on"], "2026-09-23")

        expired = self._result(on=date(2027, 1, 1))
        self.assertEqual(expired["pages"], [])
        entry = expired["excluded"][self.page.id]
        self.assertEqual(entry["page_version"], 1)
        self.assertTrue(any("过期" in r and "页面v1" in r for r in entry["reasons"]))

        domestic_only = self.system.export_batch(
            scope="星际发行", on=date(2026, 9, 23), **EDITOR)
        entry = domestic_only["excluded"][self.page.id]
        self.assertTrue(any("出版范围" in r for r in entry["reasons"]))

    def test_export_does_not_mix_versions_produced_mid_flight(self):
        """复现并发改稿的混合快照: 冻结之后发生的一切都不得进入本次导出。"""

        def mutate(sys):
            # 另一路处理在导出开始后同时推进: 脚本v2、页面v2、新画格、
            # 史料许可改成过期、并在v2上制造事实争议。
            sys.new_segment_version(
                sys_page_seg[0], text="拂晓全营进入阵地。",
                actor="学员乙", base_version=1)
            sys.new_page_version(
                sys_page_page[0],
                script_refs={sys_page_seg[0]: 2},
                design_refs={sys_page_dsn[0]: 1},
                source_refs={sys_page_src[0]: 1},
                actor="学员乙", base_version=1)
            sys.add_panel(
                sys_page_page[0], page_version=2, index=2,
                sketch_ref="sketches/3-2.png", actor="学员乙")
            sys.revise_source(
                sys_page_src[0], citation="《战史》p12(修订)",
                license=lic(("国内",), expires=date(2020, 1, 1)),
                base_revision=1, **EDITOR)
            o1 = sys.sign_opinion(
                target_kind="page", target_id=sys_page_page[0], scope="史实",
                stance="日期25", content="", author="李专家", role="党史专家")
            o2 = sys.sign_opinion(
                target_kind="page", target_id=sys_page_page[0], scope="史实",
                stance="日期24", content="", author="陈专家", role="军史专家")
            sys.adopt_opinion(o1.id, **EDITOR)
            sys.adopt_opinion(o2.id, **EDITOR)

        sys_page_src = [self.source.id]
        sys_page_seg = [self.segment.id]
        sys_page_dsn = [self.design.id]
        sys_page_page = [self.page.id]

        class ConcurrentSystem(ReviewSystem):
            def _export_snapshot(self, page_ids):
                frozen = super()._export_snapshot(page_ids)
                mutate(self)
                return frozen

        concurrent = ConcurrentSystem(clock=lambda: NOW)
        # 与原系统共享全部活动表: 突变经由子类发生, 原系统随后导出时应看到新状态。
        concurrent.sources = self.system.sources
        concurrent.segments = self.system.segments
        concurrent.designs = self.system.designs
        concurrent.pages = self.system.pages
        concurrent.opinions = self.system.opinions
        concurrent.issues = self.system.issues
        result = concurrent.export_batch(
            scope="海外", actor="王编辑", role="编辑")

        # 本次导出严格停留在开始时的 v1: 没有 v2、没有新画格、没有 r2、
        # 没被事后出现的许可与议题排除。
        (exported,) = result["pages"]
        self.assertEqual(exported["page_version"], 1)
        self.assertEqual([p["id"] for p in exported["panels"]],
                         [pnl.id for pnl in self.page.versions[0].panels])
        self.assertEqual(exported["sources"][0]["revision"], 1)
        self.assertEqual(result["excluded"], {})

        # 而处理完成后再次导出, 看到的才是新状态: v2 被陈旧依赖/许可/议题排除。
        after = self.system.export_batch(
            scope="海外", actor="王编辑", role="编辑")
        self.assertEqual(after["pages"], [])
        entry = after["excluded"][self.page.id]
        self.assertEqual(entry["page_version"], 2)
        reasons = " ".join(entry["reasons"])
        self.assertIn("过期", reasons)
        self.assertIn("事实争议", reasons)
        self.assertIn("页面v2", reasons)


if __name__ == "__main__":
    unittest.main()
