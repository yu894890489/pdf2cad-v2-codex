# 新图纸完整处理流程

## 本地上传

```powershell
.\start-local.ps1 -Root runs/studio-complete -Port 8774 -Config examples/workflow.native.json
```

打开启动端口对应的本机网址，上传 PDF。当前运行端口为 `http://127.0.0.1:8774/`。可一次选多张，按队列处理，每张创建独立目录。完成后查看“图纸与三维”和“全流程终检”。模型显示在原图上层，可旋转、缩放、对齐及切换杆件、板件、孔位。

完整流程现在默认执行 **多面装配 → 缺失面反算 → 整面补长/截短及放置 → 杆长/BOM 与接缝复核**。无需逐面点击按钮。上传处理完成后自动提供可编辑的装配项目和“自动装配与补全报告”；命令行 `process` / `run-pipeline.ps1` 同样输出 `site/assembly/index.html` 与 `report.json`，可通过本地 HTTP 服务打开查看。逐面原始模型、BOM 和 PDF 保留，修正只写入装配层。

自动阶段先搜索有证据的四面环，未找到时联合多个公共杆件剖面求解收口。仅当最佳候选给出唯一收口方向时自动采用；有歧义时列出原因。随后对尚未定位、具有两个已连接邻面依据的面反算补全。几何不共面或件号/杆端错误导致无法定位时，所有面仍进入窗口，报告状态为 `blocked` 或 `partial_candidate`，不会把执行完成标为尺寸合格。BOM 数量大于 1 的同号观察不会自动视为同一根实物。

杆件被连接板遮挡时，比例校核同时比较可见段和原生线条接续。只有更多独立杆件支持、且包含未补长杆件的校核时，才替换原来的可见段比例。件号误落到邻近细条时，使用独立比例、BOM 角钢肢宽、方向与引线重新检查候选；多个候选无法区分时保留疑点。原候选、原始轴线和关联依据都保存。

剖面联合求解先使用铰接姿态。若误差集中在闭环的最后一条接缝，程序以正视面固定，对全部公共杆件端点做有界的刚体联合拟合，并检查约束是否充分。此步骤只移动、旋转结构面，不修改其杆长；仍使用原有候选拒绝阈值，验收仍为 3 mm / 1°。`taper_fit.refinement` 记录前后残差，`taper_fit.seams` 逐条记录件号、结构面和误差；失败时报告具体不满足的接缝。

可在流程 JSON 中配置：

```json
"assembly": {
  "enabled": true,
  "tip_direction": "auto",
  "complete_missing_faces": true,
  "max_corrections": 12
}
```

`tip_direction` 可按已知正视图指定为 `negative_x` 或 `positive_x`；`max_corrections` 限制每次尝试反算的面数。`complete_missing_faces: false` 仅装配已有几何，`enabled: false` 跳过整个阶段。默认全本地计算，无需 vLLM。可编辑项目提供“自动装配并反算补全”按钮重新执行；已有手动连接作为依据保留。源图/比例变化会使旧修正失效，手动布局不会被后台自动重排。

