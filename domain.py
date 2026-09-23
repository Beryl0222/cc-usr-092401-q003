"""连环画创作审稿的领域模型与规则。

五条主线:
1. 版本互引: 脚本段落、人物设定、分镜页均按版本演进, 页面版本记录其采用的依赖版本,
   依赖陈旧即不可交付。
2. 专业签署: 党史专家/作家/画家等只签署自己专业范围内的意见; 意见签署时绑定目标版本
   及其依赖(脚本/设定/史料修订)快照, 采纳前再次核对当前版本, 过期意见只能留档或转签。
3. 冲突会审: 同一对象、同一目标版本、同一专业范围内结论冲突的已采纳意见才进入联合会审;
   意见撤回/驳回或页面换版后, 未结议题随之消解或随旧版留档; 学员可提交有依据的异议重开。
4. 授权检查: 素材授权、保密期、出版范围随页面版本在交付与批量导出时检查。
5. 全程追溯: 任一画格可反查采用的史料、文字版本与决定人。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count


# ---------- 错误 ----------


class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作。"""


class StaleVersionError(DomainError):
    """基于陈旧版本的修改, 需刷新后重试。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


# ---------- 角色与状态词汇 ----------

EXPERT_SCOPES = {
    "党史专家": "史实",
    "军史专家": "史实",
    "作家": "文字",
    "文学编辑": "文字",
    "画家": "画面",
    "美术编辑": "画面",
}
EDITOR_ROLES = {"编辑"}
TRAINEE = "学员"

OPINION_STATES = ("提出", "采纳", "驳回", "撤回")
ISSUE_STATES = ("待会审", "已关闭", "已消解", "随版留档")
PAGE_STATES = ("构思中", "待评审", "联合会审", "精稿中", "可出版")
PAGE_TRANSITIONS = {
    "构思中": {"待评审"},
    "待评审": {"联合会审", "精稿中"},
    "联合会审": {"精稿中"},
    "精稿中": {"可出版"},
    "可出版": set(),
}


# ---------- 实体 ----------


@dataclass(frozen=True)
class License:
    """素材授权: 出版范围、授权截止日、保密期截止日。"""

    publication_scopes: frozenset
    expires_at: date | None = None
    confidential_until: date | None = None

    def problems(self, scope: str | None, on: date) -> list[str]:
        problems = []
        if self.expires_at is not None and on > self.expires_at:
            problems.append(f"授权已于{self.expires_at.isoformat()}过期")
        if self.confidential_until is not None and on <= self.confidential_until:
            problems.append(f"保密期至{self.confidential_until.isoformat()}届满")
        if scope is not None and scope not in self.publication_scopes:
            problems.append(f"出版范围不含「{scope}」")
        return problems


@dataclass
class HistoricalSource:
    id: str
    title: str
    citation: str
    revision: int
    license: License
    history: list = field(default_factory=list)


@dataclass
class ScriptVersion:
    version: int
    text: str
    author: str
    created_at: datetime


@dataclass
class ScriptSegment:
    id: str
    title: str
    versions: list = field(default_factory=list)

    @property
    def current(self) -> ScriptVersion:
        return self.versions[-1]


@dataclass
class CharacterDesign:
    id: str
    name: str
    versions: list = field(default_factory=list)  # 元素为 dict(version, brief, author, created_at)

    @property
    def current(self) -> dict:
        return self.versions[-1]


@dataclass
class Panel:
    id: str
    index: int
    sketch_ref: str
    page_version: int


@dataclass
class PageVersion:
    version: int
    script_refs: dict  # 脚本段落 id -> 采用的版本号
    design_refs: dict  # 人物设定 id -> 采用的版本号
    source_refs: dict  # 史料 id -> 采用的修订号
    panels: list = field(default_factory=list)
    author: str = ""
    created_at: datetime | None = None


@dataclass
class Page:
    id: str
    title: str
    status: str = "构思中"
    versions: list = field(default_factory=list)
    history: list = field(default_factory=list)

    @property
    def current(self) -> PageVersion:
        return self.versions[-1]


@dataclass
class Opinion:
    id: str
    target_kind: str  # page / script / design / source
    target_id: str
    target_version: int
    scope: str  # 史实 / 文字 / 画面
    stance: str  # 结论标识, 用于冲突检测
    content: str
    author: str
    role: str
    state: str = "提出"
    decided_by: str | None = None
    outdated: bool = False  # 针对的版本已被更新(乱序点评)
    # 签署时冻结的版本依据: 页面意见保存该页版本采用的脚本/设定/史料修订;
    # 依赖对象意见保存当时页面版本与对象版本的对应关系。
    snapshot: dict = field(default_factory=dict)
    countersigned_from: str | None = None  # 由哪条过期意见转签而来
    history: list = field(default_factory=list)


@dataclass
class Objection:
    id: str
    author: str
    content: str
    evidence: str  # 依据的史料 id
    created_at: datetime


@dataclass
class Issue:
    id: str
    target_kind: str
    target_id: str
    target_version: int  # 冲突只在同一目标版本内成立
    scope: str
    subject: str
    is_major_fact: bool
    opinion_ids: list = field(default_factory=list)
    state: str = "待会审"
    resolution: str | None = None  # 消解/留档原因
    decision: str | None = None
    decided_by: str | None = None
    objections: list = field(default_factory=list)
    history: list = field(default_factory=list)


# ---------- 审稿系统 ----------


class ReviewSystem:
    """内存中的审稿领域服务; clock 可注入以便测试。"""

    def __init__(self, clock=None):
        self._clock = clock or datetime.now
        self._seq = count(1)
        self.sources: dict[str, HistoricalSource] = {}
        self.segments: dict[str, ScriptSegment] = {}
        self.designs: dict[str, CharacterDesign] = {}
        self.pages: dict[str, Page] = {}
        self.opinions: dict[str, Opinion] = {}
        self.issues: dict[str, Issue] = {}

    # ----- 基础设施 -----

    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._now().date()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq)}"

    @staticmethod
    def _require_editor(role: str):
        if role not in EDITOR_ROLES:
            raise PermissionDenied(f"角色「{role}」无权执行编辑操作")

    def _get(self, table: dict, key: str, kind: str):
        try:
            return table[key]
        except KeyError:
            raise NotFoundError(f"{kind}不存在: {key}") from None

    # ----- 史料与授权 -----

    def register_source(self, *, title: str, citation: str, license: License,
                        actor: str, role: str) -> HistoricalSource:
        self._require_editor(role)
        source = HistoricalSource(
            id=self._new_id("SRC"), title=title, citation=citation,
            revision=1, license=license,
        )
        source.history.append(f"{self._now().isoformat()} {actor} 登记史料")
        self.sources[source.id] = source
        return source

    def revise_source(self, source_id: str, *, citation: str, license: License,
                      actor: str, role: str, base_revision: int) -> HistoricalSource:
        self._require_editor(role)
        source = self._get(self.sources, source_id, "史料")
        if base_revision != source.revision:
            raise StaleVersionError(
                f"史料{source_id}当前修订为r{source.revision}, 基于r{base_revision}的修改被拒绝")
        source.citation = citation
        source.license = license
        source.revision += 1
        source.history.append(f"{self._now().isoformat()} {actor} 修订至r{source.revision}")
        return source

    # ----- 脚本段落 -----

    def create_segment(self, *, title: str, text: str, actor: str) -> ScriptSegment:
        segment = ScriptSegment(id=self._new_id("SEG"), title=title)
        segment.versions.append(ScriptVersion(1, text, actor, self._now()))
        self.segments[segment.id] = segment
        return segment

    def new_segment_version(self, segment_id: str, *, text: str, actor: str,
                            base_version: int) -> ScriptVersion:
        segment = self._get(self.segments, segment_id, "脚本段落")
        if base_version != segment.current.version:
            raise StaleVersionError(
                f"脚本段落{segment_id}当前为v{segment.current.version}, "
                f"基于v{base_version}的修改被拒绝")
        version = ScriptVersion(segment.current.version + 1, text, actor, self._now())
        segment.versions.append(version)
        return version

    # ----- 人物设定 -----

    def create_design(self, *, name: str, brief: str, actor: str) -> CharacterDesign:
        design = CharacterDesign(id=self._new_id("DSN"), name=name)
        design.versions.append({
            "version": 1, "brief": brief, "author": actor,
            "created_at": self._now(),
        })
        self.designs[design.id] = design
        return design

    def new_design_version(self, design_id: str, *, brief: str, actor: str,
                           base_version: int) -> dict:
        design = self._get(self.designs, design_id, "人物设定")
        if base_version != design.current["version"]:
            raise StaleVersionError(
                f"人物设定{design_id}当前为v{design.current['version']}, "
                f"基于v{base_version}的修改被拒绝")
        version = {
            "version": design.current["version"] + 1, "brief": brief,
            "author": actor, "created_at": self._now(),
        }
        design.versions.append(version)
        return version

    # ----- 分镜页 -----

    def create_page(self, *, title: str, script_refs: dict, design_refs: dict,
                    source_refs: dict, actor: str) -> Page:
        page = Page(id=self._new_id("PG"), title=title)
        self.pages[page.id] = page
        self._append_page_version(
            page, script_refs=script_refs, design_refs=design_refs,
            source_refs=source_refs, actor=actor)
        return page

    def _validate_refs(self, script_refs, design_refs, source_refs):
        for seg_id, version in script_refs.items():
            segment = self._get(self.segments, seg_id, "脚本段落")
            if not 1 <= version <= segment.current.version:
                raise NotFoundError(f"脚本段落{seg_id}没有版本v{version}")
        for design_id, version in design_refs.items():
            design = self._get(self.designs, design_id, "人物设定")
            if not 1 <= version <= design.current["version"]:
                raise NotFoundError(f"人物设定{design_id}没有版本v{version}")
        for source_id, revision in source_refs.items():
            source = self._get(self.sources, source_id, "史料")
            if not 1 <= revision <= source.revision:
                raise NotFoundError(f"史料{source_id}没有修订r{revision}")

    def _append_page_version(self, page: Page, *, script_refs, design_refs,
                             source_refs, actor) -> PageVersion:
        self._validate_refs(script_refs, design_refs, source_refs)
        version = PageVersion(
            version=len(page.versions) + 1,
            script_refs=dict(script_refs),
            design_refs=dict(design_refs),
            source_refs=dict(source_refs),
            author=actor,
            created_at=self._now(),
        )
        page.versions.append(version)
        page.history.append(
            f"{self._now().isoformat()} {actor} 提交页面v{version.version}")
        # 换版后, 针对旧页面版本的未结议题不再约束新稿, 随旧版留档。
        self._archive_issues_for_page_version(page, version.version)
        return version

    def new_page_version(self, page_id: str, *, script_refs: dict, design_refs: dict,
                         source_refs: dict, actor: str, base_version: int) -> PageVersion:
        page = self._get(self.pages, page_id, "分镜页")
        if base_version != page.current.version:
            raise StaleVersionError(
                f"分镜页{page_id}当前为v{page.current.version}, "
                f"基于v{base_version}的修改被拒绝")
        return self._append_page_version(
            page, script_refs=script_refs, design_refs=design_refs,
            source_refs=source_refs, actor=actor)

    def add_panel(self, page_id: str, *, page_version: int, index: int,
                  sketch_ref: str, actor: str) -> Panel:
        page = self._get(self.pages, page_id, "分镜页")
        version = next((v for v in page.versions if v.version == page_version), None)
        if version is None:
            raise NotFoundError(f"分镜页{page_id}没有版本v{page_version}")
        panel = Panel(
            id=self._new_id("PN"), index=index,
            sketch_ref=sketch_ref, page_version=page_version,
        )
        version.panels.append(panel)
        page.history.append(
            f"{self._now().isoformat()} {actor} 在v{page_version}添加画格{panel.id}")
        return panel

    # ----- 专业签署与意见生命周期 -----

    def _target_current(self, target_kind: str, target_id: str):
        if target_kind == "page":
            return self._get(self.pages, target_id, "分镜页").current.version
        if target_kind == "script":
            return self._get(self.segments, target_id, "脚本段落").current.version
        if target_kind == "design":
            return self._get(self.designs, target_id, "人物设定").current["version"]
        if target_kind == "source":
            return self._get(self.sources, target_id, "史料").revision
        raise DomainError(f"未知的点评对象类型: {target_kind}")

    def _sign_snapshot(self, target_kind: str, target_id: str, target_version: int) -> dict:
        """签署时冻结版本依据。

        页面意见: 保存该页版本实际采用的脚本版本、设定版本、史料修订号。
        依赖意见(script/design/source): 保存当时引用该版本的各页面版本,
        便于采纳前核对页面是否已经换版。
        """
        if target_kind == "page":
            page = self._get(self.pages, target_id, "分镜页")
            page_version = next(
                (v for v in page.versions if v.version == target_version), None)
            if page_version is None:
                raise NotFoundError(f"分镜页{target_id}没有版本v{target_version}")
            return {
                "kind": "page",
                "page_version": target_version,
                "script_refs": dict(page_version.script_refs),
                "design_refs": dict(page_version.design_refs),
                "source_refs": dict(page_version.source_refs),
            }
        snapshot = {"kind": target_kind, "version": target_version, "pages": {}}
        for page in self.pages.values():
            refs = self._page_refs_for(page.current, target_kind)
            if target_id in refs and refs[target_id] == target_version:
                snapshot["pages"][page.id] = page.current.version
        return snapshot

    @staticmethod
    def _page_refs_for(page_version: PageVersion, target_kind: str) -> dict:
        if target_kind == "script":
            return page_version.script_refs
        if target_kind == "design":
            return page_version.design_refs
        if target_kind == "source":
            return page_version.source_refs
        return {}

    @staticmethod
    def _page_refs_for_frozen(frozen_page: dict, target_kind: str) -> dict:
        if target_kind == "script":
            return frozen_page["script_refs"]
        if target_kind == "design":
            return frozen_page["design_refs"]
        if target_kind == "source":
            return frozen_page["source_refs"]
        return {}

    def sign_opinion(self, *, target_kind: str, target_id: str, scope: str,
                     stance: str, content: str, author: str, role: str,
                     target_version: int | None = None) -> Opinion:
        if role not in EXPERT_SCOPES:
            raise PermissionDenied(f"角色「{role}」不能签署专业意见")
        if EXPERT_SCOPES[role] != scope:
            raise PermissionDenied(
                f"{role}只能签署「{EXPERT_SCOPES[role]}」范围内的意见, 不能签署「{scope}」")
        current = self._target_current(target_kind, target_id)
        if target_version is None:
            target_version = current
        elif not 1 <= target_version <= current:
            raise NotFoundError(f"{target_kind}{target_id}没有版本v{target_version}")
        opinion = Opinion(
            id=self._new_id("OP"), target_kind=target_kind, target_id=target_id,
            target_version=target_version, scope=scope, stance=stance,
            content=content, author=author, role=role,
            outdated=(target_version != current),
            snapshot=self._sign_snapshot(target_kind, target_id, target_version),
        )
        stamp = self._now().isoformat()
        if opinion.outdated:
            opinion.history.append(
                f"{stamp} {author}({role}) 针对v{target_version}提出(当前v{current}, 标记过期)")
        else:
            opinion.history.append(f"{stamp} {author}({role}) 提出")
        self.opinions[opinion.id] = opinion
        return opinion

    def _opinion(self, opinion_id: str) -> Opinion:
        return self._get(self.opinions, opinion_id, "意见")

    def _opinion_current_version(self, opinion: Opinion) -> int:
        return self._target_current(opinion.target_kind, opinion.target_id)

    def _assert_adoptable(self, opinion: Opinion):
        """采纳前再次核对: 只有仍绑定当前版本的意见可以改变新稿状态。"""
        current = self._opinion_current_version(opinion)
        if opinion.target_version != current:
            raise StateError(
                f"意见{opinion.id}针对{opinion.target_kind}{opinion.target_id}"
                f"v{opinion.target_version}, 当前为v{current}, 已过期; "
                "只能留档或由原专业专家转签到新版本, 不能直接采纳改变新稿状态")

    def adopt_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        self._require_editor(role)
        opinion = self._opinion(opinion_id)
        if opinion.state != "提出":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能采纳")
        self._assert_adoptable(opinion)
        opinion.state = "采纳"
        opinion.decided_by = actor
        opinion.outdated = False
        opinion.history.append(f"{self._now().isoformat()} {actor} 采纳")
        self._detect_conflict(opinion)
        return opinion

    def reject_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        self._require_editor(role)
        opinion = self._opinion(opinion_id)
        if opinion.state != "提出":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能驳回")
        opinion.state = "驳回"
        opinion.decided_by = actor
        opinion.history.append(f"{self._now().isoformat()} {actor} 驳回(留档)")
        # 驳回迟到的旧版意见同样可能使同版议题失去对立, 顺带复核。
        self._resolve_issues_for_opinion(opinion)
        return opinion

    def withdraw_opinion(self, opinion_id: str, *, actor: str, role: str) -> Opinion:
        opinion = self._opinion(opinion_id)
        if role not in EDITOR_ROLES and actor != opinion.author:
            raise PermissionDenied("只有编辑或意见作者本人可以撤回意见")
        if opinion.state != "采纳":
            raise StateError(f"意见{opinion_id}当前为「{opinion.state}」, 不能撤回")
        opinion.state = "撤回"
        opinion.decided_by = actor
        opinion.history.append(
            f"{self._now().isoformat()} {actor} 撤回(原已采纳, 记录留档)")
        self._resolve_issues_for_opinion(opinion)
        return opinion

    def countersign_opinion(self, opinion_id: str, *, target_version: int | None = None,
                            actor: str | None = None, role: str | None = None,
                            content: str | None = None) -> Opinion:
        """把过期意见转签到新版本: 同专业专家重新签署, 旧意见仅留档。

        未被采纳的旧意见转签后标记留档; 已曾采纳(含撤回)的意见不得删除也不改动状态,
        新意见通过 countersigned_from 记录来源。
        """
        old = self._opinion(opinion_id)
        current = self._opinion_current_version(old)
        if old.target_version == current:
            raise StateError(
                f"意见{opinion_id}仍针对当前版本v{current}, 无需转签, 可直接走采纳流程")
        target_version = current if target_version is None else target_version
        if not 1 <= target_version <= current:
            raise NotFoundError(
                f"{old.target_kind}{old.target_id}没有版本v{target_version}")
        if target_version != current:
            raise StateError("转签只能转签到当前版本, 再次过期的意见应继续留档")
        signer = actor or old.author
        signer_role = role or old.role
        new = self.sign_opinion(
            target_kind=old.target_kind, target_id=old.target_id, scope=old.scope,
            stance=old.stance, content=content if content is not None else old.content,
            author=signer, role=signer_role, target_version=target_version)
        new.countersigned_from = old.id
        new.history.append(
            f"{self._now().isoformat()} 由旧意见{old.id}(v{old.target_version})转签至v{target_version}")
        # 旧意见留档: 未决的标注过期关闭, 曾被采纳的保持原状态不可删除。
        if old.state == "提出":
            old.state = "驳回"
            old.decided_by = signer
            old.history.append(
                f"{self._now().isoformat()} 转签至{new.id}, 旧意见留档不再采纳")
        else:
            old.history.append(
                f"{self._now().isoformat()} 其结论已转签为{new.id}, 本记录随v{old.target_version}留档")
        return new

    def delete_opinion(self, opinion_id: str, *, actor: str, role: str) -> None:
        opinion = self._opinion(opinion_id)
        if opinion.state in ("采纳", "撤回"):
            raise PermissionDenied(
                f"意见{opinion_id}曾被采纳(当前「{opinion.state}」), 任何人不得删除, 记录留档")
        if role not in EDITOR_ROLES and actor != opinion.author:
            raise PermissionDenied("只有编辑或意见作者本人可以删除未采纳的意见")
        del self.opinions[opinion_id]

    # ----- 冲突会审 -----

    @staticmethod
    def _issue_key(opinion: Opinion):
        return (opinion.target_kind, opinion.target_id,
                opinion.target_version, opinion.scope)

    def _active_peers(self, opinion: Opinion) -> list[Opinion]:
        """同一对象、同一目标版本、同一范围内结论对立的已采纳意见。"""
        return [
            other for other in self.opinions.values()
            if other.id != opinion.id
            and other.state == "采纳"
            and other.target_kind == opinion.target_kind
            and other.target_id == opinion.target_id
            and other.target_version == opinion.target_version
            and other.scope == opinion.scope
            and other.stance != opinion.stance
        ]

    def _detect_conflict(self, opinion: Opinion) -> Issue | None:
        peers = self._active_peers(opinion)
        if not peers:
            return None
        key = self._issue_key(opinion)
        for issue in self.issues.values():
            if (issue.target_kind, issue.target_id,
                    issue.target_version, issue.scope) == key \
                    and issue.state == "待会审":
                if opinion.id not in issue.opinion_ids:
                    issue.opinion_ids.append(opinion.id)
                    issue.history.append(
                        f"{self._now().isoformat()} 意见{opinion.id}并入会审")
                return issue
        issue = Issue(
            id=self._new_id("IS"),
            target_kind=opinion.target_kind,
            target_id=opinion.target_id,
            target_version=opinion.target_version,
            scope=opinion.scope,
            subject=(f"{opinion.target_kind}:{opinion.target_id}"
                     f"v{opinion.target_version} 的{opinion.scope}结论冲突"),
            is_major_fact=(opinion.scope == "史实"),
            opinion_ids=[p.id for p in peers] + [opinion.id],
        )
        issue.history.append(
            f"{self._now().isoformat()} 冲突成立(v{opinion.target_version}), 进入联合会审")
        self.issues[issue.id] = issue
        if opinion.target_kind == "page":
            page = self.pages[opinion.target_id]
            if page.status == "待评审":
                page.status = "联合会审"
                page.history.append(
                    f"{self._now().isoformat()} 因议题{issue.id}转入联合会审")
        return issue

    def _issue_still_conflicted(self, issue: Issue) -> list[Opinion]:
        """议题内仍处于采纳态、且版本仍为当前版本的对立意见。"""
        members = [self.opinions.get(oid) for oid in issue.opinion_ids]
        members = [o for o in members if o is not None and o.state == "采纳"]
        stances = {o.stance for o in members}
        if len(stances) < 2:
            return []
        # 绑定版本必须仍是对象当前版本, 否则该议题只约束旧版。
        current = self._target_current(issue.target_kind, issue.target_id)
        if issue.target_version != current:
            return []
        return members

    def _resolve_issues_for_opinion(self, opinion: Opinion):
        """意见撤回/驳回后, 失去对立或版本过期的未结议题自动消解。"""
        for issue in list(self.issues.values()):
            if issue.state != "待会审":
                continue
            if opinion.id not in issue.opinion_ids:
                continue
            current = self._target_current(issue.target_kind, issue.target_id)
            if issue.target_version != current:
                self._archive_issue(
                    issue, "随版留档",
                    f"目标已更新至v{current}, 议题随v{issue.target_version}留档")
                continue
            if not self._issue_still_conflicted(issue):
                self._archive_issue(
                    issue, "已消解",
                    f"意见{opinion.id}变更为「{opinion.state}」, 同版同范围不再存在对立结论")

    def _archive_issue(self, issue: Issue, state: str, reason: str):
        issue.state = state
        issue.resolution = reason
        issue.history.append(f"{self._now().isoformat()} {reason}")

    def _archive_issues_for_page_version(self, page: Page, new_version: int):
        """页面换版: 旧页面版本议题与不再被任何现版页面采用的旧依赖议题随版留档。"""
        page_version = next(v for v in page.versions if v.version == new_version)
        for issue in list(self.issues.values()):
            if issue.state != "待会审":
                continue
            if issue.target_kind == "page" and issue.target_id == page.id:
                if issue.target_version < new_version:
                    self._archive_issue(
                        issue, "随版留档",
                        f"页面换版至v{new_version}, 议题随v{issue.target_version}留档, 不阻断新稿")
                continue
            if self._issue_matches_page_version(issue, page, page_version):
                continue
            # 新稿不再采用受争议的旧依赖版本; 若没有任何现版页面仍采用它, 议题留档。
            if any(
                self._issue_matches_page_version(issue, other, other.current)
                for other in self.pages.values()
            ):
                continue
            if issue.target_version != self._target_current(
                    issue.target_kind, issue.target_id):
                self._archive_issue(
                    issue, "随版留档",
                    f"页面v{new_version}不再采用{issue.target_kind}{issue.target_id}"
                    f"v{issue.target_version}, 无现版页面引用, 议题随旧版留档")

    def close_issue(self, issue_id: str, *, decision: str, actor: str,
                    role: str) -> Issue:
        self._require_editor(role)
        issue = self._get(self.issues, issue_id, "议题")
        if issue.state != "待会审":
            raise StateError(f"议题{issue_id}当前为「{issue.state}」, 不能关闭")
        if not decision:
            raise DomainError("关闭议题必须给出会审结论")
        issue.state = "已关闭"
        issue.decision = decision
        issue.decided_by = actor
        issue.history.append(f"{self._now().isoformat()} {actor} 关闭: {decision}")
        return issue

    def submit_objection(self, issue_id: str, *, content: str, evidence: str,
                         actor: str, role: str) -> Objection:
        if role != TRAINEE:
            raise PermissionDenied(f"角色「{role}」不能提交学员异议")
        issue = self._get(self.issues, issue_id, "议题")
        if not evidence:
            raise DomainError("异议必须附史料依据")
        self._get(self.sources, evidence, "史料")  # 依据必须指向已登记史料
        # 只随旧版留档的议题不再介入当前稿件, 异议应针对新版本重新提出。
        current = self._target_current(issue.target_kind, issue.target_id)
        if issue.state == "随版留档" or issue.target_version != current:
            raise StateError(
                f"议题{issue_id}针对v{issue.target_version}(当前v{current}), 不约束当前版本, "
                "不能重开; 请针对当前版本提交新意见")
        if issue.state == "已消解":
            # 对立已经消失的议题不能仅凭异议复活, 需要新版本上的新意见重新成立冲突。
            raise StateError(
                f"议题{issue_id}已因对立结论消失而消解, 异议不能重开; "
                "请针对当前版本提交新意见")
        objection = Objection(
            id=self._new_id("OBJ"), author=actor, content=content,
            evidence=evidence, created_at=self._now(),
        )
        issue.objections.append(objection)
        if issue.state == "已关闭":
            issue.state = "待会审"
            issue.history.append(
                f"{self._now().isoformat()} {actor} 提交有依据异议, 议题重开")
            if issue.is_major_fact and issue.target_kind == "page":
                page = self.pages[issue.target_id]
                if page.status in ("精稿中", "可出版"):
                    page.status = "联合会审"
                    page.history.append(
                        f"{self._now().isoformat()} 重大事实议题{issue.id}重开, 退回联合会审")
        else:
            issue.history.append(
                f"{self._now().isoformat()} {actor} 提交异议{objection.id}")
        return objection

    # ----- 页面状态机与交付门禁 -----

    def _issue_matches_page_version(self, issue: Issue, page: Page,
                                    page_version: PageVersion) -> bool:
        """议题只在同一目标版本、且页面当前仍采用该版本时约束页面。"""
        if issue.target_kind == "page":
            return issue.target_id == page.id and issue.target_version == page_version.version
        refs = self._page_refs_for(page_version, issue.target_kind)
        return issue.target_id in refs and refs[issue.target_id] == issue.target_version

    def _open_issues_for_page(self, page: Page, *, major_only: bool = False,
                              fact_only: bool = False,
                              page_version: PageVersion | None = None) -> list:
        version = page_version or page.current
        result = []
        for issue in self.issues.values():
            if issue.state != "待会审":
                continue
            if not self._issue_matches_page_version(issue, page, version):
                continue
            if major_only and not issue.is_major_fact:
                continue
            if fact_only and issue.scope != "史实":
                continue
            result.append(issue)
        return result

    def _stale_dependencies(self, page: Page,
                            page_version: PageVersion | None = None,
                            latest_refs: dict | None = None) -> list[str]:
        current = page_version or page.current
        problems = []
        for seg_id, version in current.script_refs.items():
            latest = latest_refs["script"][seg_id] if latest_refs else \
                self.segments[seg_id].current.version
            if version != latest:
                problems.append(f"脚本段落{seg_id}依赖陈旧: 采用v{version}, 当前v{latest}")
        for design_id, version in current.design_refs.items():
            latest = latest_refs["design"][design_id] if latest_refs else \
                self.designs[design_id].current["version"]
            if version != latest:
                problems.append(f"人物设定{design_id}依赖陈旧: 采用v{version}, 当前v{latest}")
        for source_id, revision in current.source_refs.items():
            latest = latest_refs["source"][source_id] if latest_refs else \
                self.sources[source_id].revision
            if revision != latest:
                problems.append(f"史料{source_id}依赖陈旧: 采用r{revision}, 当前r{latest}")
        return problems

    def _page_problems(self, page: Page, *, scope: str | None, on: date,
                       page_version: PageVersion | None = None,
                       latest_refs: dict | None = None,
                       licenses: dict | None = None,
                       issues: list | None = None) -> list[str]:
        """页面在指定版本/日期/范围快照下的阻断原因, 供交付门禁与批量导出共用。"""
        version = page_version or page.current
        tag = f"(页面v{version.version})"
        if issues is None:
            open_issues = self._open_issues_for_page(
                page, fact_only=True, page_version=version)
        else:
            open_issues = [
                i for i in issues
                if i.scope == "史实"
                and self._issue_matches_page_version(i, page, version)
            ]
        problems = [
            f"事实争议未关闭: {issue.id}({issue.subject}){tag}"
            for issue in open_issues
        ]
        problems.extend(self._stale_dependencies(page, version, latest_refs))
        for source_id in version.source_refs:
            source = self.sources[source_id]
            license_ = licenses.get(source_id) if licenses else source.license
            problems.extend(
                f"史料{source_id}: {problem}{tag}"
                for problem in license_.problems(scope, on)
            )
        return problems

    def page_blockers(self, page_id: str, *, scope: str | None = None,
                      on: date | None = None) -> list[str]:
        """返回页面当前不可交付的原因; 空列表表示可交付。"""
        page = self._get(self.pages, page_id, "分镜页")
        on = on or self._today()
        return self._page_problems(page, scope=scope, on=on)

    def deliverable(self, page_id: str, *, scope: str | None = None,
                    on: date | None = None) -> bool:
        return not self.page_blockers(page_id, scope=scope, on=on)

    def transition_page(self, page_id: str, target: str, *, actor: str,
                        role: str) -> Page:
        self._require_editor(role)
        page = self._get(self.pages, page_id, "分镜页")
        if target not in PAGE_TRANSITIONS.get(page.status, set()):
            raise StateError(f"页面{page_id}不能从「{page.status}」转入「{target}」")
        if target == "精稿中":
            majors = self._open_issues_for_page(page, major_only=True)
            if majors:
                raise StateError(
                    "重大事实未关闭, 不能转入精稿: "
                    + ", ".join(issue.id for issue in majors))
        if target == "可出版":
            blockers = self.page_blockers(page_id)
            if blockers:
                raise StateError("页面不可交付, 不能转入可出版: " + "; ".join(blockers))
        page.status = target
        page.history.append(f"{self._now().isoformat()} {actor} 转入「{target}」")
        return page

    # ----- 追溯与导出 -----

    def panel_trace(self, page_id: str, panel_id: str) -> dict:
        page = self._get(self.pages, page_id, "分镜页")
        version = next(
            (v for v in page.versions if any(p.id == panel_id for p in v.panels)), None)
        if version is None:
            raise NotFoundError(f"画格{panel_id}不属于分镜页{page_id}")
        panel = next(p for p in version.panels if p.id == panel_id)
        scripts = []
        for seg_id, seg_version in version.script_refs.items():
            segment = self.segments[seg_id]
            text = segment.versions[seg_version - 1].text
            scripts.append({
                "segment_id": seg_id, "version": seg_version, "text": text,
                "latest_version": segment.current.version,
            })
        sources = []
        for source_id, revision in version.source_refs.items():
            source = self.sources[source_id]
            sources.append({
                "source_id": source_id, "title": source.title,
                "citation": source.citation, "revision": revision,
                "latest_revision": source.revision,
            })
        opinions = [
            {
                "opinion_id": o.id, "scope": o.scope, "stance": o.stance,
                "state": o.state, "author": o.author, "role": o.role,
                "target_version": o.target_version,
                "decided_by": o.decided_by, "outdated": o.outdated,
                "countersigned_from": o.countersigned_from,
            }
            for o in self.opinions.values()
            if o.target_kind == "page" and o.target_id == page_id
        ]
        issues = [
            {
                "issue_id": i.id, "subject": i.subject, "scope": i.scope,
                "target_version": i.target_version,
                "state": i.state, "is_major_fact": i.is_major_fact,
                "resolution": i.resolution,
                "decision": i.decision, "decided_by": i.decided_by,
            }
            for i in self.issues.values()
            if i.target_kind == "page" and i.target_id == page_id
        ]
        return {
            "page_id": page_id,
            "page_version": version.version,
            "panel": {"id": panel.id, "index": panel.index,
                      "sketch_ref": panel.sketch_ref},
            "scripts": scripts,
            "sources": sources,
            "opinions": opinions,
            "issues": issues,
            "deciders": sorted({
                x for x in [o.decided_by for o in self.opinions.values()
                            if o.target_kind == "page" and o.target_id == page_id]
                + [i.decided_by for i in self.issues.values()
                   if i.target_kind == "page" and i.target_id == page_id]
                if x
            }),
        }

    def _export_snapshot(self, page_ids: list | None):
        """导出开始时冻结: 候选及其页面版本、各依赖最新版本、史料许可与引文、未结议题。

        导出过程中任何改稿(页面换版/状态流转、脚本/设定/史料修订、追加画格、
        意见导致的议题变化)都不得混入本次结果, 故此处只复制值, 不保留活动引用。
        """
        on = self._today()
        if page_ids is None:
            candidates = [p for p in self.pages.values() if p.status == "可出版"]
        else:
            candidates = [self._get(self.pages, pid, "分镜页") for pid in page_ids]
        frozen_pages = []
        for page in candidates:
            version = page.current
            frozen_pages.append({
                "page_id": page.id,
                "title": page.title,
                "status": page.status,
                "page_version": version.version,
                "script_refs": dict(version.script_refs),
                "design_refs": dict(version.design_refs),
                "source_refs": dict(version.source_refs),
                "panels": [
                    {"id": p.id, "index": p.index, "sketch_ref": p.sketch_ref}
                    for p in version.panels
                ],
            })
        latest_refs = {
            "script": {sid: seg.current.version for sid, seg in self.segments.items()},
            "design": {did: dsn.current["version"] for did, dsn in self.designs.items()},
            "source": {sid: src.revision for sid, src in self.sources.items()},
        }
        sources_frozen = {
            sid: {"citation": src.citation, "revision": src.revision,
                  "license": src.license}  # License 为 frozen, 修订时整体替换
            for sid, src in self.sources.items()
        }
        open_issues = copy.deepcopy(
            [i for i in self.issues.values() if i.state == "待会审"])
        return on, frozen_pages, latest_refs, sources_frozen, open_issues

    def export_batch(self, *, scope: str, page_ids: list | None = None,
                     actor: str, role: str, on: date | None = None) -> dict:
        """批量导出: 只包含本次快照下获准的内容, 被排除页面附对应版本的原因。"""
        self._require_editor(role)
        snapshot_on, frozen_pages, latest_refs, sources_frozen, open_issues = \
            self._export_snapshot(page_ids)
        on = on or snapshot_on
        exported, excluded = [], {}
        for fp in frozen_pages:  # 循环内只读取冻结值, 中途改稿无法混入
            tag = f"(页面v{fp['page_version']})"
            problems = []
            if fp["status"] != "可出版":
                problems.append(
                    f"页面状态为「{fp['status']}」, 未达到可出版{tag}")
            # 事实争议: 未结议题必须命中本次冻结的页面版本与其依赖版本。
            for issue in open_issues:
                if issue.scope != "史实":
                    continue
                if issue.target_kind == "page":
                    matched = issue.target_id == fp["page_id"] \
                        and issue.target_version == fp["page_version"]
                else:
                    refs = self._page_refs_for_frozen(fp, issue.target_kind)
                    matched = issue.target_id in refs \
                        and refs[issue.target_id] == issue.target_version
                if matched:
                    problems.append(
                        f"事实争议未关闭: {issue.id}({issue.subject}){tag}")
            # 依赖陈旧: 与导出开始时冻结的各对象最新版本比较。
            for seg_id, seg_version in fp["script_refs"].items():
                latest = latest_refs["script"].get(seg_id, seg_version)
                if seg_version != latest:
                    problems.append(
                        f"脚本段落{seg_id}依赖陈旧: 采用v{seg_version}, 当前v{latest}{tag}")
            for design_id, design_version in fp["design_refs"].items():
                latest = latest_refs["design"].get(design_id, design_version)
                if design_version != latest:
                    problems.append(
                        f"人物设定{design_id}依赖陈旧: 采用v{design_version}, 当前v{latest}{tag}")
            for source_id, revision in fp["source_refs"].items():
                latest = latest_refs["source"].get(source_id, revision)
                if revision != latest:
                    problems.append(
                        f"史料{source_id}依赖陈旧: 采用r{revision}, 当前r{latest}{tag}")
            # 史料许可: 按本次导出日期与出版范围, 依据冻结时的许可快照判断。
            for source_id in fp["source_refs"]:
                frozen = sources_frozen[source_id]
                problems.extend(
                    f"史料{source_id}: {problem}{tag}"
                    for problem in frozen["license"].problems(scope, on)
                )
            if problems:
                excluded[fp["page_id"]] = {
                    "page_version": fp["page_version"],
                    "reasons": problems,
                }
                continue
            exported.append({
                "page_id": fp["page_id"],
                "title": fp["title"],
                "page_version": fp["page_version"],
                "panels": fp["panels"],
                "sources": [
                    {
                        "source_id": sid,
                        "citation": sources_frozen[sid]["citation"],
                        "revision": rev,
                    }
                    for sid, rev in fp["source_refs"].items()
                ],
                "scripts": [
                    {"segment_id": sid, "version": v}
                    for sid, v in fp["script_refs"].items()
                ],
            })
        return {
            "scope": scope,
            "exported_at": self._now().isoformat(),
            "exported_on": on.isoformat(),
            "exported_by": actor,
            "pages": exported,
            "excluded": excluded,
        }
