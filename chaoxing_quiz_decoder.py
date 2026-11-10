#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学习通（超星）章节测验离线解密 + 解析工具
==========================================

把浏览器"另存为"下来的学习通页面，还原成一份可读的 Markdown 题库。

原理
----
学习通在作业页里嵌了一段 base64 的 @font-face（字体名 font-cxsecret）。
服务端把部分汉字替换成生僻字（挙 / 挚 / 挒 / 捘 …），再用这个自定义字体
把这些生僻字的字形"画成"原字的样子。于是：

    屏幕上看 → 正常中文
    抓源码   → 一堆生僻字

这个字体是**内嵌在 HTML 里的**，等于把密钥和锁一起给了你。
本工具的做法：
    1. 从 HTML 里抠出字体，base64 解码成 .ttf
    2. 解析 ttf 的 cmap，拿到全部被替换的码位
    3. 用这个字体把每个码位渲染成图片
    4. 拿图片去和系统里若干中文字体渲染的同一个字比较（余弦相似度）
       多字体取平均分 → 分数最高的那个字就是明文
    5. 用得到的映射表还原整页文字，再抽成结构化题目，输出 Markdown

依赖：Pillow、numpy
    pip install pillow numpy

依赖：只需要 Pillow
    pip install pillow

用法
----
    python chaoxing_quiz_decoder.py 学生学习页面.html
    python chaoxing_quiz_decoder.py 学生学习页面.html -o 题目.md
    python chaoxing_quiz_decoder.py 学生学习页面.html --show-map   # 只看映射表
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pickle
import re
import struct
import sys
import tempfile
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")


# --------------------------------------------------------------------------
# 1. 从 HTML 里抽出内嵌字体
# --------------------------------------------------------------------------

# @font-face{font-family:'font-cxsecret';src:url('data:application/font-ttf;charset=utf-8;base64,AAEA...')}
_FONT_FACE_RE = re.compile(
    r"@font-face\s*\{[^}]*?font-family\s*:\s*['\"]?([^;'\"]+)['\"]?[^}]*?"
    r"url\(\s*['\"]?data:[^,()'\"]*,([A-Za-z0-9+/=\s]+)['\"]?\s*\)",
    re.I | re.S,
)


@dataclass
class EmbeddedFont:
    family: str
    data: bytes

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.data).hexdigest()[:12]


def extract_fonts(html: str) -> list[EmbeddedFont]:
    """找出页面里所有 base64 内嵌字体。"""
    out = []
    for m in _FONT_FACE_RE.finditer(html):
        family = m.group(1).strip().strip("'\"")
        try:
            data = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=False)
        except Exception:
            continue
        if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
            continue
        out.append(EmbeddedFont(family=family, data=data))
    return out


# --------------------------------------------------------------------------
# 2. 解析 TrueType 的 cmap，拿到「被替换的字」
# --------------------------------------------------------------------------

def _sfnt_tables(data: bytes) -> dict[str, tuple[int, int]]:
    if data[:4] == b"ttcf":  # 字体集合，取第一个
        off = struct.unpack(">I", data[12:16])[0]
        data = data[off:]
    num = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(num):
        tag, _chk, off, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
        tables[tag.decode("latin-1")] = (off, length)
    return tables


