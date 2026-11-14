# -*- coding: utf-8 -*-
"""检查系统：状态一致性 / 知识边界 / 设定矛盾 / 细纲完成度 / 重复表达 / 雷区。

设计红线：只报告 + 定位 + 建议，绝不擅自改文。用户对每条可选：让 AI 改 / 忽略 / 标记误报。
"""
from .jsonx import extract_json, find_field
from .pipeline import Pipeline
from .store import ProjectStore
from .task_templates import render as render_task


class Checker:
    """一致性审稿服务。"""

    def __init__(self, store: ProjectStore):
        self.store = store
        self.pipeline = Pipeline(store)

    def check_chapter(self, chapter_id: int, on_chunk=None) -> dict:
        """检查整章，返回 {ok, issues, text, log_id}。issues 含误报过滤。"""
        ch = self.store.get_chapter(chapter_id)
        if not ch:
            return {"ok": False, "error": "章节不存在"}
        blocks = self.store.list_text_blocks(chapter_id)
        accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
        if not accepted:
            return {"ok": False, "error": "本章还没有已接受的正文"}
        prose = "\n\n".join(b["content"] for b in accepted)
        task = render_task("check", self.store)
        # 流式：检查整章是长输出，非流式须等完整响应，渠道 60/100 秒超时会直接失败；
        # 流式边收边活不易超时，且 on_chunk 可回报实时进度。
        r = self.pipeline.run("check", {
            "chapter": ch,
            "prose": prose,
            "character_names": ch.get("characters") or [],
        }, task, stream=True, on_chunk=on_chunk)
        if not r["ok"]:
            return r
        obj, err = extract_json(r["text"])
        if err:
            return {**r, "ok": False, "error": f"检查结果解析失败：{err}"}
        raw_issues = obj.get("issues") if isinstance(obj, dict) else None
        if raw_issues is None:
            # 容错：数组顶层
            raw_issues = obj if isinstance(obj, list) else []
        issues = []
        fps = self.store.list_false_positives()
        for it in raw_issues:
            if not isinstance(it, dict):
                continue
            quote = str(find_field(it, "quote", "text", default=""))
            problem = str(find_field(it, "problem", "desc", default=""))
            if any(fp in quote or fp in problem for fp in fps):
                continue
            issues.append({
                "type": str(find_field(it, "type", "category", default="其他")),
                "location": str(find_field(it, "location", "position", default="")),
                "quote": quote,
                "problem": problem,
                "suggestion": str(find_field(it, "suggestion", "fix", default="")),
            })
        self.store.save_check(chapter_id, issues)
        return {**r, "issues": issues}

    def check_selection(self, chapter_id: int, text: str, on_chunk=None) -> dict:
        """检查选中段落（轻量版）。"""
        ch = self.store.get_chapter(chapter_id)
        if not ch:
            return {"ok": False, "error": "章节不存在"}
        task = render_task("check_selection", self.store)
        r = self.pipeline.run("check", {
            "chapter": ch,
            "prose": text,
            "character_names": ch.get("characters") or [],
        }, task, stream=True, on_chunk=on_chunk)
        if not r["ok"]:
            return r
        obj, err = extract_json(r["text"])
        if err:
            return {**r, "ok": False, "error": f"检查结果解析失败：{err}"}
        raw = obj.get("issues") if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
        issues = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            issues.append({
                "type": str(find_field(it, "type", "category", default="其他")),
                "location": "",
                "quote": str(find_field(it, "quote", "text", default="")),
                "problem": str(find_field(it, "problem", "desc", default="")),
                "suggestion": str(find_field(it, "suggestion", "fix", default="")),
            })
        self.store.save_check(chapter_id, issues)
        return {**r, "issues": issues}

    def fix_issue(self, chapter_id: int, issue: dict, instruction: str = "",
                  on_chunk=None, stop_event=None) -> dict:
        """针对一条问题，生成修改建议（改写角色 prose_rewrite）。返回建议文本，用户确认后替换。

        2026-10-08：老 rewrite 角色删除，审校修复并入 prose_rewrite（唯一改写角色）。
        """
        ch = self.store.get_chapter(chapter_id)
        if not ch:
            return {"ok": False, "error": "章节不存在"}
        blocks = self.store.list_text_blocks(chapter_id)
        accepted = [b for b in blocks if b["status"] in ("已接受", "已锁定")]
        target = ""
        quote = issue.get("quote", "")
        for b in accepted:
            if quote and quote in b["content"]:
                target = b["content"]
                break
        if not target:
            target = "\n\n".join(b["content"] for b in accepted)[-3000:]
        req = instruction or "修复该问题"
        # 尺度收敛问题的针对性指令：把本章细纲块的 must_have（施工单）带进改写要求，
        # 让 rewrite 按施工单恢复显式描写密度（check→rewrite 闭环的核心环节）
        if "尺度" in str(issue.get("type", "")):
            mhs = []
            for b in self.store.list_blocks(chapter_id):
                mh = str(b.get("must_have") or "").strip()
                if mh and mh not in mhs:
                    mhs.append(mh)
            if mhs:
                req = (f"{req}。该段存在尺度收敛：按细纲 must_have 恢复显式、直白的描写密度，"
                       f"不得软化、不得概述跳过。本块必含要素：{'；'.join(mhs)[:200]}")
        task = (f"审稿发现以下问题：{issue.get('problem', '')}（{issue.get('type', '')}）\n"
                f"建议：{issue.get('suggestion', '')}\n"
                f"请按要求修复：{req}\n"
                "约束：只修改问题所在句子/段落；不得改变已定事实、人物对白、人物性格与叙事功能；"
                "不得使用被指出的套话表达。"
                "只输出修改后的段落正文（对应原问题位置），不要解释；"
                "不要写 P 段号或「修改后」这类标题，不要把问题段落之外的正文一并抄回来。")
        return self.pipeline.run("prose_rewrite", {
            "chapter": ch,
            "rewrite_text": target,
            "character_names": ch.get("characters") or [],
            "user_req": req,
        }, task, stream=True, on_chunk=on_chunk, stop_event=stop_event)