已有完整解析结果可单独重算此阶段，无需重新提取图元：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild assemble --out runs\new-job
# 有明确尖端方向时可附加 --tip-direction negative_x
```

输出到该运行的 `site/assembly/` 并同步终检。退出码 2 表示仍为待复核候选。此命令更新保存结果页，不覆盖工作台中已手动编辑的装配项目；工作台项目可用“自动装配并反算补全”按钮继续。

整个流程在本机执行。默认不需要 GPU、vLLM 或云端接口。已有服务运行时直接使用页面；不要重复占用同一端口。

## 多图纸组成横担层

同一批上传会同时创建装配项目；原图解析任务仍各自独立，避免不同 PDF 的相同件号互相覆盖。也可勾选历史任务联合装配。项目支持最多 30 张不同 PDF，并按文件内容去重。使用“补充图纸”可持续加入图纸；已有手动布局保留，完成解析后新增面自动出现。

多图自动装配先按来源图纸分别选择正视面、求解连接与反算补全，再汇总到同一窗口。某张图无法装配或没有可用模型，不会阻止其他图的求解。每组保留各自的正视面和收口约束；“自动处理结果”可展开具体图纸并点击“查看此图装配”。只有明确的人工跨图连接会合并图纸组，不会仅凭不同 PDF 中同名件号自动合并。单张上传仍使用原来的单图求解流程。

原图解析入口保留在工作台的项目卡片，以及装配页顶部“图纸解析与审核”工具栏中：图纸与三维、全流程终检、BOM 与图元、尺寸初审、三维尺寸复核、检查报告、输入检查、BOM 数组、模型数据、原 PDF、运行日志和重新处理。多图项目通过“当前图纸”切换，点击结构面会同步选择其来源图纸。未生成的结果显示为不可用并提供原因；重新处理创建新任务，保留原模型和装配布局。保存报告页提供同样的结果导航，在工作台中执行重新处理。

程序从已定比例的杆件边界寻找共同件号，并同时核对规格、BOM 长度和投影边长。只因长度接近的边列为人工候选。自动以正视面竖直、邻面 ±90° 搜索四面闭合环；前/后件分组约束平行；额外端面需至少两条公共边同时刚性对齐。单铰链不足以唯一确定的面保留在窗口中供手工放置。

手工操作：装配项目默认启用“拖动模型”，按住结构面即可移动；默认整个已连接组一起平移，取消“移动整个连接组”可单独移动一面。移动方向可选屏幕平面、地面 XY 或仅 X/Y/Z，右侧位置输入框随拖动更新。空白处拖动旋转视角、右键平移视角、滚轮缩放；松手自动保存，Esc 取消本次拖动，支持撤销、重做和刷新恢复。模型的杆长、板形和孔位不会因平移而改变；单独移动已连接的一面后，会重新检查连接间隙。沿轴拖动时若轴与视线接近平行，先旋转视角或改为屏幕平面。保存报告页为查看模式，编辑请进入工作台的装配项目。

也可输入 XYZ 位置和三个旋转角；切换“选择连接边”依次点 A、B，输入带符号角度，再连接。A 固定，B 及其已连接组一起移动；可以切换端点方向、解除连接、设置正视参考、选择参与闭合检查的面。未定比例的原线条会放大展示，必须补足该面 mm/pt 后才可按尺寸连接。

闭合检查以 3 mm 端点公差、1° 连接角误差核对，不拉伸模型：四面环与包含所有端面的完整多面体分别报告；重叠、穿插、开放边和前后平行冲突单独列出。边界目前为外包多边形，物理接头、板厚偏置、角钢肢朝向和实物数量尚未确认，结果保持 `engineering_verified: false`。

共享杆件即使位于凸包内部，也保留为定位候选；只有外边界候选参与自动闭合搜索。若某一横向剖面通过两条杆件分别与前后面相连，程序会检查两条轴线是否平行：平行平面与同一横向平面的交线应平行。非零夹角只作为与默认平行规则的冲突证据，不直接当成真实三维二面角，也不擅自改变图纸尺寸。杆长/BOM 差异及多件共用轴线保持“待核对”，因为可能来自投影、杆端缺失或前后重叠表达。

“预览连接”只临时显示候选，不保存布局；“取消预览”恢复已保存状态。连接、移动等操作支持最近 30 步撤销/重做，服务重启后保留。源图、模型或比例变化会清空旧历史。可点击“聚焦连接组”查看已连接的局部模型。

装配使用本地 NumPy/SciPy/Shapely 和 Three.js，不需要 vLLM。自动图像识别不确定时，先修正逐面结果或手动装配；模型服务不能代替尺寸和闭合检查。

### 装配接口（可复用）

上传接口照常返回任务 ID。携带 `/api/session` 返回的 `X-Upload-Token`：

- `POST /api/assemblies`，JSON 为 `{"job_ids":["任务ID1","任务ID2"],"title":"横担层"}`，返回项目 ID 与本地页面 URL。
- `GET /api/assemblies/<项目ID>`：获取来源、各面模型、边、候选配对、布局与检查结果。
- `POST /api/assemblies/<项目ID>`：每次带当前 `revision`，并指定 `action`。支持 `add_jobs`、`auto`、`arrange`、`front`、`active`、`scale`、`pose`、`connect`、`preview_connect`、`unlink`、`import`、`undo`、`redo`。
- `automatic_pipeline`：保留已有连接作为依据，继续自动装配、反算补全和尺寸复核；返回 `automatic_pipeline` 报告，修改布局可撤销。原始输入没有变化时，刷新页面不会重复修正杆件。
- `pose` 携带 `face_id`、`pose: {position:[x,y,z], quaternion:[x,y,z,w]}`、`move_connected`。
- `connect` 携带 `a`、`b`（完整边 ID）、`angle_deg`（−180 至 180）、`flip`（是否反向对齐端点）。
- `preview_connect` 使用同样参数，返回候选 `scene` 和 `assessment`，不改动布局或修订；`undo`、`redo` 只需 `action` 和当前 `revision`。响应 `history` 给出可撤销/重做的步数，`review` 保存装配诊断。
- `import` 需要同一 `input_digest` 与完整 `scene`；旧修订返回 HTTP 409，不覆盖其他窗口的新布局。
- `review_missing_face` 携带 `face_id`：先排除待查面，使用两个已连接邻面的唯一公共件号反算四角轮廓，再回查原矢量。约束有效时直接修正整面杆件并放置，更新 `scene.face_corrections`、位置和公共边连接；单次操作可撤销。有效面数据 `faces` 包含修正几何，`geometry_revision` 变化时前端重建三维实体。修正杆件的 `correction` 字段记录原轴、修正轴、原长、修正长、BOM 差、补长/截短分段与依据。公共边以邻面端点为约束，内部杆件按轮廓映射，满足边界交点及 BOM 候选范围的截短斜杆尝试补至边杆。
- 回查响应 `missing_face_review.correction.status` 为 `applied_candidate` 或 `blocked`。超过 3 mm 非共面误差、轮廓退化、缺少实体杆件或连接冲突时不应用几何修改，响应保留原因。原 PDF/BOM/识别模型不改写；刷新、重启及重复反算都从原始几何重建修正层。比例或上游输入变动使旧修正失效。修正后的贴合误差不是独立验收依据，`engineering_verified` 仍为 `false`。
- 使用当前 `missing_face_review.artifact_key` 作为 `?basis=` 可读取装配目录下的 `missing-face-source.svg`、`missing-face-corrected.svg` 和 `missing-face-review.json`；过期依据返回 HTTP 409。

项目持久化目录为 `<工作台根目录>/assemblies/<项目ID>/`。`project.json` 保存布局，`assembly.json` 保存完整面和检查依据。PDF 或上游模型变动会重新校验来源摘要；不匹配的数据不载入装配。

## 一条命令处理

```powershell
.\run-pipeline.ps1 -Pdf 'F:\图纸\新图纸.pdf' -Out 'runs\new-drawing-001' -Config examples/workflow.native.json -NoServe
```

输出目录须为新的或空目录。程序不会将上一次图纸的模型混入新结果。

Python 调用：

```python
from tower_rebuild.workflow import process_drawing

