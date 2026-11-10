# 学习通章节测验离线解密器

把浏览器"另存为"下来的学习通页面 → **自动解密字体混淆** → 解析成 Markdown 题库。

不需要登录、不联网、不发任何请求，纯本地跑。

---

## 一、它能做什么

```
学生学习页面.html          ←  你在浏览器里 Ctrl+S 存下来的整页
        │
        ├─ 找出 *_files/work.html（题目真正的容器）
        ├─ 抠出内嵌的 base64 字体 → 解出被混淆的 96 个汉字
        ├─ 按 DOM 结构抽出题干 / 选项 / 题号 / 题型
        └─ 输出 测验题库.md
```

实测：**40 道题（15 单选 + 7 多选 + 18 判断）全部还原，96 个加密字符准确率 96/96**。

---

## 二、原理：为什么源码里是乱码

学习通在作业页里塞了一段 `@font-face`：

```css
@font-face{
  font-family:'font-cxsecret';
  src:url('data:application/font-ttf;charset=utf-8;base64,AAEAAAAMAIAAAwBAQkFTRRuOGNgA…')
}
```

服务端把正文里的部分汉字**换成生僻字**（挙 挚 挒 捘 挕 挔 …），再让这个自定义字体把生僻字的字形**画成原字的样子**。于是：

| 视角 | 看到的内容 |
|---|---|
| 屏幕上 | `（）是人们对生活在其中的世界…` ✅ 正常 |
| 源码 / 复制粘贴 / 爬虫 | `（）是人们对挙活在挒挘的挕挔…` ❌ 乱码 |

**关键点：字体是内嵌在 HTML 里的，等于锁和钥匙一起给了你。**

解密就三步：

1. **抠字体** — 正则抓 `data:...;base64,` 后面那段，base64 解码得到 `.ttf`
2. **读码位** — 解析 ttf 的 `cmap` 表，拿到全部被替换的码位（本例 96 个）
3. **认字形** — 用这个字体把每个码位渲染成图片，再拿去和正常字体渲染的候选汉字比余弦相似度，最像的那个就是明文

第 3 步本来最容易翻车（字形相近的字会认错），但这个加密字体的 `name` 表里明明白白写着底稿字体：

```
nid= 1  'Source Han Sans CN Normal'
nid= 1  '思源黑体'
```

所以只要把**思源黑体 CN Normal** 放进来当参照，匹配就变成近乎精确比对——正确字和参照字几乎是同一个字形，汉明距离接近 0，第二名差一大截。

---

## 三、环境

只需要一个库：

```bash
pip install pillow
```

Python 3.9+。（**不需要 numpy**，位运算做汉明距离已经够快。）

---

## 四、怎么运行

### 1. 打开终端，进到本目录

```powershell
cd "F:\DeepSeekHarness\学习通网站解析"
```

### 2. 跑

```powershell
python chaoxing_quiz_decoder.py 学生学习页面.html
```

屏幕上会看到：

```
题目页：...\学生学习页面_files\work.html
内嵌字体：1 个 font-cxsecret(67d7e5bf4651, 22436B)
底稿字体：Source Han Sans CN Normal  →  SourceHanSansCN-Normal.otf
原始题目：40 道
  · 首次运行：渲染 3851 个候选字 × 1 种字体（约 1 分钟，之后会缓存）…
      SourceHanSansCN-Normal.otf 完成 (16s)
完成：F:\DeepSeekHarness\学习通网站解析\学生学习页面_题目.md  （40 题，解密 96 个字符）
```

同目录下就会多出一个 md 文件，用任何 Markdown 编辑器打开即可。

**耗时**：首次约 20 秒（渲染 3800 多个候选字），之后有缓存，**约 2 秒**。缓存放在系统临时目录，删掉也不影响。

### 3. 常用参数

```powershell
# 指定输出文件名
python chaoxing_quiz_decoder.py 学生学习页面.html -o 题库.md

# 只看解密映射表（排查用）
python chaoxing_quiz_decoder.py 学生学习页面.html --show-map

# 把映射表导成 JSON，给别的脚本复用
python chaoxing_quiz_decoder.py 学生学习页面.html --map-json map.json

# 某个字认错了，手动改
python chaoxing_quiz_decoder.py 学生学习页面.html --fix 捗=他

# 静默模式（不打印过程）
python chaoxing_quiz_decoder.py 学生学习页面.html --quiet
```

输入也可以直接传整个目录（含 `xxx_files/` 的那个文件夹）。

> **`python` 找不到？** 换成 `py` 或完整路径，例如
> `C:\Users\你\AppData\Local\Programs\Python\Python312\python.exe`。
> 先确认 `python -c "import PIL; print(PIL.__version__)"` 能打印版本号。

---

## 五、关于底稿字体（重要）

脚本会自动读内嵌字体的 `name` 表，然后在这些位置找同名字体：

1. `--font-dir` 指定的目录
2. 脚本旁边的 `fonts/`
3. 当前目录的 `fonts/` 和 `tools/fonts/`

仓库里已经放了 `fonts/SourceHanSansCN-Normal.otf`（思源黑体，Apache-2.0，可自由分发），所以开箱即用。

**如果找不到底稿字体**，脚本会退回"拿系统里十几个中文字体各算一遍、取最高分"的策略——仍能还原，但准确率会掉到 95% 左右，偶尔会把「学」认成「字」这种。此时用 `--fix` 补一下即可。

---

## 六、输出长什么样

```markdown
# 思想道德与法治（智慧课程） · 第一章测验

| 项目 | 值 |
|---|---|
| 课程 | 思想道德与法治（智慧课程） |
| 作业名称 | 第一章测验 |
| 满分 | 100.0 |
| workId | 56564540 |
| classId | 151647229 |
| chapterId | 1259228378 |

**共 40 题**，解密 96 个加密字符

## 单选题（共 15 题）

**1.** （）是人们对生活在其中的世界及其人与世界关系的总体看法和根本观点？

- **A.** 人生观
- **B.** 价值观
- **C.** 恋爱观
- **D.** 世界观
```

---

## 七、局限

- **只管题目，不管答案**。学习通判题在服务端，页面里 `answer{id}` 全为空值，没有正确答案字段。
- 只处理**字体混淆**这一种。页面上的验证码、人脸抓拍、加密签名参数（`enc` / `matchEnc` / `uploadEnc`）是提交环节的事，跟解析无关。
- 依赖 `work.html` 里的 DOM 结构（`div.singleQuesId` / `div.TiMu` / `ul.Zy_ulTop`）。学习通改版后需要跟着调。

---

## 八、文件说明

| 文件 | 说明 |
|---|---|
| `chaoxing_quiz_decoder.py` | 主脚本，单文件，可独立拷走 |
| `fonts/SourceHanSansCN-Normal.otf` | 思源黑体（解密用的参照字体） |
| `测验题库.md` | 本仓库页面的解析结果 |
| `学习通页面解析报告.md` | 对整个页面的逆向分析笔记 |
| `学生学习页面.html` + `学生学习页面_files/` | 你的原始网页快照 |
