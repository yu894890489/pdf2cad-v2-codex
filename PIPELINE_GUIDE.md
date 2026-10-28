# 新图纸完整处理流程

## 本地上传

```powershell
.\start-local.ps1 -Root runs/studio-complete -Port 8774 -Config examples/workflow.native.json
```

打开启动端口对应的本机网址，上传 PDF。当前运行端口为 `http://127.0.0.1:8774/`。可一次选多张，按队列处理，每张创建独立目录。完成后查看“图纸与三维”和“全流程终检”。模型显示在原图上层，可旋转、缩放、对齐及切换杆件、板件、孔位。

整个流程在本机执行。默认不需要 GPU、vLLM 或云端接口。已有服务运行时直接使用页面；不要重复占用同一端口。

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