def font_codepoints(data: bytes) -> list[str]:
    """返回字体 cmap 里所有有字形的码位（即被替换掉的那些字）。"""
    tables = _sfnt_tables(data)
    if "cmap" not in tables:
        return []
    base, _ = tables["cmap"]
    n = struct.unpack(">H", data[base + 2: base + 4])[0]
    subtables = []
    for i in range(n):
        pid, eid, off = struct.unpack(">HHI", data[base + 4 + 8 * i: base + 12 + 8 * i])
        subtables.append((pid, eid, base + off))

    # 优先 unicode 全字库(format 12)，退回 BMP(format 4)
    subtables.sort(key=lambda t: (t[1] == 4, t[0] not in (3, 0)))
    chars: list[str] = []
    for pid, eid, off in subtables:
        fmt = struct.unpack(">H", data[off: off + 2])[0]
        if fmt == 12:
            ngroups = struct.unpack(">I", data[off + 12: off + 16])[0]
            for g in range(ngroups):
                s, e, _gid = struct.unpack(">III", data[off + 16 + 12 * g: off + 28 + 12 * g])
                for cp in range(s, e + 1):
                    if cp <= 0x10FFFF:
                        chars.append(chr(cp))
            break
        if fmt == 4:
            seg2 = struct.unpack(">H", data[off + 6: off + 8])[0]
            seg = seg2 // 2
            ends = struct.unpack(f">{seg}H", data[off + 14: off + 14 + seg2])
            starts = struct.unpack(f">{seg}H", data[off + 16 + seg2: off + 16 + 2 * seg2])
            deltas = struct.unpack(f">{seg}h", data[off + 16 + 2 * seg2: off + 16 + 3 * seg2])
            ro = off + 16 + 3 * seg2
            ranges = struct.unpack(f">{seg}H", data[ro: ro + seg2])
            for i in range(seg):
                for cp in range(starts[i], ends[i] + 1):
                    if cp == 0xFFFF:
                        continue
                    if ranges[i] == 0:
                        gid = (cp + deltas[i]) & 0xFFFF
                    else:
                        p = ro + 2 * i + ranges[i] + 2 * (cp - starts[i])
                        gid = struct.unpack(">H", data[p: p + 2])[0]
                        if gid:
                            gid = (gid + deltas[i]) & 0xFFFF
                    if gid:
                        chars.append(chr(cp))
            break
    return chars


# --------------------------------------------------------------------------
# 3. 字形比对：把密文字画出来，去系统字体里找最像的那个字
# --------------------------------------------------------------------------

DEFAULT_REF_FONTS = [
    "msyh.ttc", "msyhbd.ttc", "msyhl.ttc", "simhei.ttf",
    "Deng.ttf", "Dengb.ttf", "STXIHEI.TTF", "STSONG.TTF",
    "simsun.ttc", "simsunb.ttf", "simkai.ttf", "simfang.ttf",
    "SIMLI.TTF", "SIMYOU.TTF", "HYZhongHeiTi-197.ttf", "STKAITI.TTF",
]

_FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts"),
    Path.home() / "Library/Fonts",
]


def find_ref_fonts(names: list[str] | None = None, limit: int = 16) -> list[str]:
    """在当前系统里找出可用的中文参考字体。"""
    names = names or DEFAULT_REF_FONTS
    found: list[str] = []
    for d in _FONT_DIRS:
        if not d.exists():
            continue
        index = {p.name.lower(): p for p in d.rglob("*") if p.is_file()}
        for n in names:
            p = index.get(n.lower())
            if p and str(p) not in found:
                found.append(str(p))
    return found[:limit]