result = process_drawing(
    'drawing.pdf', 'runs/new-job',
    config='examples/workflow.native.json',
)
print(result['status'], result.get('final_check_counts'), result.get('error'))
```

## 各阶段的代码与产物

| 阶段 | 主要代码 | 结果（相对运行目录） |
| --- | --- | --- |
| 输入检查 | `workflow.py` | 原 PDF、逐页覆盖情况、配置快照 |
| 无文字层矢量图 OCR | `ocr_input.py` | `ocr/analysis.pdf`、`ocr/report.json`、`site/ocr/` 原图/文字/结构对照 |
| BOM 与图元解析 | `native_bom.py`、`decomposition.py`、`primitives.py` | `site/tables/`、`site/structure/` |
| 图元归面与拓扑 | `topology_views.py`、`topology.py`、`topology_overlap.py` | `site/topology/`：杆件、板形、孔位、连接、证据及补段 |
| 图纸尺寸初审 | `dimension_audit.py` | `site/audit/`、`dimension-audit.md` |
| 基本三维模型 | `model_preview.py`、`topology_web/model-layer.js` | `site/topology/model-preview.json`、可旋转网页 |
| 图纸与模型测量比对 | `model_dimension_audit.py` | `site/model-audit/`、`model-dimension-audit.md` |
| 可选视觉复核 | `vision_review.py` | `site/vision-review/report.json`，关闭时记录 `disabled` |
| 多面装配与反算补全 | `assembly_pipeline.py` | `site/assembly/`：三维页面、布局、尺寸差异及补全证据 |
| 全流程终检 | `final_audit.py` | `site/final/`、`final-check.md` |

状态和每阶段耗时写入 `workflow.json`；完整配置写入 `workflow-config.json`。网页和命令行共用一个入口。上传任务的重试使用该任务保存的配置，不受之后更改的服务默认配置影响。

### 识别规则

- 先读取 BOM；每行保存原页坐标、规格、数量和长度。
- `Q345L…` 等角钢规格生成 L 形截面；`Q345-…` 等板材使用多边形及板厚。其余规格按分类保留，不冒充角钢。
- 圈内件号可含字母，如 `509A/509B`；前后位置和同号不同观察保留。单圈、双圈都查 BOM。
- 表格与图框粘连时，通过原生文字中的“编号/规格/长度/数量”表头组合重新定位，不使用固定右侧裁剪坐标。
- 虚线、尺寸线与引线不单独生成杆件。连接板遮挡补段保存证据；交叉不直接等于接点。
- 各面独立识别后记录跨面同号候选。仅同号不能证明是同一根实物。
- 初审用尺寸图、原生方向、端部标注和 BOM 计算；模型全长与节点跨度分开比较。

## 哪些新图纸可以直接处理

当前主流程面向结构形式相近、带 BOM 的输电塔角钢图纸，输入为 CAD 导出的矢量 PDF：

1. 结构线条为原生矢量，文字可提取或能经 OCR 恢复；未加密，页面旋转已经固化。
2. BOM 有可读取的件号、规格、长度和数量；件号在当前文件内含义唯一。
3. 结构视图可分开，件号和杆件、板件的位置能够关联。
4. 有足够的尺寸与端部基准；不同视图和真实实物的对应有依据。

可自动完成程序处理和生成候选模型，不等于任意图纸都已正确复原。扫描图、模糊文字、缺 BOM、缺比例、缺视图、独立图纸重复编号会影响建模，程序保留证据并明确报告。纯扫描件尚未接通经过精度验证的 OCR/矢量化适配器；启用视觉复核也不会把扫描图片称为已提取的原生图元。

CAD 导出时将文字转成线条的矢量 PDF，会自动进入本地 OCR。先安装可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ocr]"
```

