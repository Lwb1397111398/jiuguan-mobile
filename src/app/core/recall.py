# -*- coding: utf-8 -*-
"""记忆召回词（recall keys）——模糊回溯引擎。

## 为什么需要它

记忆对象（人物 / 世界实体）久未出场后会「沉睡」退出注入，只有「本次写作上下文
再次提到它」才被唤醒重新注入。原来的判定是**精确子串**（`name in haystack`），
漏召回非常严重：

- 记忆里叫「三生三世十里桃花剑」，正文只写「他使了一把剑」→ 匹配不上
- 反过来：记忆里只记了「一把剑」，正文才出现全名 → 也匹配不上
- 记忆里是「康熙帝」，正文写「皇上」→ 匹配不上（称谓共指，别名也救不了）

## 方案

为每个记忆对象生成一组「召回词」，任一召回词命中即视为被点名。召回词来源：

1. **规范名** + 已记录的 **aliases**（模型归档时记下的简称）
2. **身份词**：从 identity 切出的实词（「大清康熙皇帝」→「大清」「皇帝」）
3. **称谓同义词**：若身份里含某概念（如「皇帝」），则「皇上/陛下/圣上/天子」
   全部成为该对象的召回词（解决「皇上指康熙帝」）
4. **长名尾部核心词**：「三生三世十里桃花剑」→「桃花剑」「花剑」
5. **单字泛词**（如「剑」）：太容易误命中，**仅当全书只有这一个对象用它时启用**

## 两级映射

- **严格映射**（`strict`）：召回词唯一对应一个对象时，才用它把名单里的名字
  「改写」成规范名（如块名单写「皇上」→ 康熙帝）。歧义词不参与改写，避免乱改名。
- **宽松命中**（`hits`）：所有召回词都参与 `in haystack` 判定，歧义词也能唤醒
  多个候选——宁可多召回一点，也不能漏召回（漏 = AI 让死人出场 / 写错道具归属）。

## 用户自定义

设置键 `recall_titles`（每行 `皇上=皇帝`）可追加同义词，覆盖内置表没有的叫法。
"""

import re

# ---------------------------------------------------------------- 称谓同义词表
# 同义词 → 规范概念。若某对象的身份（或名字）里含规范概念，则所有同义词
# 都成为它的召回词，从而解决「皇上 → 康熙帝」这类共指。
TITLE_SYNONYMS: dict[str, str] = {
    # 君主 / 皇族
    "皇上": "皇帝", "陛下": "皇帝", "圣上": "皇帝", "天子": "皇帝",
    "万岁爷": "皇帝", "圣驾": "皇帝", "君王": "皇帝", "帝王": "皇帝",
    "皇后": "皇后", "娘娘": "皇后", "国母": "皇后",
    "太后": "太后", "皇太后": "太后", "太妃": "太后",
    "太子": "太子", "储君": "太子", "东宫": "太子",
    "殿下": "殿下", "王爷": "王爷", "亲王": "王爷", "藩王": "王爷",
    "公主": "公主", "郡主": "郡主", "格格": "公主",
    # 师徒
    "师父": "师父", "师傅": "师父", "师尊": "师父", "恩师": "师父",
    "老师": "师父", "先生": "师父",
    "师兄": "师兄", "师姐": "师姐", "师弟": "师弟", "师妹": "师妹",
    "师伯": "师伯", "师叔": "师叔", "师祖": "师祖",
    # 门派 / 宗教
    "掌门": "掌门", "宗主": "掌门", "门主": "掌门", "掌教": "掌门",
    "教主": "教主", "圣女": "圣女", "圣子": "圣子",
    "长老": "长老", "执事": "执事", "护法": "护法", "堂主": "堂主",
    "道长": "道长", "道友": "道友", "真人": "真人", "仙长": "道长",
    "大师": "大师", "方丈": "方丈", "住持": "住持", "施主": "施主",
    "主持": "住持",
    # 军政官职
    "将军": "将军", "大将军": "将军", "统领": "将军", "元帅": "将军",
    "都尉": "将军", "校尉": "将军", "千户": "将军",
    "大人": "大人", "老爷": "老爷", "老太爷": "老爷",
    "少爷": "少爷", "公子": "公子", "小姐": "小姐", "姑娘": "姑娘",
    "县令": "县令", "知县": "县令", "知府": "知府", "府尹": "知府",
    "丞相": "丞相", "宰相": "丞相", "首辅": "丞相",
    "捕头": "捕头", "捕快": "捕头", "衙役": "捕头",
    # 亲属称谓
    "父亲": "父亲", "爹": "父亲", "爹爹": "父亲", "老爹": "父亲",
    "娘": "母亲", "母亲": "母亲", "娘亲": "母亲", "老娘": "母亲",
    "哥哥": "哥哥", "兄长": "哥哥", "大哥": "哥哥", "兄台": "哥哥",
    "姐姐": "姐姐", "姐": "姐姐", "弟弟": "弟弟", "妹妹": "妹妹",
    "爷爷": "爷爷", "祖父": "爷爷", "奶奶": "奶奶", "祖母": "奶奶",
    "外公": "外公", "外婆": "外婆", "舅舅": "舅舅", "姑姑": "姑姑",
    "叔叔": "叔叔", "伯伯": "伯伯", "婶婶": "婶婶", "嫂子": "嫂子",
    "娘子": "娘子", "相公": "相公", "夫君": "相公", "娘亲": "母亲",
    # 江湖 / 尊称
    "前辈": "前辈", "老前辈": "前辈", "老丈": "前辈", "老先生": "前辈",
    "大侠": "大侠", "侠士": "大侠", "少侠": "少侠", "高手": "高手",
    "阁下": "阁下", "足下": "阁下", "兄台": "阁下",
    "统领": "将军", "头领": "头领", "寨主": "头领", "当家": "头领",
    "店主": "店主", "老板": "店主", "掌柜": "店主", "小二": "小二",
    "大夫": "大夫", "郎中": "大夫", "医仙": "大夫",
    "管家": "管家", "丫鬟": "丫鬟", "侍女": "丫鬟", "护卫": "护卫",
}

