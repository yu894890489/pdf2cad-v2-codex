# 输电塔杆件图纸 → 可核对的三维框架

完整流程的安装、上传、接口、输入条件和可选视觉服务配置见 [新图纸完整处理说明](PIPELINE_GUIDE.md)。当前本地工作台：`http://127.0.0.1:8774/`（`runs/studio-complete`）；启动默认仍为 8770，可用 `-Port` 指定。

可复用 Python 程序，入口为 `tower-rebuild` / `python -m tower_rebuild`。通用代码不依赖第 9 张图的件号、坐标和面数。原来的 `build_sheet9_rods.py` 等保留为旧实现，不由新流程调用。

**当前入口（2026-10-28）：本地上传工作台 + 完整本地流水线。** 一张新 PDF 会依次经过输入检查、必要时的本地 OCR 与文字笔画分离、BOM/表格分离、图元分类、逐面拓扑、重叠段恢复、尺寸初审、识别完整性检查、基础三维预览、三维尺寸复核、可选视觉复核和全流程终检。角钢采用 BOM 的 L 形截面；有原生板形依据时生成有厚度的连接板，并保留孔位与疑点。每张图纸单独保存来源、数组、模型和报告。

**验收目标：杆件件号、数量、连接关系和主要尺寸准确；所有疑点列出。** 当前提供逐面候选实体和多图纸空间装配工作台。装配会区分四侧面闭合环与完整封闭多面体，并检查端点间隙、角度、前后面平行关系和面穿插；不保证任意新图纸自动准确复原。无依据的板形、孔径、杆端或零件对应不会以已确认的实体呈现。以下独立 Qwen/vLLM 装配流程仍保留；其早期真实图纸自动装配未通过验收，记录见 `output/REUSABLE_PIPELINE_VALIDATION.md`。

## 启动当前工作台（8774）

在 PowerShell 中运行以下命令，加载已有图纸、处理结果和装配项目：

```powershell
Set-Location 'F:\workspace\orca\pdf2cad-codex'
.\start-local.ps1 -Root 'runs/studio-complete' -Port 8774 -Config 'examples/workflow.native.json'
```