OCR 使用本地 RapidOCR / ONNX Runtime，不需要 vLLM 或云端接口。默认 `ocr.mode="auto"`，仅处理有矢量且无可提取文字的页面；`"disabled"` 可关闭。参数在流程配置的 `ocr` 字段中调整，例如 `render_scale: 3`、`min_confidence: 0.75`、`review_confidence: 0.93`。

程序按重叠图块恢复文字，利用原生表格边界逐格识别，保留每个原始读数、置信度、坐标和不同识别结果。表格 OCR 使用省略表格线的临时渲染，避免边框盖住件号末尾数字；原图不改动。分析副本仅增加不可见文字层，逐页检查原始路径几何与可见图面一致。文字区域内的原始笔画被标记并从结构推理中排除，JSON 中仍保留全部命令。跨越文字框的线、圆孔和与长结构线相接的短线保留。

上传结果中的“OCR 文字与笔画”提供原图、结构矢量、文字笔画切换以及逐条文字检查。低置信度和多次识别不一致的结果单列；BOM 缺项或冲突仍会阻止建模，不能把 OCR 置信度当作工程准确率。带文字层分析副本和识别报告通过独立哈希绑定，修改后必须重算下游阶段。纯扫描件的结构矢量化仍未接通。

## 终检如何定义结果