class GlyphMatcher:
    """渲染 + 比对。参考签名的结果会缓存，第二次运行就是秒开。

    没有用 numpy：把每个字渲染成 RES×RES 的灰度图，二值化成一个大整数，
    比 similarity 时直接算 (a ^ b) 的二进制 1 的个数（汉明距离），
    这是 C 层的一次位运算，比纯 Python 循环快两个数量级。
    """

    SIZE = 96           # 渲染字号
    RES = 24            # 归一化到 RES×RES
    COARSE_TOP = 25     # 汉明距离初筛保留的候选数
    LOW_MARGIN = 0.02   # 第一名与第二名差距小于此值 → 标记为"待确认"

    def __init__(self, cache_dir: Path | None = None, ref_fonts: list[str] | None = None,
                 base_font: str | None = None, verbose: bool = True):
        if ref_fonts:
            self.ref_fonts = ref_fonts
        elif base_font:
            # 找到底稿字体就用它一家 —— 密文字形就是从它身上来的，比对近乎精确
            self.ref_fonts = [base_font]
        else:
            self.ref_fonts = find_ref_fonts()
        if not self.ref_fonts:
            raise RuntimeError("系统里没找到可用的中文参考字体")
        self.exact = bool(base_font and not ref_fonts)
        self.cache_dir = cache_dir or (Path(tempfile.gettempdir()) / "cx_quiz_cache")
        self.verbose = verbose
        self._tmpdir = self.cache_dir / "fonts"
        self._tmpdir.mkdir(parents=True, exist_ok=True)
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    # ---- 字体对象（必须复用：truetype() 每次都会把整个字体文件读一遍）----
    def _font(self, font_path: str | Path, index: int = 0):
        key = (str(font_path), index)
        f = self._fonts.get(key)
        if f is None:
            f = ImageFont.truetype(str(font_path), self.SIZE, index=index)
            self._fonts[key] = f
        return f

    # ---- 渲染 ----
    def _render(self, font_path: str | Path, ch: str, index: int = 0):
        """返回 (二值化位图整数, 灰度字节串)；两样都没有意义时返回 None。"""
        try:
            f = self._font(font_path, index)
        except Exception:
            return None
        img = Image.new("L", (self.SIZE * 2, self.SIZE * 2), 0)
        try:
            ImageDraw.Draw(img).text((self.SIZE // 2, self.SIZE // 2), ch, font=f, fill=255)
        except Exception:
            return None
        bbox = img.getbbox()
        if not bbox:
            return None
        px = img.crop(bbox).resize((self.RES, self.RES), Image.LANCZOS).tobytes()
        bits = 0
        for p in px:
            bits = (bits << 1) | (1 if p > 127 else 0)
        return bits, px

    # ---- 候选字 ----
    @staticmethod
    def candidates(extra: str = "") -> list[str]:
        cand: list[str] = []
        # GB2312 一级字（3755 个最常用汉字）
        for hi in range(0xB0, 0xD8):
            for lo in range(0xA1, 0xFF):
                try:
                    cand.append(bytes([hi, lo]).decode("gb2312"))
                except UnicodeDecodeError:
                    pass
        cand.extend(extra)
        return list(dict.fromkeys(cand))

    # ---- 参考签名（带缓存） ----
    def reference_matrix(self, cands: list[str]) -> list[tuple[str, int, bytes]]:
        key_src = json.dumps({
            "fonts": [[f, int(os.path.getmtime(f))] for f in self.ref_fonts],
            "size": self.SIZE, "res": self.RES,
            "hash": hashlib.sha1("".join(cands).encode()).hexdigest(),
        }, sort_keys=True)
        key = hashlib.sha1(key_src.encode()).hexdigest()[:16]
        cache = self.cache_dir / f"ref_{key}.pkl"

        if cache.exists():
            try:
                with cache.open("rb") as fh:
                    data = pickle.load(fh)
                if len(data) == len(self.ref_fonts):
                    if self.verbose:
                        print(f"  · 参考签名缓存命中 {cache.name}", file=sys.stderr)
                    return data
            except Exception:
                pass

        if self.verbose:
            print(f"  · 首次运行：渲染 {len(cands)} 个候选字 × {len(self.ref_fonts)} 种字体"
                  f"（约 1 分钟，之后会缓存）…", file=sys.stderr)
        t0 = time.time()
        mats: list[list[tuple[str, int, bytes]]] = []
        for fp in self.ref_fonts:
            rows = []
            for ch in cands:
                v = self._render(fp, ch)
                if v is not None:
                    rows.append((ch, v[0], v[1]))
            mats.append(rows)
            if self.verbose:
                print(f"      {Path(fp).name:24s} 完成 ({time.time()-t0:.0f}s)", file=sys.stderr)
        try:
            with cache.open("wb") as fh:
                pickle.dump(mats, fh, protocol=4)
        except Exception:
            pass
        return mats

    # ---- 主入口 ----
    def build_mapping(self, font: EmbeddedFont, extra_chars: str = "",
                      topk: int = 3) -> tuple[dict[str, str], dict[str, list[tuple[float, str]]]]:
        cipher = font_codepoints(font.data)
        if not cipher:
            return {}, {}

        # 把字体落盘（ImageFont 只认路径）
        ttf_path = self._tmpdir / f"{font.sha1}.ttf"
        if not ttf_path.exists():
            ttf_path.write_bytes(font.data)

        queries = []
        for ch in cipher:
            v = self._render(ttf_path, ch)
            if v is not None:
                queries.append((ch, v[0], v[1]))
        if not queries:
            return {}, {}

        cands = self.candidates(extra_chars)
        refs = self.reference_matrix(cands)

        full = self.RES * self.RES
        mapping: dict[str, str] = {}
        detail: dict[str, list[tuple[float, str]]] = {}

        for ch, qbits, qpx in queries:
            # 第一轮：汉明距离初筛（位运算，极快）
            coarse: list[tuple[int, str, bytes]] = []
            for rows in refs:
                for cand, rbits, rpx in rows:
                    coarse.append(((qbits ^ rbits).bit_count(), cand, rpx))
            coarse.sort(key=lambda t: t[0])
            # 第二轮：在前若干名里用灰度 L1 精排
            fine = []
            for _d, cand, rpx in coarse[:self.COARSE_TOP]:
                l1 = sum(abs(a - b) for a, b in zip(qpx, rpx))
                fine.append((1.0 - l1 / (full * 255.0), cand))
            fine.sort(key=lambda t: -t[0])
            mapping[ch] = fine[0][1]
            detail[ch] = [(s, c) for s, c in fine[:topk]]
        return mapping, detail


# --------------------------------------------------------------------------
# 3.5 认出加密字体的"底稿字体"
# --------------------------------------------------------------------------
# 学习通这个加密字体，本质是「把某个正常字体里的字抠出来，重新编个码位」。
# 它的 name 表往往原样保留着底稿的名字（实测：Source Han Sans CN Normal /
# 思源黑体）。认出底稿后，拿同一个字体来比对，就是像素级精确匹配。

def font_display_name(data: bytes) -> str:
    try:
        tables = _sfnt_tables(data)
        if "name" not in tables:
            return ""
        base, _ = tables["name"]
        _fmt, count, str_off = struct.unpack(">HHH", data[base: base + 6])
        fallback = ""
        for i in range(count):
            pid, _eid, _lid, nid, ln, off = struct.unpack(
                ">HHHHHH", data[base + 6 + 12 * i: base + 18 + 12 * i])
            if nid not in (1, 4, 16):
                continue
            raw = data[base + str_off + off: base + str_off + off + ln]
            try:
                txt = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("latin-1")
            except Exception:
                continue
            txt = txt.strip()
            if nid == 1 and txt:
                return txt
            if txt and not fallback:
                fallback = txt
        return fallback
    except Exception:
        return ""


def _font_family_of(path: Path) -> str:
    try:
        return font_display_name(Path(path).read_bytes())
    except Exception:
        return ""


def find_local_base_font(base_name: str, dirs: list[Path]) -> str | None:
    """在给定目录里找名字对得上的那个底稿字体。"""
    if not base_name:
        return None
    key = re.sub(r"\s+", "", base_name).lower()
    for d in dirs:
        if not d or not Path(d).is_dir():
            continue
        for p in sorted(Path(d).rglob("*")):
            if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            fam = re.sub(r"\s+", "", _font_family_of(p)).lower()
            if fam and (fam == key or fam in key or key in fam):
                return str(p)
    return None


# --------------------------------------------------------------------------
# 4. 一个够用的 HTML 树
# --------------------------------------------------------------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
         "link", "meta", "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "_parts")

    def __init__(self, tag="", attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children: list[Node] = []
        self.parent = parent
        self._parts: list[str | Node] = []   # 保持文字与子节点的先后顺序

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def append_child(self, node: "Node"):
        self.children.append(node)
        self._parts.append(node)

    def append_text(self, s: str):
        if not s:
            return
        if self._parts and isinstance(self._parts[-1], str):
            self._parts[-1] += s
        else:
            self._parts.append(s)

    @property
    def text(self) -> str:          # 兼容旧写法
        return "".join(p for p in self._parts if isinstance(p, str))

    def iter(self, tag: str | None = None):
        if tag is None or self.tag == tag:
            yield self
        for c in self.children:
            yield from c.iter(tag)

    def find_all(self, tag=None, cls: str | None = None, **attrs) -> list["Node"]:
        out = []
        for n in self.iter(tag):
            if cls and cls not in n.classes:
                continue
            if all(n.attrs.get(k) == v for k, v in attrs.items()):
                out.append(n)
        return out

    def first(self, tag=None, cls: str | None = None, **attrs):
        r = self.find_all(tag, cls, **attrs)
        return r[0] if r else None

    def desc_first(self, tag=None, cls: str | None = None):
        """只在子孙里找，不包含自己。"""
        for c in self.children:
            if (tag is None or c.tag == tag) and (cls is None or cls in c.classes):
                return c
            r = c.desc_first(tag, cls)
            if r is not None:
                return r
        return None

    def text_content(self) -> str:
        out = []
        for p in self._parts:
            out.append(p if isinstance(p, str) else p.text_content())
        return "".join(out)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.append_child(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.append_child(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.append_text(data)


def parse_html(html: str) -> Node:
    b = _TreeBuilder()
    b.feed(html)
    b.close()
    return b.root


# --------------------------------------------------------------------------
# 5. 从页面里抽题目
# --------------------------------------------------------------------------

QTYPE = {"0": "单选题", "1": "多选题", "3": "判断题"}


@dataclass
class Option:
    label: str
    text: str


@dataclass
class Question:
    no: str
    qtype: str
    qid: str
    stem: str
    options: list[Option] = field(default_factory=list)


def _clean(s: str) -> str:
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    return s.strip()


# 题干里偶尔混进题型标签、选项里自带 "A、" 前缀
_TYPE_TAG_RE = re.compile(r"【(?:单选题|多选题|判断题|填空题|简答题|论述题)】")
_OPT_LABEL_RE = re.compile(r"^\s*[A-Ha-h]\s*[、.．:：)）]\s*")


def extract_questions(root: Node) -> list[Question]:
    out: list[Question] = []
    for block in root.find_all("div", "singleQuesId"):
        qid = block.attrs.get("data", "")
        inner = block.first("div", "TiMu")
        if inner is None:
            continue

        num_node = inner.first("i", "fl")
        no = _clean(num_node.text_content()) if num_node else ""

        # 题型 + 题干：注意 div.Zy_TItle 自己就带 clearfix，
        # 真正的正文在它里面那个 div.fontLabel 里（不含题号）
        title_div = inner.first("div", "Zy_TItle")
        qtype = ""
        stem = ""
        if title_div is not None:
            span = title_div.first("span", "newZy_TItle")
            if span is not None:
                m = re.match(r"【(.+?)】", _clean(span.text_content()))
                qtype = m.group(1) if m else ""
            body = title_div.desc_first("div", "fontLabel") \
                or title_div.desc_first("div", "clearfix") \
                or title_div
            txt = body.text_content()
            txt = _TYPE_TAG_RE.sub("", txt)
            stem = _clean(txt)
            # body 若是 title_div 自己，会把题号也带进来
            if no and stem.startswith(no):
                stem = _clean(stem[len(no):])

        opts: list[Option] = []
        ul = inner.first("ul", "Zy_ulTop")
        if ul is not None:
            for li in ul.find_all("li"):
                span = li.first("span")
                label = _clean(span.text_content()) if span else ""
                a = li.first("a", "after")
                txt = _clean(a.text_content()) if a else ""
                if not txt and li.attrs.get("aria-label"):
                    txt = _clean(re.sub(r"选择$", "", li.attrs["aria-label"]))
                    txt = re.sub(r"^[A-D]\s*", "", txt)
                # 选项文字里自带的 "A、" / "A." 去掉，避免和前面的标签重复
                txt = _clean(_OPT_LABEL_RE.sub("", txt))
                opts.append(Option(label=label, text=txt))

        # 没有选项的判断题补上「对 / 错」
        if not opts and qtype == "判断题":
            opts = [Option("A", "对"), Option("B", "错")]

        out.append(Question(no=no, qtype=qtype, qid=qid, stem=stem, options=opts))
    return out


# --------------------------------------------------------------------------
# 6. 元数据
# --------------------------------------------------------------------------

def _hidden_values(root: Node) -> dict[str, str]:
    vals = {}
    for n in root.find_all("input"):
        k = n.attrs.get("id") or n.attrs.get("name")
        if k:
            vals.setdefault(k, n.attrs.get("value", ""))
    return vals


COURSE_RE = re.compile(r'"coursename"\s*:\s*"([^"]+)"')
CHAPTER_RE = re.compile(r'"knowledgename"\s*:\s*"([^"]+)"')
WORKTITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')


def collect_meta(paths: dict[str, Path]) -> dict[str, str]:
    meta: dict[str, str] = {}

    cards = paths.get("cards")
    if cards and cards.exists():
        html = cards.read_text("utf-8", errors="replace")
        c = COURSE_RE.search(html)
        if c:
            meta["课程"] = c.group(1)
        for m in WORKTITLE_RE.finditer(html):
            t = m.group(1)
            if t and t not in ("workA", "workB"):
                meta.setdefault("作业名称", t)
                break

    work = paths.get("work")
    if work and work.exists():
        root = parse_html(work.read_text("utf-8", errors="replace"))
        hv = _hidden_values(root)
        for key, label in [("fullScore", "满分"), ("workId", "workId"),
                           ("userId", "userId"), ("cpi", "cpi"),
                           ("classId", "classId"), ("knowledgeid", "chapterId")]:
            if hv.get(key):
                meta[label] = hv[key]

    return meta


# --------------------------------------------------------------------------
# 7. 主流程
# --------------------------------------------------------------------------

def locate(downloaded: Path) -> dict[str, Path]:
    """把「另存为」的目录结构找一遍：主页 / cards.html / work.html。"""
    downloaded = downloaded.resolve()
    files_dir = None
    if downloaded.is_dir():
        htmls = list(downloaded.glob("*.html"))
        files_dir = next((p for p in downloaded.iterdir()
                          if p.is_dir() and p.name.endswith("_files")), None)
    else:
        files_dir = downloaded.with_name(downloaded.stem + "_files")
        htmls = [downloaded]

    out: dict[str, Path] = {}
    if files_dir and files_dir.is_dir():
        for key, name in (("cards", "cards.html"), ("work", "work.html")):
            p = files_dir / name
            if p.exists():
                out[key] = p
        if "work" not in out:  # 兜底：挑一个含 TiMu 的 html
            for p in files_dir.glob("*.html"):
                try:
                    if "singleQuesId" in p.read_text("utf-8", errors="replace")[:400000]:
                        out["work"] = p
                        break
                except Exception:
                    continue
    if "work" not in out:
        for p in htmls:
            try:
                if "singleQuesId" in p.read_text("utf-8", errors="replace"):
                    out["work"] = p
                    break
            except Exception:
                continue
    if htmls:
        out["main"] = downloaded if downloaded.is_file() else htmls[0]
    return out


def to_markdown(questions: list[Question], meta: dict[str, str],
                stats: dict[str, object], source: Path) -> str:
    L: list[str] = []
    title = meta.get("课程", "学习通章节测验")
    if meta.get("作业名称"):
        title += " · " + meta["作业名称"]
    L.append(f"# {title}")
    L.append("")
    L.append(f"> 来源文件：`{source.name}`")
    if meta:
        L.append("")
        L.append("| 项目 | 值 |")
        L.append("|---|---|")
        for k, v in meta.items():
            L.append(f"| {k} | {v} |")
    L.append("")
    L.append(f"**共 {len(questions)} 题**"
             + (f"，解密 {stats.get('decoded_chars', 0)} 个加密字符"
                if stats.get("decoded_chars") else ""))
    L.append("")

    cur_type = None
    for q in questions:
        if q.qtype != cur_type:
            cur_type = q.qtype
            n = sum(1 for x in questions if x.qtype == cur_type)
            L.append("")
            L.append(f"## {cur_type}（共 {n} 题）")
            L.append("")
        L.append(f"**{q.no}.** {q.stem}")
        L.append("")
        for o in q.options:
            L.append(f"- **{o.label}.** {o.text}")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="学习通章节测验：字体解密 + 题库解析 → Markdown")
    ap.add_argument("path", help="另存为下来的 .html，或包含它的目录")
    ap.add_argument("-o", "--out", help="输出 Markdown 路径（默认与输入同名）")
    ap.add_argument("--show-map", action="store_true", help="只打印解密映射表")
    ap.add_argument("--map-json", help="把映射表存成 JSON")
    ap.add_argument("--ref-fonts", help="逗号分隔的参考字体文件名")
    ap.add_argument("--font-dir", action="append", default=[],
                    help="放底稿字体的目录（可重复，默认会自动找 ./fonts 和脚本旁的 fonts/）")
    ap.add_argument("--fix", action="append", default=[],
                    metavar="密文=明文",
                    help="人工纠正某个字，例如 --fix 捗=他（可重复）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.path)
    if not src.exists():
        print(f"找不到：{src}", file=sys.stderr)
        return 2

    paths = locate(src)
    if "work" not in paths:
        print("没找到题目所在的 work.html", file=sys.stderr)
        return 2

    verbose = not args.quiet
    if verbose:
        print(f"题目页：{paths['work']}", file=sys.stderr)

    html = paths["work"].read_text("utf-8", errors="replace")
    fonts = extract_fonts(html)
    if verbose:
        print(f"内嵌字体：{len(fonts)} 个 "
              + ", ".join(f"{f.family}({f.sha1}, {len(f.data)}B)" for f in fonts),
              file=sys.stderr)
    if not fonts:
        print("没有发现加密字体，页面可能没加密", file=sys.stderr)

    # 认出底稿字体：密文字体是从哪个正常字体抠出来的
    base_font = None
    base_name = font_display_name(fonts[0].data) if fonts else ""
    if base_name:
        dirs = [Path(d) for d in args.font_dir]
        here = Path(__file__).resolve().parent
        dirs += [here / "fonts", here / "tools" / "fonts",
                 Path.cwd() / "fonts", Path.cwd() / "tools" / "fonts"]
        base_font = find_local_base_font(base_name, dirs)
        if verbose:
            print(f"底稿字体：{base_name}"
                  + (f"  →  {Path(base_font).name}" if base_font
                     else "  （本地没有，退回用系统字体近似匹配）"), file=sys.stderr)

    root = parse_html(html)
    questions = extract_questions(root)
    if verbose:
        print(f"原始题目：{len(questions)} 道", file=sys.stderr)

    # 用页面上已有的明文当候选补充
    plain = "".join(q.stem + "".join(o.text for o in q.options) for q in questions)

    matcher = GlyphMatcher(ref_fonts=args.ref_fonts.split(",") if args.ref_fonts else None,
                           base_font=base_font, verbose=verbose)
    mapping: dict[str, str] = {}
    detail: dict[str, list[tuple[str, float]]] = {}
    for f in fonts:
        m, d = matcher.build_mapping(f, extra_chars=plain)
        mapping.update(m)
        detail.update(d)

    # 人工纠正
    for item in args.fix:
        if "=" in item:
            k, v = item.split("=", 1)
            mapping[k.strip()] = v.strip()

    # 低置信度的字单独列出来
    unsure = []
    for ch, cands in detail.items():
        if len(cands) >= 2 and cands[0][0] - cands[1][0] < matcher.LOW_MARGIN:
            unsure.append((ch, cands[0][1], cands[1][1], mapping.get(ch, "")))
    if unsure and verbose:
        print(f"  ! {len(unsure)} 个字低置信，建议抽查（密文→采用/次选）：", file=sys.stderr)
        for ch, used, alt, _ in unsure:
            print(f"      {ch} → {used}  (次选 {alt})", file=sys.stderr)

    # 就算没有并列，也把分数最低的几个列出来供抽查
    scored = sorted(((d[0][0], ch, d[0][1]) for ch, d in detail.items() if d))
    if scored and verbose:
        weakest = scored[:3]
        if any(s < 0.90 for s, _c, _v in weakest):
            print("  · 把握最低的几个字（分数 密文→采用）："
                  + "  ".join(f"{s:.2f} {c}→{v}" for s, c, v in weakest), file=sys.stderr)

    if args.map_json:
        Path(args.map_json).write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        if verbose:
            print(f"映射表已写入 {args.map_json}", file=sys.stderr)

    if args.show_map:
        for k, v in mapping.items():
            cands = " / ".join(f"{c}({s:.3f})" for s, c in detail.get(k, []))
            print(f"{k} U+{ord(k):04X} -> {v}\t[{cands}]")
        return 0

    # 还原
    def fix(s: str) -> str:
        return "".join(mapping.get(c, c) for c in s)

    for q in questions:
        q.stem = fix(q.stem)
        for o in q.options:
            o.text = fix(o.text)

    meta = collect_meta(paths)
    for k in list(meta):
        meta[k] = fix(meta[k])

    out_path = Path(args.out) if args.out else src.with_name(
        (src.stem if src.is_file() else src.name) + "_题目.md")
    md = to_markdown(questions, meta,
                     {"decoded_chars": len(mapping)}, src)
    out_path.write_text(md, encoding="utf-8")
    print(f"完成：{out_path}  （{len(questions)} 题，解密 {len(mapping)} 个字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