打开 [本地工作台](http://127.0.0.1:8774/)。`runs/studio-complete` 是当前历史数据目录，`workflow.native.json` 使用本地处理配置。服务监听 `0.0.0.0:8774`，本机通过上述地址访问；运行时保留终端，按 `Ctrl+C` 停止服务。

也可以在同一项目目录直接使用 Python 启动：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild studio --root runs/studio-complete --port 8774 --config examples/workflow.native.json
```

## 最终模型 BOM

自动装配和反算结束后生成 BOM。装配页点击 **当前模型 BOM**，可查看当前已保存模型的结果，下载 BOM CSV、逐组件长度明细 CSV 和完整 JSON。CSV 使用 UTF-8 BOM，可直接用 Excel 打开。导出时重新量取当前几何，支持反算修正、撤销、比例调整后的结果；不会重新求解或移动模型。

表格保留原图的件号、规格、长度、数量、单重、总重和备注，新增最终模型长度、最大绝对长度差、模型观察数、修正观察数、关注原因，最后一列为 **是否需要人工关注**。原表完整数据另附在页面和 JSON 中；不能唯一对应的重量、备注留空并提示。

- 杆件长度从最终三维端点计算，包含已应用的反算补长或截短，不使用缓存的原识别长度。同件号有多个长度时全部列出，不取平均；明细可定位到具体结构面和组件。
- 不同 PDF 的件号分别统计。原图数量保留，跨视图观察数不当作实物数量。
- 连接板提供当前板形包络长边，单独标明口径，不与原图下料长直接作差。未生成的模型长度留空。
- 缺失组件、未确认端点、反算候选、未通过接缝检查、同件号长度冲突及关联单线图未通过，都会提示人工关注。默认 3 mm 是长度差诊断阈值，不是加工公差。

已有流程结果可单独导出（不重跑识别、装配）：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild final-bom --out runs/new-drawing
```

输出位于 `site/final-bom/`。此命令读取流程保存的自动装配快照；手动编辑后的结果应从装配项目的“当前模型 BOM”导出。旧项目也可直接打开此入口，无需重新上传。

## 零件放样 DWG

在装配页或“最终模型 BOM”点击 **零件放样 DWG**，再点击“生成当前模型的 DWG 放样图”。服务在后台逐件生成、转换和回读验证，页面显示进度，可逐件下载 DWG/DXF、预览 SVG，或下载全部 DWG 的 ZIP。

- 按图纸、件号、规格、最终模型长度及轮廓/孔位分组。不同长度不平均；同规格同长度但轮廓/孔位不同仍分别出图。完全相同的跨视图几何只合并观察记录，原图数量不会累加，也不擅自分配到长度变体。
- 角钢图包含两肢侧视图、L 形截面、总长及截面尺寸；所有几何为毫米、模型空间 1:1。侧视图取当前模型端截，不凭空补出切角、火曲、圆角或钻孔。尚未确定的杆件孔所属肢面会提示待核对。
- 连接板保留真实模型多边形，标注长宽、厚度和顶点坐标。仅输出有明确孔径依据且完整位于板内的模型孔，给出孔中心坐标与直径。缺少有效板形或比例的零件记录在“未生成项”，不用 BOM 矩形代替。
- 输出是真正的 DWG 2018（AC1032）。每份文件都会重新打开并转回 DXF，核对毫米单位、零件轮廓、截面和孔位。失败文件不会放进成功 DWG 下载包。
- 当前模型的端点、切角、火曲和部分孔位尚未确认，图内保留“是否需要人工关注：是”及具体原因。这里是模型候选放样，不是已确认的加工下料图。
- 人工关注原因沿用件号级 BOM 汇总，包括此件号其他结构面的未确认观察；某个变体已出图时，其汇总中仍可能保留其他面缺失板形等记录。
- 生成期间可以继续查看模型；结果绑定生成开始时的模型快照。模型几何变化后页面提示过期，旧文件保留，重新生成得到新版本；移动和旋转整个零件不会改变其放样尺寸。

DWG 转换需要本机已安装并可运行的 **AutoCAD Core Console**。中文标注使用黑体 `simhei.ttf`，其他电脑查看 DWG 时也需有该字体；网页 SVG 预览已转成字形轮廓。程序从安装注册表自动寻找 `accoreconsole.exe`，也可以在启动服务前指定：

```powershell
$env:TOWER_ACCORECONSOLE = 'D:\Program Files\Autocad2025\AutoCAD 2025\accoreconsole.exe'
.\start-local.ps1 -Root 'runs/studio-complete' -Port 8774 -Config 'examples/workflow.native.json'
```

CAD 转换在本地隐藏进程中完成，使用隔离配置，不上传图纸。没有转换器时保留 DXF 和明确的 DWG 未生成状态，不更改文件扩展名冒充 DWG。现有流程保存的自动装配快照可通过命令行导出；手动调整后的模型从装配页导出：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild part-drawings --out runs/new-drawing
```

命令行输出位于 `site/part-drawings/<版本ID>/`；工作台输出位于数据目录的 `part-drawings/<装配ID>/<版本ID>/`。每个文件名包含件号、规格、模型长度和变体标识，`report.json` 保留全部来源、合并观察和未生成原因。

## 最简单的使用方式：上传新图纸

首次在新电脑安装（推荐 Python 3.12）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
# 支持 CAD 将文字转为线条的 PDF 时安装本地 OCR：
.\.venv\Scripts\python.exe -m pip install -e ".[ocr]"
```

在项目根目录启动：

```powershell
.\start-local.ps1
```

打开 **http://127.0.0.1:8770/**，选择或拖入一个或多个 PDF，点击“开始处理”。图纸依次在本机处理，页面显示实际阶段；完成后点击“打开图纸与三维”，再点击“显示三维模型”。上传页面也提供 BOM 数组、图元分类、尺寸初审、检查报告和模型数据。关闭终端或按 Ctrl+C 停止服务；未完成的任务标记中断，可从历史记录重新处理。

- 上传文件仅保存至本机 `runs/studio/<任务ID>/`。文件名相同也不会覆盖；重试创建新的任务目录，原结果保留。
- 默认不调用 Qwen、vLLM 或任何云端接口；网页的三维库也在本地。
- 处理失败会显示阶段与原因，并提供本地日志；支持 100 MB 以内 PDF。扫描件、加密文件、旋转元数据及未读出 BOM 等情况明确提示，不将空模型算作成功。
- 当前服务按本机配置监听 `0.0.0.0`，本机访问使用 `127.0.0.1`。修改端口或结果位置：`.\start-local.ps1 -Root 'F:\drawing-results' -Port 8772`。

### 不使用上传页面：一条命令处理新图

```powershell
.\run-pipeline.ps1 -Pdf 'F:\图纸\新图纸.pdf'
# 或指定新的输出目录，只生成文件
.\run-pipeline.ps1 -Pdf 'F:\图纸\新图纸.pdf' -Out 'runs\another-drawing' -NoServe
```

未指定 `-Out` 时自动创建时间戳和随机 ID 组成的目录。完整流程只接受新的或空目录，避免新图纸混入旧模型。需要重算已有结果时，使用下面介绍的分阶段命令。高级表格区域和二维复核输入可通过 `-TableRegions`、`-Review` 传入。

Python 调用入口：

```python
from tower_rebuild.workflow import process_drawing

result = process_drawing("drawing.pdf", "runs/new-job")
print(result["status"], result.get("counts"), result.get("error"))
```

等价命令：`python -m tower_rebuild process drawing.pdf --out runs/new-job`。任务状态与每阶段耗时写入 `workflow.json`；`needs_review` 表示已生成候选与报告，`no_model` 表示没有足够实体依据，`unsupported` 表示输入需要补充处理，`failed` 表示运行失败。前三者退出码为 `2`，失败为 `1`，PowerShell 脚本接受 `2` 并输出实际状态。完整流程不自动输出“工程验收通过”。

上传 API：先 `GET /api/session`，以返回的 token 作为 `X-Upload-Token`，`POST /api/jobs` 的正文为 PDF 字节，`X-File-Name` 为 URL 编码后的文件名。`GET /api/jobs/<ID>` 查询阶段、状态和结果链接；`POST /api/jobs/<ID>/retry` 创建独立重试任务。网页和命令行共用 `process_drawing`，没有第二套手工生成逻辑。

### 将多个结构面组成横担层

每批上传的一个或多个 PDF 会自动建立一个装配项目。也可在历史图纸中勾选多个任务，点击“将选中图纸联合装配”。点击“打开三维装配”进入同一个本地三维窗口：

1. 自动寻找边界上的共同件号，核对规格、BOM 长度和观察边长，再尝试 90° 刚性连接形成四面环。额外端面须有至少两条公共边同时匹配才自动加入。
2. 正视参考面垂直地面，Z 轴向上；明确的前/后件组拆为两个面并检查平行关系。可重新指定正视面，特殊角度通过手动旋转或边连接指定。
3. 不能闭合时明确提示补充图纸；点击“补充图纸”上传更多 PDF，或加入已处理任务。不同图纸的 BOM、面和边分别保留来源；相同 PDF 内容去重。
4. 所有识别出的面均进入窗口。选择“拖动面”可自由移动；右侧可输入 XYZ 位置和旋转角度。选择“选择连接边”，依次点击 A、B，输入角度后连接。默认移动整个已连接组；边长不符会显示间隙，不拉伸杆件。
5. 未定比例的视图仅放大显示原矢量线条，不能按尺寸连接；可输入已知尺寸推算的 mm/pt 比例。复选框控制哪些面参与闭合检查，其他面仍保留供查看。
6. 布局自动保存在工作台根目录下的 `assemblies/<项目ID>/project.json`，完整面/边/约束和检查结果在同目录 `assembly.json`，可导出/导入布局 JSON。
7. “装配依据与待核对项”列出已有视图缺项、公共杆件、杆长/BOM 差异、重合轴线，以及横向剖面与前后平行规则的冲突。凸包内部的公共杆件也可用于定位连接，但不冒充外边界闭合。
8. 可以先“预览连接”，查看位置、端点间隙和推定边界穿插；预览不保存，确认连接后才写入布局。支持最近 30 步撤销/重做和“聚焦连接组”。加入新来源、更新源模型或修改比例会清空旧撤销记录，避免恢复到过期几何。
9. “三维件号标牌”默认显示所选面，可切换为“全部面”。件号采用杆件旁的双面实体小牌，随结构面旋转、随远近缩放，并受前方构件遮挡；前/后件保留前缀。“标牌大小”滑块可调整大小，放大模型可看清小字。点击标牌高亮对应杆件并查看规格、BOM 长和模型轴长；相同件号的不同观察独立保留。标牌单独存放在显示层，不参与实体尺寸、包围盒或布局保存。
10. 对横向逐渐收窄的结构，在“横向收口校正”中指定尖端在正视图左侧或右侧。程序联合前后件面及 2–4 个横向剖面的公共杆件，搜索端点方向与非直角姿态。无需先形成完整四面环，可先预览再保存局部装配；“俯视连接组”用于检查收口方向。正视面保持原位，其余无关面的布局保持不变，保存后可撤销。
11. 收口定位只采用同一来源内、BOM 数量为 1、规格与长度一致且对应唯一的公共杆件；重复实物、跨图同号尚无映射或约束不足时保留人工处理。求解保持原始轴长和比例，仅作刚性旋转/平移。候选残差上限为杆长的 3%（短杆至少 3 mm），**不作为验收公差**；接缝仍按 3 mm / 1° 严格检查，超差继续列出。收口宽度取参考杆件的共同纵向区间，不代表整根横担的端部尺寸。
12. 某个结构面因识别截短而拼不上时，在“缺失面反算与原图回查”选择该面。程序先排除该面的连接，用至少两个已连接邻面的唯一公共件号定位，再由两条公共长杆的端点计算候选四边形、两端宽度、对角线、共面残差和对地倾角。预测不读取待查面的轴线坐标；仍列出邻面轴长与 BOM 的差异和既有接缝超差，不能把当前装配当作已验证的设计基准。
13. 随后读取哈希校验过的原生线条与杆件观察，检查杆件两侧轮廓是否都超出识别段。双侧支持时生成外包轴候选并作不缩放的刚性试拼，保留板下断线、斜切端点等疑点。点击按钮还会按邻面反算轮廓修正**整组杆件并直接放置**：公共边端点落在反算面上，其余杆件通过四角透视映射保持直线关系；明显截短的通长斜杆，若延长线同时交于两条边杆且长度与 BOM 相差不超过 15%，尝试补至交点。这个 15% 是候选筛选范围，**不是尺寸验收公差**。修正可能改变长度和夹角，不修改角钢截面尺寸。两公共杆对应不唯一、轮廓退化、已有连接冲突或邻面端点离最佳平面超过 3 mm 时保留诊断并说明不能直接修正的原因。
14. 黄色为保留的实体杆段，橙色为补长实体段，红色虚线为截去段（独立显示层，可关闭，不计入实体尺寸或包围盒）。长度分段以映射后的观察中点为基准：保留 + 截去 = 原轴长，保留 + 补长 = 修正轴长；既有轮廓调整，也有推断补段，不能把橙色一律当作原图确认的漏段。所有杆件保留原端点、原长、BOM、修正长、补长/截短量及推断依据；板形和孔中心随面移动，板厚、孔径保持原值，仍需核对。修正保存在 `scene.face_corrections`，可撤销/重做、导出/导入、刷新和重启恢复；重复计算从原始几何开始，不叠加长度改动。原 PDF、BOM 和上游识别模型保持原值。源数据或比例变动后修正失效，必须重新反算。
15. 缺失面选择框始终列出结构面，并区分“可反算”和“待连接 / 校核”；未建立连接、缺少两个独立邻面或比例未定时显示具体原因。选中项按项目在浏览器保存，刷新后保留；没有本地选择时使用上次回查的面。
16. “按公共件号尝试装配”显示运行中和最终结果。未找到完整四面环时，若已选择尖端方向（或该正视面此前有明确选择），继续尝试多剖面局部收口定位；未知方向不自动猜测。失败保持布局并说明原因，重复得到相同布局不占撤销记录。重新排列面后保留该项目此前的尖端方向；源数据变化会重新校验。正视面与无关连接组的位置保持原值。
17. 回查输出保存在装配目录：`missing-face-source.svg/png` 为原图证据，`missing-face-corrected.svg` 为含件号和颜色分段的修正结构图，`missing-face-review.json` 为完整计算依据。原图外包轴刚性试拼与反算轮廓可单独开启。移动面使旧回查依据失效，但已保存的杆件修正仍保留。对齐间隙很小也不等于 BOM 已通过；界面继续显示原始差异、修正后 BOM 差异和既有接缝超差。此步骤不需要额外视觉服务，原矢量不足时推断与原图证据分别记录。
18. **上传/命令行完整流程默认自动执行装配与反算补全**。新增 `assembly` 阶段：四面环搜索 → 多剖面收口求解（唯一方向才自动采用）→ 尚未定位面的反算修正与放置 → 杆长/BOM、接缝、完整闭合复核。输出 `site/assembly/assembly.json`、`report.json` 和本地三维页面；全流程终检包含装配后的尺寸检查。工作台自动创建或更新对应装配项目，可继续人工编辑。界面“自动处理结果”列出已连接/修正面数、无法定位的原因、尺寸差异和逐面反算记录。配置及复用接口见 [完整流程说明](PIPELINE_GUIDE.md)。自动计算仅生成可追溯候选，识别截短或误配妨碍定位时保留全部面供核对，不宣称组装成功。

此装配阶段不需要 vLLM。自动结果属于几何候选：同件号可能对应多个实物，外包多边形不一定是实际结构边界；板厚偏置、角钢肢方向、物理数量、孔与连接板仍需原图复核。四面环搜索和多剖面收口定位分别提供；缺少证据的复杂结构仍使用手动装配。“已连接面”计入存在间隙的已摆放面，尺寸合格接缝另行统计。验证记录见 [装配测试](output/ASSEMBLY_VALIDATION.md) 和 [第 10/11 张实测](output/ASSEMBLY_10_11_VALIDATION.md)。

### 单线图整体尺寸检查与修正

完整流程新增“单线图尺寸检查”入口：单线图原标注作为整体尺寸基准，先确认尺寸边对应的结构面与件号，再计算杆件投影长度、端套有效伸出、搭接扣减和退距。水平尺寸使用水平投影，竖向尺寸使用竖向投影，不能用斜杆全长直接相加。

- 自动检测单线图数值及两端尺寸界线；没有尺寸界线依据的数字不当作已确认尺寸，圈号与虚线不能充当整体尺寸。
- 支持组合整体尺寸。例如第 9 张横担层下边（排除塔身梯形）以 **7000 + 3364 / 2 + 480 / 2 = 8922 mm** 为基准。`derived_dimensions` 引用原尺寸 ID 与系数，不使用任意公式求值。
- `reference_only_dimensions` 可列出塔身等仅作参考的原尺寸 ID；组合公式中的原尺寸也默认只作参考，避免重复要求它们各自对应一根横担杆件。原值和原图仍保留。
- 页面可下载包含原尺寸和杆件观察 ID 的模板，填写 `bindings` 后在页面提交；系统创建独立检查任务，保留原模型与人工布局。
- 端部项必须指向原图数字 `text_id` 或已确认采用全长的 `bom_mark`。`end_extension` 加、`overlap` 和 `setback` 减；值均为非负的**尺寸方向贡献量**。端套总长不等于有效伸出量，不能用未确认余差填入端部项。
- `correspondence_confirmed: true` 只确认对应；`end_basis_confirmed: true` 表示端部尺寸链已完整。端部未知时仍显示已对应模型段与整体基准的差值，但保持待定。
- `correction` 指定待修正的杆件观察 ID、固定端 `anchor: "a"` 或 `"b"`，以及 `apply: true`。只有端部尺寸链、单线图、BOM 和原生双侧延伸线/端面线均支持时，才改模型轴线。多个冲突约束、同轮廓不同长度件号混淆、无原图支持或反算杆长与 BOM 不符都会阻止应用。修正前轴线及依据保留，重建从原始识别开始。
- 三维尺寸复核和装配终检重新量测实际模型，包括装配反算修改过的杆件。使用同一单线图尺寸修正后的数值吻合会标记为依据复用，不算独立准确性证明。

对应格式见 [单线图对应示例](examples/schematic-mapping.example.json)，文件中的哈希和 ID 必须从当前结果的 `site/schematic/template.json` 复制。示例中的数值是格式演示，并非第 9 张端套尺寸。

已有结果可在命令行更新：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild audit --out runs/new-drawing --schematic-map review.json
.\.venv\Scripts\python.exe -m tower_rebuild preview --out runs/new-drawing
```

第一条计算尺寸链，第二条执行有依据且明确指定的修正、模型复核、装配及终检。结果位于 `site/schematic/`；`report.json` 是原标注/BOM 对照，`model-check.json` 含实际模型差值、结构图遗漏检查及修正记录。新图上传默认生成待确认对应，不能按“最接近的 BOM 长度”自动确定件号。

### 多页图纸、重复件号与输入检查

每次完整流程会生成 `input-review.md`。上传页面的“输入检查”可以查看各页原生路径、文字和大幅栅格图片覆盖情况。部分页面可提取不代表所有页面已完整识别；混合矢量/扫描 PDF 中未覆盖的页会单独列出。

读取 BOM 时另存 `site/tables/bom-observations.json`，保留每条有效行的页码、原页位置、规格、长度和数量；`bom-check.json` 检查是否能安全转换为后续拓扑使用的唯一件号清单：

- 同一位置被提取多次只算一次观察。
- 不同位置重复件号，即使数值相同，也不自动假定为同一份 BOM 或累加数量。
- 规格、长度、数量有冲突，或表格中存在不完整行时，完整流程停在 BOM 阶段，保留证据和检查报告，状态为 `unsupported`。
- 当前文档级拓扑要求件号含义唯一。若多页实际是独立图纸且各自重新编号，请拆为单独 PDF 分别上传；共享 BOM 的跨页归属仍需要确认，不能用自动覆盖消除歧义。

输入检查通过只表示满足当前提取条件；实体数量、连接和主要尺寸仍按逐面检查报告复核。

完整源码包：`output/tower-rebuild-source.zip`。包含代码、本地网页与三维库、启动脚本、示例、测试及说明；不包含用户 PDF、密钥或运行缓存。

## 已实现的流程

### 先拆表格，再分解结构图元

独立入口，不调用 Qwen / vLLM。先对新图执行这一步，保存后续识别可复用的基础数据：

```powershell
.\run-decompose.ps1 -Pdf 'F:\你的图纸\新图纸.pdf' -Out 'runs\new-drawing-decomposed'
```

运行后打开 `http://127.0.0.1:8768/`。页面分为“结构图 · 图元分类”和“表格 · 数据数组”，可筛选图元类型、圈号用途、件号，点击图元查看原始坐标及对应 BOM。点击 BOM 件号可定位相关圈注。所有资源来自本机。

也可仅生成文件：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild decompose 'F:\你的图纸\新图纸.pdf' --out runs/new-drawing-decomposed
```

主要文件：

| 文件（相对输出目录） | 内容 |
|---|---|
| `site/tables/bom.json` | **JSON 数组**；件号、原规格、下料长度、数量、材料分类、原页证据和来源表格 |
| `site/tables/tables.json` | **JSON 数组**；构件表、螺栓/脚钉/垫圈表、标题栏的二维单元格数组，附坐标和原生文字 |
| `site/structure/primitives.json` | **JSON 数组**；结构区全部原生图元，保留原路径命令、样式、坐标和分类依据 |
| `site/tables/primitives.json` | 表格区原生图元；与结构区图元互斥且合起来完整覆盖源文件 |
| `site/structure/classification.json` | 按几何类型和用途分类的图元 ID 索引 |
| `site/structure/callouts.json` | 圆圈内数字与 BOM 对应，保留 `前` / `后`，角钢、板件分别标记 |
| `site/structure/dash-candidates.json` | 连续短线形成的虚线候选，保留全部线段与判定依据 |
| `site/pages/p001-tables.png` | 表格部分，保持整页坐标 |
| `site/pages/p001-structure.png` | 去除表格的结构图部分，保持整页坐标 |
| `site/summary.json` / `partition.json` | 统计、分区边界、完整性校验、待确认项 |

图元先按直线、圆、圆弧、贝塞尔曲线、矩形、四边形、多边形、折线、复合路径、文字、栅格图片分类，再标注角钢圈号、板件圈号、尺寸数字候选、规格文字等用途。**一个 PDF 绘图对象不等于一根杆件。** 同一条线的轮廓与填充可能各有一份，当前保留原始绘制对象，尚未推断杆件端点、长度或连接关系。

图元 `bbox_pt`、原路径和几何坐标单位为 **PDF 点**（原页左上原点，X 向右、Y 向下），不当作工程毫米。`bbox` 为原页归一坐标；BOM 的 `length_mm` 才是原表下料长度。原生虚线与根据短线重复模式推断的虚线候选分开记录，后者不自动删除。

本阶段校验：所有原生路径保存、ID 唯一、两部分互斥且完整、源 PDF 哈希一致、BOM 值与独立裁剪后提取的表格数组逐项相符。原始表格中的 `null` 表示合并延续，`""` 表示空单元格。没有完全进入单元格的原生文字会另外保存并标出。

当前原生分解适用于含可读文字和矢量路径的 PDF。扫描图会保留为栅格图元，并提示需要 OCR / 矢量化，不会宣称已拆出图片内的线条。若表格自动边界不合适，可使用 `--table-regions regions.json` 提供数组，例如：

```json
[{"page":1,"kind":"bom","title":"构件表","bbox_pt":[100,100,400,700]}]
```

查看浏览器及分区检查：

```powershell
.\.venv\Scripts\python.exe tools/check_decomposition_viewer.py --out runs/new-drawing-decomposed
```

该检查还会逐像素验证“表格部分 + 结构部分”可重组为原页图像。矢量提取基于 [PyMuPDF 的路径接口](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_drawings)；原生路径保留，圆弧等拟合分类同时记录拟合误差。

### 后续识别与三维装配

1. **导入 PDF**：计算 SHA-256，渲染整页与高分辨率裁剪，保留文本坐标和矢量路径。扫描 PDF 通过图片识别；文本提取为空不等于没有内容。
2. **分区后先读 BOM**：矢量 PDF 优先按原生表格单元格读取，依据表头重新定位边界，拆分缺少分隔线的合并行。没有可读文字时再对长表分条识别。保留每行原规格、长度、数量和原页证据；重复行冲突必须核对。
3. **确定材料类型**：`Q345L...` / `L...` 识别为角钢，`Q345-...` / `-厚×宽` 为板件；圆圈单双不作为材料类型判据。未知规格报错。
4. **逐面识别**：用实际墨迹连通区域修正模型提出的主要结构面裁剪框，再提取实线杆件、件号、端点和标注尺寸。虚线/参考线不可作为实体。交叉点不自动当作连接点。
5. **按实物实例拼接**：同一个件号可有多根杆件；跨面合并需要共享杆件实例及一致端点。正视图作为竖直的 X/Z 基准面；其余面的空间位置由共享节点、长度、方向关系约束求解。
6. **数值校核**：BOM 数量、所有实体边覆盖、件号对应、跨面同杆同端点、尺寸与投影偏差、连通性、雅可比秩/剩余自由度、多初值装配歧义。任何超差都保留，不用稳健损失掩盖。
7. **第二轮视觉复核**：对每个面复看件号、实线、尺寸，发现问题加入待核对列表。这仍是模型复核，不视为独立证明。
8. **本地网页与核对记录**：逐面原图/绿色模型投影对照，查看每个实例的 BOM 长度和节点跨度。无法装配时显示橙色二维识别叠图，明确标为尚未形成三维；问题按类型分组。人工核对记录绑定输入与求解结果哈希，修改后旧记录失效。

## 安装

推荐 Python 3.12，使用项目虚拟环境，不改全局 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

本工作区已创建 `.venv` 并安装。若终端的 `python` 不是目标版本，可把首行替换成完整 Python 路径。

## 你指定的阿里云接口

配置文件：[`examples/qwen.aliyun.json`](examples/qwen.aliyun.json)

- 协议：Anthropic Messages，地址 `https://coding.dashscope.aliyuncs.com/apps/anthropic`。
- 模型：`qwen3.7-plus`。
- 默认使用 `anthropic_output_mode: "tool"` 返回结构化观察数据，兼容服务把工具数据标为 `end_turn` 的情况；也保留 `json_schema` 模式。这里只解析数据，不执行模型返回的工具或命令。
- 密钥只读本机环境变量 `QWEN_API_KEY`，不写入 JSON、日志或浏览器。
- 该配置显式允许向指定接口发送文字和图纸裁剪图片。网页、模型、报告和缓存保存在本机。
- 默认的 vLLM 配置只允许本机地址，不会自动切到云服务。

先在本机安全地设置这个接口对应的密钥，再检查连接：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild probe --config examples/qwen.aliyun.json
```

`401` 表示密钥无效或不适用于该接口。Coding 接口与其他百炼接口的密钥/权限需对应。程序不会自动尝试其他账户或服务。

### 新图纸一条命令

```powershell
.\run-drawing.ps1 -Pdf 'F:\你的图纸\新图纸.pdf' -Out 'runs\new-drawing'
```

识别完成后启动 `http://127.0.0.1:8766/`。有疑点也会生成核对页面。单面识别、装配或复核响应在有限重试后仍不符合格式，会保留失败阶段记录并阻断三维通过；网络、布局或 BOM 执行失败则退出并生成 `failure.json`。该脚本在前台运行本地服务，关闭终端或按 Ctrl+C 即停止。脚本保持 ASCII 编码兼容 Windows PowerShell 5.1，并在参数绑定之后解析默认配置路径。

也可分步运行：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild run 'F:\你的图纸\新图纸.pdf' --out runs/new-drawing --config examples/qwen.aliyun.json
.\.venv\Scripts\python.exe -m tower_rebuild serve --out runs/new-drawing --port 8766
```

`run` 返回码 **2** 表示候选结果需要核对，不是程序崩溃。返回码 **1** 是执行失败；只有完成核对且所有约束通过才返回 **0**。

## 使用本机 vLLM

编辑 [`examples/vllm.local.json`](examples/vllm.local.json) 的 `model` 为实际部署的**视觉模型**名称，服务地址默认 `http://127.0.0.1:8000/v1`。服务需要支持图片输入和 JSON Schema 输出；文本模型不能替代。

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild probe --config examples/vllm.local.json
.\run-drawing.ps1 -Pdf 'F:\你的图纸\新图纸.pdf' -Config examples/vllm.local.json
```

本项目是推理客户端，不自动下载权重或安装 GPU 推理服务。vLLM 后端按自己的环境部署，通过本机端口接入。已按官方 `structured_outputs.json` 请求格式实现，不使用旧的 `guided_json`。

## 修正与继续运行

### 只导入图纸，不调用模型

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild prepare 'F:\你的图纸\新图纸.pdf' --out runs/new-drawing
```

### 单独识别

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild extract --out runs/new-drawing --config examples/qwen.aliyun.json
```

识别请求按模型、提示词、Schema、图片内容做哈希缓存。有效响应可重用；仅包裹一个对象的数组、颠倒顺序的证据框角点可以无歧义归一化，随后仍须完整字段校验。多个对象不会随意合并，截断响应不会接受。格式错误最多重试两次，原始响应按尝试次数保存；修复解析器后可重新校验原始缓存，避免重复调用。换图必须换输出目录。

### 修改数据后重新计算，不重复识别

编辑 `runs/new-drawing/observations.json`，再运行：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild build runs/new-drawing/observations.json --out runs/new-drawing --config examples/qwen.aliyun.json
```

若布局或某个面的识别本身错误，可在 `runs/new-drawing/overrides/` 放阶段覆盖文件，然后重新 `extract`：

| 覆盖文件 | 内容 |
|---|---|
| `layout-1.json` | 第 1 页区域列表，格式同 `stages/layout-1.json`；id 不含自动增加的 `p1_` 前缀 |
| `bom-p1_区域id-0.json` | 对应第 0 条 BOM 裁剪的 `{rows, issues}` |
| `face-p1_区域id.json` | 对应面的完整 Face 数据，格式同 `stages/face-*.json` |
| `assembly.json` | 完整 Assembly 数据；不改其他面的观察 |

覆盖文件须有原图依据；程序仍进行语义与数值检查。当前没有浏览器拖节点编辑功能，修正入口是 JSON。

### 完成原图核对

1. 逐一核对 BOM、件号、节点、各结构面、主要尺寸、整体翻折方向。
2. 所有疑点必须先在数据中查清并修正；仅填写核对记录不能越过错误。
3. 把 `review-template.json` 复制为 `review.json`，填写真实核对人、说明；核对完成后设 `accepted: true`，`checks` 填以下六项。

```json
["bom", "callouts", "joints", "faces", "dimensions", "assembly"]
```

4. 保留模板中的 `drawing_sha256`、`result_sha256`，运行：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild build runs/new-drawing/observations.json --out runs/new-drawing --config examples/qwen.aliyun.json --review runs/new-drawing/review.json
```

仅此时可能生成 `reviewed_axes.dxf`。核对状态表示此输入已对照确认且约束通过，**不表示加工模型认证或任意新图纸 100% 自动准确**。

## 数据约定

- 所有图像坐标和 bbox 以**原始整页**归一到 `0..1`，左上原点，x 向右、y 向下；裁剪坐标必须换回原页。
- 世界坐标为 mm，正视基准面为 X/Z、Z 向上。每个面都有局部二维点和未知/有证据的空间姿态。
- `Calibration` 的 `x/y` 是原图水平/竖直投影尺寸；`distance` 是尺寸界点间直线距离。旋转摆放的面可在有依据的等比例假设下使用 `uniform_scale: true` 与 `distance`。
- 非比例示意图、非正投影或标注不一致时，不能靠像素比例恢复精确形状。补充准确节点尺寸，或用人工尺寸化的 `coordinate_units: "mm"` 输入；视觉模型禁止直接生成毫米坐标。
- `Joint.id` 是空间节点实例；`Member.id` 是实物实例；`mark` 只是 BOM 件号。
- `Member.end_offsets_mm = [a,b]`：第一条 occurrence 的 a、b 端各自从节点向杆内缩进，正值内缩、负值外伸；`下料长度 = 节点跨度 - a - b`。没有证据必须 null。
- 每张输入应是一套需要装配的相关图，不应把不同装配、相同件号但不同含义的图纸混在一个输入中。
- 当前拒绝带页面旋转元数据的裁剪识别；需先将旋转固化。多曲线/弯曲杆不能由一条直线准确表示，需另行扩展。

导出完整 JSON Schema：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild schema --out output/drawing.schema.json
```

## 输出目录

```text
runs/<drawing>/
  manifest.json            原始 PDF 路径、哈希、页面尺寸
  pages/                  整页 PNG、文本、矢量证据
  stages/                 布局、BOM、各面识别与原图裁剪
  cache/                  视觉模型响应及内容缓存（含图纸文字，留在本机）
  overrides/              可选阶段人工修正
  observations.json       可编辑的完整识别/装配数据
  review-template.json    本次核对模板，默认未批准
  site/                   本地浏览器展示及下载文件
    model.json            候选/已核对状态 + 实例坐标 + 问题列表
    report.json           每一条约束的误差、阈值及自由度
    faces/                每个面原图及模型投影叠图
    candidate_axes.dxf    带状态说明的候选连接轴线
    reviewed_axes.dxf     仅通过核对流程时存在
    bom.csv
```

网页使用无外部依赖的 Canvas 2D 绘制可旋转的空间杆件，避免 CDN/模块加载及 WebGL 兼容问题；数据请求有超时和明确报错。服务只监听 `127.0.0.1`。

## 验证与局限

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe examples/make_box_example.py
.\.venv\Scripts\python.exe -m tower_rebuild build examples/box.observations.json --out runs/box-demo
.\.venv\Scripts\python.exe -m tower_rebuild serve --out runs/box-demo
```

合成箱体是独立的 13 根杆件、6 个面示例，不是第 9 张图的复原，也没有原 PDF，因此显示未完成原图核对。测试用两个不同尺寸箱体验证通用求解；负面测试验证错件、漏件、虚线、冲突和欠约束不会被接受。`tests/browser_smoke.py` 在禁用 WebGL 的 Chrome 下检查六个面、件号筛选、选择、网络失败提示和本地资源加载（需额外安装 Playwright，并存在脚本中的 Chrome 路径）。

真实图纸的识别准确率必须用人工标注样本独立评估，不能用程序自己生成的投影误差充当准确率。阿里云真实接口现已调用成功。第 10 张图的自动识别未通过装配校验，原始响应、分面观察和问题均保留在 `runs/new-drawing/`；46 项代码测试通过不表示真实图纸识别正确。

本地页面可重复做浏览器检查（需额外安装 Playwright 和 Chrome）：

```powershell
.\.venv\Scripts\python.exe tools/check_local_viewer.py --url http://127.0.0.1:8766/ --out runs/new-drawing
```

该命令逐面检查原图和叠图加载、状态与实例列表、远程资源请求、缺失模型文件的报错，保存 `browser-check.json` 和截图。它只检查网页功能，不判断工程图准确性。

### 与独立标注比较

`evaluate` 支持 BOM/数量/长度/规格、杆件端点拓扑精确率与召回率、刚体对齐后的三维节点误差。只消除坐标原点与旋转差异，不允许缩放，也不会把镜像模型判为相同。

```powershell
.\.venv\Scripts\python.exe examples/make_box_example.py
.\.venv\Scripts\python.exe -m tower_rebuild evaluate runs/box-demo/site/model.json examples/box.reference.json --out runs/box-demo/evaluation.json
```

真实图纸的参考格式可按 `examples/box.reference.json` 填写：`annotator`、`kind`、`source_sha256`、`tolerance_mm`、`bom`、`members`（件号+端点）、`nodes`（独立标注坐标）。若节点 ID 不同，使用 `--node-map mapping.json`，文件为 `{"候选节点ID":"参考节点ID"}` 一一对应表。合成例子的通过只证明算法在该例子可用，不是实际工程图识别准确率。

### 接口依据

- [阿里云 qwen3.7-plus 模型能力](https://help.aliyun.com/zh/model-studio/qwen3-7-plus)：支持图片输入。
- [阿里云 Anthropic Messages](https://help.aliyun.com/zh/model-studio/anthropic-api-messages)：图片 base64 与 `output_config.format` JSON Schema。
- [vLLM 结构化输出](https://docs.vllm.ai/en/latest/features/structured_outputs/) / [多模态输入](https://docs.vllm.ai/en/latest/features/multimodal_inputs/)。
- [SciPy least_squares](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html)。

## 图元之后：逐面拓扑、杆件、连接板、孔位

新增独立 `topology` 阶段，消费 `decompose` 保存的数组。这个阶段不调用云端视觉服务，网页也只在本机运行。

已有分解结果时：

```powershell
.\run-topology.ps1 -Out 'runs\new-drawing-decomposed'
```

新图纸从表格分离开始：

```powershell
.\run-topology.ps1 -Pdf 'F:\图纸\新图纸.pdf' -Out 'runs\another-drawing' -Port 8769
```

浏览器打开脚本打印的地址，例如 `http://127.0.0.1:8768/topology/`。已有同目录服务正在运行时，用 `-NoServe` 只重新生成数据，再刷新网页。PowerShell 启动脚本只含 ASCII，兼容 Windows PowerShell 5.1。

处理顺序：

1. **结构面**：根据原生图元渲染形成的连通区域分组，避免矩形裁剪重叠导致混面。保存剖面标题、前后重叠标识和每面原生图元 ID。单线图作为正视参照；主结构视图的前/后/上/下角色尚需核对。
2. **杆件与杆件**：用 BOM 材质识别杆件；填充和描边重复绘制只在派生几何中去重。建立平行轮廓带、件号观察、候选端点接触和跨视图同件号索引。连续主弦边线可支持合并另一侧被节点打断的轮廓，缺口与覆盖比例保留。普通内部交叉不自动连接，前后件号不合并。
3. **连接板**：件号、规格、下料长度保留 BOM 证据；用封闭轮廓、引线和共享孔位提供候选归属。端点吸附容差 0.65 PDF 点仅用于轮廓闭合。找不到明确轮廓时仍保留板件记录，并标记未确定。
4. **孔位**：排除件号圆圈，合并同心重复圆投影。只有详图中的明确孔径标注及数量一致时记录毫米孔径；其余孔径为 `null`。孔/螺栓符号、前后重叠归属和未闭合圆弧均保留疑点。

所有二维坐标均为**原始 PDF 点**，不是毫米。BOM 下料长度另外保存；同面长度比例诊断用于发现截短轴线和错配，不作为尺寸校准。观察条数不等于实物数量，跨面相同件号不自动合并为同一实物。

输出在 `site/topology/`：

| 文件 | 内容 |
| --- | --- |
| `views.json` | 各视图、原生图元归属、件号/板件/孔位索引 |
| `members.json` | 杆件件号、前后标记、BOM、原识别段、补段候选、长度诊断、证据 |
| `overlaps.json` | 每条观察的重叠段恢复依据、尺度候选、端点区间、剩余缺长和多解状态 |
| `connections.json` | 杆件对、接点候选、共享孔/板、未连接交叉及状态 |
| `plates.json` | 板件 BOM、轮廓候选和杆件/孔位关系 |
| `holes.json` | 圆形投影、原页坐标、显式孔径证据和归属候选 |
| `graph.json` | 关系图节点、边、跨视图件号分组；`physical_connections` 只含明确复核的连接 |
| `geometry.json` | 原生笔画、合并轮廓、平行带和闭合区域，均含来源 ID |
| `unassigned.json` | 未归组图元，保留供复核 |
| `report.json` | 输入哈希、计数、完整性检查和具体疑点 |
| `review-template.json` | 与本次输入绑定的二维对应复核模板 |

网页能逐面切换杆件/连接板/孔位，显示候选叠图、杆件关系图，点击记录查看 BOM 和原生图元证据。所属视图底图是几何分组后的候选图像，勾选“完整原图对照”可查看未经视图遮罩的原始结构图。

### 杆件进入连接板后的重叠段

两侧轮廓同时可见的区间只作为原识别杆身，不能当成下料全长。杆件与板件允许占据相同二维区域，同一孔投影也允许同时归属于杆件与板件候选。

补段阶段保留 `visible_axis` / `visible_polygon`，结合单侧原生边线延续、端部孔排、BOM 下料长度和多个独立杆件提供的比例候选，生成 `axis` / `polygon`。同一圈号内的前后杆件只给比例投一票；已补出的杆件不会反过来参与本轮比例估计。

- 两端孔排必须横向对齐，并在孔距突变处停止追踪，避免把相邻斜杆孔位算进来。
- 只有规则、绕圆分布的短圆弧才补作隐藏孔/螺栓投影；虚线参照线仍不生成杆件，孤立圆弧也不当完整孔。已有杆件端部的成对短线轮廓，在方向、宽度、连接范围及孔位均支持时可作为遮挡边线证据。
- 每端保存原生边线覆盖区间、推断缺口、孔位/板件 ID。BOM 单独不足以决定缺长分配，不默认均分到两端。
- 多个相近可行端点保存为 `endpoint_ranges`；位置区间太宽或与另一根共线杆件冲突时保留待定。
- 可见杆身不足以估计比例时，先追踪不依赖 BOM 目标的原生边线延续；至少三个独立观察形成一致比例，且至少有一个未延长杆件交叉检查，才用于当前面的长度约束。不会先按 BOM 拉长，再拿拉长结果校准比例。
- 隐藏轮廓可完整接续但与圈号对应 BOM 冲突时，状态为 `native_extent_bom_conflict_candidate`，只按原图成对边线恢复二维范围，保留原 BOM 和冲突提示，不据此确认实体或三维尺寸。
- 只追出一部分边线时标为 `partial_overlap_recovered_candidate`，保存 `remaining_length_pt_candidate`，继续提示缺长。
- `full_endpoints_verified` 保持 `false`。比例与 BOM 拟合只是候选约束，不能作为独立尺寸验收；未确认的投影方向、弯折杆和板边仍需核对。

页面将杆身与重叠段统一绘成连续黄色线。可关闭“包含重叠段”对照原识别范围；端点区间、补段依据和 BOM 冲突在记录详情中保留。实际图纸运行记录见 `output/OVERLAP_VALIDATION.md`。

### 固定已经核对的二维对应

复制 `review-template.json`，保留原图、分解输入、派生几何三个哈希，防止算法调整后旧记录指向了不同轮廓。`parts` 中添加实际记录的 `id`、同面 `geometry_id`（平行带或闭合区域）及 `reason`。`views` 可填写实际视图 ID、`face_role`（front/rear/top/bottom/section/detail/schematic）和依据。

```json
{
  "id": "p1_view_04_member_001",
  "geometry_id": "p1_view_04_band_0040",
  "reason": "对照原图件号引线和两端节点核对"
}
```

以上 ID 只是格式示例，必须使用当前输出中的实际 ID。运行：

```powershell
.\run-topology.ps1 -Out 'runs\new-drawing-decomposed' -Review 'runs\new-drawing-decomposed\topology-review.json' -NoServe
```

再检查重建后的关系，才在模板 `connections` 中填写连接 `id`、对应的两个 `member_ids`、布尔值 `connected` 和 `reason`。输入哈希不符、跨面轮廓、杆件尚未核对或连接指向的杆件发生变化时，会拒绝确认。该复核记录仅适用于二维对应；不能由此声称三维装配或加工孔位已验收。

自动化退出码：`0` 为对应命令通过，`2` 表示数据已生成但有待核对项，`1` 为运行错误。拓扑阶段目前通常返回 `2`，启动脚本会继续提供本地页面。

```powershell
.\.venv\Scripts\python.exe tools/check_topology_viewer.py --out runs/new-drawing-decomposed
```

检查每个面的三个层级、原图/分组图切换、证据选择、纯本地请求和数据缺失提示。浏览器检查证明功能可用，不证明工程识别正确。当前阶段不会生成已确认的三维模型；详见 `output/TOPOLOGY_VALIDATION.md`。

## 尺寸初审：示意图 → 分段跨度 → 端部尺寸 → BOM

`run-topology.ps1` 现在在拓扑生成后自动运行尺寸初审。已有拓扑结果时可单独重算；本地服务已在运行时使用 `-NoServe`：

```powershell
.\run-audit.ps1 -Out 'runs\new-drawing-decomposed' -NoServe
```

打开 `http://127.0.0.1:8768/audit/`，也可从拓扑页面的“尺寸初审”进入。没有服务时省略 `-NoServe`。新图纸仍使用 `run-topology.ps1 -Pdf ... -Out ...`，一次完成表格分离、图元拓扑和尺寸初审；另选空闲端口可同时保留多个运行目录。

独立 Python 命令：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild audit --out runs/new-drawing-decomposed
```

### 计算规则与边界

1. **先识别完整尺寸示意图，再分面。** 从整页原生实线中查找带数字的独立闭合 3～8 边图形，避免分区边界截断示意图。圈号数字排除；文字方向、距离和一对一分配共同匹配边长。保存原生文本与线段 ID、原图裁剪。原生虚线不进入杆件或尺寸图边；误判为虚线模式的实线只在尺寸图检测中恢复。
2. **三角形不默认直角。** 三边先检查三角形不等式，再用余弦定理计算角度。四边形及以上检查必要边长条件；仅有边长通常不足以唯一决定形状。
3. **方向检查与候选对照。** 尺寸图唯一对应到结构面候选后，以原始杆件轮廓方向进行向量闭合。逐个反算单值改动，连同“保留原值”一起对照多边形边长条件、相邻尺寸反算和已具备基准的 BOM 换算。每个改法均重算受影响的输入，只有通过全部可用检查的唯一候选才给出建议；没有相邻/BOM 依据时明确标为仅方向支持。该结论依赖等比例投影、轮廓和尺寸边对应正确；取整误差界不包含未知绘图变形。
4. **相邻三角形交叉核对。** 共边、直杆两侧和节点对应均满足时，用余弦定理反算另一段；对标注取整使用保守区间运算，不能唯一确定正根时不推荐。不会拿小示意图画出的角度当精确角度。
5. **拼完整跨度后再比下料长度。** 共享标注边和共线方向能支持拼接相邻图的端部段；重复观察不累加。两端正负尺寸的方向、位置及唯一性支持对应时，计算“分段跨度之和 + 两端伸长/退距”。没有完整基准时保留待定，不能把跨度与 BOM 直接判成冲突，也不能用 BOM 差值反推所谓标准退距。
6. **杆旁标注另查 BOM。** 对照角钢截面与长度，保留圈号多解和轮廓方向不符。规格、长度都匹配且该件号尚未观察到时可提出圈号候选；这不等同于实物数量确认，也不包含钢材牌号验收。

审核只写报告，不修改原图、BOM、杆件件号或三维模型。输出始终为 `needs_review`，退出码 `2` 表示完成但需要复核，启动脚本按成功处理。输入 PDF、分解数据和拓扑来源哈希必须一致，否则拒绝计算。当前闭合图形检测针对原生矢量 PDF；扫描图、断线和混杂连线不能据此声称已全覆盖。

输出：

| 路径 | 内容 |
| --- | --- |
| `site/audit/report.json` | 尺寸图、向量闭合、余弦推导、分段与端部尺寸、BOM 对照、疑点和输入哈希 |
| `site/audit/*.png` | 对应原 PDF 的尺寸图、圈号和杆件端部裁剪 |
| `dimension-audit.md` | 可读的初审结论与换算式 |

第 10 页目前的三个吻合换算例：`1375−45−45=1285`、`1721+227−167+42=1823`、`1017+127+258−77−208=1117` mm。`987→994` 和斜杆圈号 `465→463` 是有条件的优先核对建议。其余待定项和第 9 页回归结果见 `output/DIMENSION_AUDIT_VALIDATION.md`。

页面及 Markdown 报告提供“不同改法的计算对照”：例如 `987→994` 后方向残差为 0.492 mm，相邻尺寸区间也支持；改成 `1375→1370` 会使竖杆计算长度从 1285 变为 1280 mm，与 BOM 相差 5 mm，且方向闭合仍不满足。相邻三角形与原三角形共享部分输入，不能把这种交叉验算当作完全独立的实测，更不能据此给出虚构的准确率。候选圈号还列出 BOM 数量、同面其他观察，以及原圈号在其他位置已匹配的数据。

页面和报告另有“逐面审核进度”，列出各面的尺寸图数、闭合吻合/冲突、对应待定、BOM 换算和缺失基准。点击结构面名称可筛选该面的详细结果。未检出尺寸图的面明确显示尚无尺寸验算结论；缺少端部依据的记录分起点/终点列出未找到标注、多个标注候选、重复引用同一标注或相邻端段多解。

结构面对应增加原始杆段位置检查：候选边必须与原始可见杆身或有原生实线证据的延续区间有实际重叠，远处短杆的无限延长线不单独构成对应依据。仅在方向相近的候选中排除缺少位置支持者；不因为某个方向更差的候选有重叠就选它。原生区间之间的空隙不会自动填满，也不使用 BOM 拟合长度或隐藏线补段自证。无支持或多个支持仍保留待定，位置吻合不等于端点/下料长度已验收。

候选尺寸比较包含原本与 BOM 不吻合但已具备换算依据的记录，也重算受改值影响的相邻三角形公式。不会只挑原本吻合的数据作为支持。

```powershell
.\.venv\Scripts\python.exe tools/check_dimension_audit_viewer.py --out runs/new-drawing-decomposed --url http://127.0.0.1:8768/audit/
```

该检查覆盖所有结构面、原图加载、公式显示、移动窗口、页面往返和缺失/损坏数据提示。图片加载失败或超过 15 秒时显示提示，可单独重试，计算数据继续保留。省略 `--url` 时，检查程序会临时启用只绑定本机的静态服务，并在检查后关闭。

## 建模前检查与逐面三维预览

`run-topology.ps1` 完成拓扑和尺寸初审后，会生成逐面识别检查与基础三维预览。已有拓扑结果时使用：

```powershell
.\run-preview.ps1 -Out 'runs\new-drawing-decomposed'
```

如果本地服务已在运行，添加 `-NoServe` 后刷新页面。该脚本重算尺寸审核及预览，接受退出码 `2`（有待复核项）；运行错误仍会停止。新图纸使用前面的 `run-topology.ps1 -Pdf ... -Out ...` 命令即可完成全部阶段。

打开 `http://127.0.0.1:8768/topology/`，选择结构面，点击“显示三维模型”。模型在图纸上层；拖动旋转、滚轮缩放、右键平移。“图纸对齐”恢复正投影叠图，“立体角度”切回倾斜观察。可分别隐藏角钢、连接板、孔位标记和杆件件号，或调整实体透明度。件号随视角保持可读，点击件号或实体可高亮对应杆件并查看 BOM 与模型依据。

### 建模依据

- `Q345L…` / `Q420L…` 生成 L 形角钢截面，肢宽和厚度来自 BOM；`Q345-…` 等板材规格提供板厚和板坯宽度。未知规格保持待定。
- 杆件轴线沿用当前二维拓扑及重叠补段，仍以黄色显示。尺寸示意图优先提供本面比例；回退依据会记录。相互冲突且无多数支持的比例保持待定。BOM 下料长度与模型投影长度分开保存，不自动把二维投影拉成 BOM 全长。
- 板件使用原生闭合多边形。允许小于 0.7 PDF 点的 T 形交点缝隙吸附，并保存原始笔画证据；板坯尺寸只用于排除不合适轮廓。没有板形依据时保留缺项，不用 BOM 长宽生成替代矩形。
- 有明确标注、数量及引线支持的孔径才记录毫米值；明确孔径且孔圆完整落在板内时切穿板实体。其余圆形以孔/螺栓投影标记显示。角钢上的孔当前为位置标记，尚未切穿角钢。
- 剖面中的长杆横截面不会沿截面短线误生成纵向杆件。单线图尚未对应到逐根 BOM 杆件时，仅作正视基准显示。

这是**逐个结构面的候选实体预览**。各面的装配变换、角钢朝向、前后层距、完整杆端和未确定板形仍需复核。三维预览可帮助检查这些缺项，不能据此称为已确认的横担箱体或加工模型。图纸、BOM 和已保存的原始拓扑不被自动改写。

| 输出 | 内容 |
| --- | --- |
| `site/topology/model-preview.json` | 每面截面、轴线、板形、孔位、比例依据和显示假设 |
| `site/topology/final-check.json` | 杆件/板件缺失件号、杆端与长度疑点、板形和孔位覆盖情况 |
| `model-preflight.md` | 逐面检查表和可读的疑点列表 |

“建模前检查”会显示当前面的覆盖情况。观察数不等于实物数量；例如同一件号在不同面、前后重叠或详图中可能重复出现。未归组圆弧不是已确认漏孔数。

三维库随源码本地打包，不依赖 CDN。数据读取超时、损坏或 WebGL 不可用时显示明确错误，保留二维图纸并提供重试入口。浏览器实测命令：

```powershell
.\.venv\Scripts\python.exe tools/check_model_preview.py --out runs/new-drawing-decomposed --url http://127.0.0.1:8768/topology/
```

该检查验证旋转、缩放、与图纸对齐、实体截面和板孔、层级开关及失败提示，不作为工程准确率。两张真实图纸的结果和局限见 `output/MODEL_PREVIEW_VALIDATION.md`。

## 三维尺寸与原初审再比对

完整上传流程在三维预览后自动执行 `model_audit`，`preview` 命令也会生成此报告。已有模型可单独运行，保留原模型和初审数值：

```powershell
.\run-model-audit.ps1 -Out 'runs\new-drawing-decomposed' -NoServe
# 可调整差异提示阈值（不是加工公差）
.\.venv\Scripts\python.exe -m tower_rebuild model-audit --out runs/new-drawing-decomposed --tolerance-mm 3
```

本地页面：`http://127.0.0.1:8768/model-audit/`；也可从“逐面结构图”或“尺寸初审”点击“三维尺寸复核”。输出为 `site/model-audit/report.json` 和 `model-dimension-audit.md`。可按结构面、件号、项目、差异或待定筛选。

- 杆长由模型挤出端点重新计算，按观察记录 ID 对应 BOM、杆旁标注和初审换算；同件号的不同观察不混用。
- 初审已完成分段、端部换算时，才将换算下料长与模型全长比对。缺少换算依据的节点跨度保持待定。
- 三角形或多边形的跨度由模型参考轴线重新求交，不复制初审的交点坐标。延长线交点不代表实体已经连接；多解或未对应的面不强行计算。
- 板形包络与 BOM 板坯长宽仅作参考。截面、板厚和明确孔径取自输入，单独标记“输入复用”。孔中心距离可量测，但尚未建立原标注尺寸链的孔距保持待定。
- 明确记录比例标定来源、被复用的尺寸和补段状态。默认差异阈值为 3 mm 加初审舍入界限；“阈值内相容”不等于独立验证或工程验收。
- 不自动改写原 PDF、BOM、初审建议或模型。源文件或模型输入发生变化会要求重算；网页检测模型/初审版本与报告不一致时显示过期提示。

验证报告中的数值是否对应网页实际网格：

```powershell
.\.venv\Scripts\python.exe tools/check_model_dimension_viewer.py --out runs/new-drawing-decomposed --url http://127.0.0.1:8768/model-audit/
```

该检查读取 Three.js 网格顶点测量杆长、截面肢宽、板形包络和厚度，以及孔中心位置，与报告核对；另检查筛选、移动窗口、过期报告与本地资源加载。结果记录在运行目录的 `model-dimension-browser-check.json`，不是图纸准确率。