# 器物 / 建筑类单字后缀：用于从超长专名里提取尾部核心词
# （「三生三世十里桃花剑」→「剑」）。单字词太泛，仅在全书唯一时启用。
ARTIFACT_SUFFIX = set(
    "剑刀枪棍鞭锏斧钺钩叉戟矛盾弓箭弩印令符箓珠丹丸散膏药酒茶盏杯碗盘瓶罐壶"
    "炉鼎钟鼓磬琴瑟筝笛箫锣钹塔楼阁殿宫院寺观庙堂亭台榭廊桥舟船车轿旗幡幢盖"
    "伞冠冕巾帽衣袍衫裙裤靴履甲铠盔胄戒环镯佩玉璧琮圭璋玦簪钗钏锁钥匙镜扇帕"
    "囊袋箧箱柜匣盒筒卷册书本经诀谱图录志传碑碣石泉池潭湖海江河溪涧林森木花"
    "草原谷峡崖峰岭山城关塞隘营寨垒堡坞庄园墅第府宅邸店铺坊市集圩场门宗派帮"
    "会国朝军营库塔井坑洞窟穴井"
)

_PUNCT_RE = re.compile(
    r"[\s，。、；：！？…—\-_/\\|·・"
    r"「」『』（）《》〈〉“”‘’"
    r"!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]+"
)


def norm(s: str) -> str:
    """归一化：全角转半角 + 去标点空白 + 大小写折叠。只用于匹配，不改存储。"""
    if not s:
        return ""
    out = []
    for ch in str(s):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:      # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:              # 全角空格
            out.append(" ")
        else:
            out.append(ch)
    return _PUNCT_RE.sub("", "".join(out)).lower()


def _segments(text: str) -> list[str]:
    """按分隔符与标点切出片段（不做滑窗，避免产生「清康」这类垃圾词）。"""
    if not text:
        return []
    return [x.strip() for x in re.split(r"[\s，。、；：！？/·・\-—_|（）]+", str(text))
            if x and x.strip()]


def _identity_terms(identity: str) -> list[str]:
    """从身份串提取有区分度的词：短片段全收，长片段收首尾各 2 字。

    「大清康熙皇帝」→ ['大清康熙皇帝', '大清', '皇帝']
    """
    terms: list[str] = []
    for seg in _segments(identity):
        if len(seg) <= 4:
            if len(seg) >= 2:
                terms.append(seg)
        else:
            terms.append(seg)
            terms.append(seg[:2])
            terms.append(seg[-2:])
    return terms


def _title_variants(terms: list[str], syn: dict[str, str]) -> list[str]:
    """称谓同义词扩展：身份里含规范概念 → 该概念的全部叫法都成为召回词。"""
    out = []
    joined = "｜".join(terms)
    for k, v in syn.items():
        if v and v in joined:
            out.append(k)
    return out


def _core_tail(name: str) -> tuple[list[str], str]:
    """长专名尾部核心词。返回 (中等词列表, 单字泛词)。

    「三生三世十里桃花剑」→ (['桃花剑', '花剑'], '剑')
    """
    n = norm(name)
    if len(n) < 3:
        return [], ""
    mids: list[str] = []
    if len(n) >= 4:
        mids.append(n[-3:])
    mids.append(n[-2:])
    last = n[-1]
    single = last if last in ARTIFACT_SUFFIX else ""
    return mids, single


def _uniq(seq) -> list[str]:
    out, seen = [], set()
    for x in seq:
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def char_keys(name: str, card: dict, syn: dict[str, str] | None = None) -> tuple[list[str], str]:
    """人物召回词。返回 (强召回词, 单字弱词)。"""
    syn = syn or TITLE_SYNONYMS
    n = norm(name)
    keys = [n]
    for a in (card.get("aliases") or []):
        a = norm(a)
        if a:
            keys.append(a)
    terms = _uniq(_identity_terms(str(card.get("identity") or "")))
    keys.extend(norm(t) for t in terms)
    keys.extend(norm(t) for t in _title_variants(terms, syn))
    mids, single = _core_tail(name)
    keys.extend(mids)
    return _uniq([k for k in keys if k]), single