终检汇总页面覆盖、BOM 完整性、图元保存、件号覆盖、杆端、连接关系、尺寸初审、模型尺寸、板形、孔位、物理数量和空间装配。

- `model_generated=true`：已生成候选实体。
- `needs_review`：有模型和报告，但存在差异或待确定依据。
- `no_model`：证据不足，没有生成实体。
- `unsupported`：输入条件不足，已保存原图和可解析证据。
- `failed`：程序运行异常，查看 `failure.json` 和日志。

目前跨面实物去重、数量和所有空间装配关系尚未完整自动确认，`engineering_accuracy_verified=false`。某项尺寸吻合不批准整个模型；BOM/比例复用、未知板形、孔距、缺段均单列。

现有结果重算终检：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild final-check --out runs/new-drawing-001
```

若修改了原生识别或模型，先重算相应下游阶段。输入哈希不一致时禁止沿用过期初审或比对。`preview` 会依次生成三维预览、尺寸比对和终检；可选视觉报告若已过期会在终检列出。

## 什么时候需要视觉模型、vLLM

**vLLM 不是必需依赖。** 它用于运行一个支持图像输入的视觉语言模型（VLM）。目前可选分支逐面读取原图裁剪，将圈号、轮廓、孔位和尺寸疑点与原生结果交叉检查，输出带原页证据的建议；不能自动修改几何或批准模型。

### 本地 vLLM

1. 在 Linux/WSL2 部署兼容的视觉模型；Windows 程序通过本机回环地址访问服务。
2. 编辑 `examples/workflow.vllm.json` 中的服务地址与实际模型名。服务需接受图像输入、结构化 JSON 输出；只有文本能力的接口不能使用。
3. 先检查真实图像输入，再启用上传处理：

```powershell
.\.venv\Scripts\python.exe -m tower_rebuild doctor --config examples/workflow.vllm.json --probe-vision --out output/vllm-check.json
.\start-local.ps1 -Root runs/studio-vllm -Port 8774 -Config examples/workflow.vllm.json
```

`doctor` 发送一张程序生成的随机四位数字图片，不发送用户图纸。验证图像输入、读取内容与 JSON 输出；通过不代表已验证工程图纸准确率。

本机检测到 RTX 2060、6 GB 显存。现有原生流程不需要这块 GPU；视觉模型能否部署取决于权重、量化、图像分辨率和上下文，需要实测，代码不预设大型模型能在该显存下运行。

官方接口和平台说明：[vLLM 多模态输入](https://docs.vllm.ai/en/latest/features/multimodal_inputs/)、[GPU / Windows 与 WSL 安装说明](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)。

### 已有 Qwen 兼容接口

`examples/workflow.qwen.json` 沿用此前提供的 Anthropic 兼容地址与模型名。该服务是否支持图像输入需先运行 `doctor --probe-vision` 验证；代码没有假定 `qwen3.7-plus` 一定具有视觉能力。

密钥仅由本机环境变量 `QWEN_API_KEY` 读取。此配置显式启用了外部服务，使用时会把结构面裁剪和对应 BOM/观察数据发送至该服务；网页仍在本机运行。默认 `workflow.native.json` 不发送这些数据。

视觉服务不可用时，保存明确错误并停止连续请求，原生模型和检查结果继续保留。视觉输出中的未知记录 ID、错误页码或裁剪范围外的证据不会成为已复核记录。

## 环境与交付

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m tower_rebuild doctor --out output/runtime-check.json
```

已验证依赖版本见 `requirements-tested.txt`。源码包为 `output/tower-rebuild-source.zip`，不含 PDF、密钥、任务缓存或模型权重。完整真实图纸回归记录见 `output/COMPLETE_WORKFLOW_VALIDATION.md`。