def entity_keys(name: str, e: dict) -> tuple[list[str], str]:
    """世界实体召回词。返回 (强召回词, 单字弱词)。"""
    keys = [norm(name)]
    for a in (e.get("aliases") or []):
        a = norm(a)
        if a:
            keys.append(a)
    mids, single = _core_tail(name)
    keys.extend(mids)
    return _uniq([k for k in keys if k]), single


class RecallIndex:
    """一次组装内的召回索引（建议 build 内缓存复用）。

    - `strict`：唯一映射 `召回词 → ("char"|"entity", 规范名)`，用于把名单里的
      别名/称谓改写回规范名（歧义词不参与，避免乱改名）。
    - `hits(hay)`：在归一化文本里命中的全部对象名（含歧义词，宁滥勿缺）。
    """

    def __init__(self, state: dict, extra_titles: dict[str, str] | None = None):
        self.state = state or {}
        self.syn = dict(TITLE_SYNONYMS)
        if extra_titles:
            self.syn.update({str(k).strip(): str(v).strip()
                             for k, v in extra_titles.items() if k and v})
        self._weak: dict[tuple[str, str], str] = {}
        self._owner: dict[str, list[tuple[str, str]]] = {}
        self._strict: dict[str, tuple[str, str]] = {}
        self._build()

    # ---------- 构建 ----------

    def _add(self, oid: tuple[str, str], key: str):
        if not key:
            return
        bucket = self._owner.setdefault(key, [])
        if oid not in bucket:
            bucket.append(oid)

    def _build(self):
        chars = self.state.get("characters") or {}
        ents = ((self.state.get("world_state") or {}).get("entities")) or {}

        for name, card in chars.items():
            if not isinstance(card, dict):
                continue
            oid = ("char", str(name))
            keys, weak = char_keys(name, card, self.syn)
            for k in keys:
                self._add(oid, k)
            if weak:
                self._weak[oid] = weak

        for name, e in ents.items():
            if not isinstance(e, dict):
                continue
            oid = ("entity", str(name))
            keys, weak = entity_keys(name, e)
            for k in keys:
                self._add(oid, k)
            if weak:
                self._weak[oid] = weak

        # 单字泛词（「剑」）：只有全书唯一对应该对象时才启用，否则全书一把
        # 剑写「剑」就会把所有剑都唤醒，等于没有过滤。
        weak_owner: dict[str, list[tuple[str, str]]] = {}
        for oid, w in self._weak.items():
            weak_owner.setdefault(w, []).append(oid)
        for w, oids in weak_owner.items():
            if len(oids) == 1:
                self._add(oids[0], w)

        self._strict = {k: v[0] for k, v in self._owner.items() if len(v) == 1}

    # ---------- 查询 ----------

    def hits(self, hay_norm: str, kind: str | None = None) -> set:
        """归一化文本里命中的对象名集合（kind 为 "char"/"entity" 时只返回该类）。"""
        if not hay_norm:
            return set()
        out: set = set()
        for k, oids in self._owner.items():
            if k and k in hay_norm:
                for oid in oids:
                    if kind is None or oid[0] == kind:
                        out.add(oid[1])
        return out

    def resolve(self, name: str, kind: str | None = None) -> str:
        """把名单里的叫法解析成规范名（仅唯一映射；歧义或未命中原样返回）。"""
        oid = self._strict.get(norm(name))
        if not oid:
            return str(name)
        if kind and oid[0] != kind:
            return str(name)
        return oid[1]

    def strict_map(self, kind: str | None = None) -> dict[str, str]:
        """严格映射的扁平副本（召回词 → 规范名），供名单批量解析。"""
        if kind is None:
            return {k: v[1] for k, v in self._strict.items()}
        return {k: v[1] for k, v in self._strict.items() if v[0] == kind}


def parse_title_setting(text: str) -> dict[str, str]:
    """解析用户自定义称谓映射（每行「皇上=皇帝」或「皇上:皇帝」）。"""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", "：", ":"):
            if sep in line:
                k, v = line.split(sep, 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    out[k] = v
                break
    return out


def rumor_objects(content: str, index: "RecallIndex | None") -> set:
    """传闻文本里提到的记忆对象名集合（用于「这条传闻与本次剧情相关」判定）。

    用法：传闻提到的对象 ∩ 本次上下文提到的对象 ≠ 空 → 传闻相关，必留。
    例：传闻「皇帝正在炼丹」提到康熙帝，本次上下文写「皇上下旨」也提到康熙帝
    （「皇上」是康熙帝的称谓召回词）→ 这条传闻与本次剧情相关。
    """
    if index is None:
        return set()
    c = norm(content)
    if not c:
        return set()
    return index.hits(c)
