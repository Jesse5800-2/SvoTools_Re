# SvoTools_Re

> 基于Python实现的 `.svo` 格式解析工具
----
## 介绍
用于解析、解包、校验和重打包 SBGA `stevia` 命名空间下的 `.svo` 资源文件。目前支持：

- 读取 `AVTS` 目录表
- 按块提取内嵌 DDS（**DirectDraw Surface**） 纹理
- 与原 DDS 目录做 SHA256 对照
- 依据 `manifest.json` 原样重拼 `.svo`（1:1 roundtrip）

目前仅依赖 Python 标准库，无外部包。

----



#### 用法

无需安装。确保本地拥有 Python 3.8+ 运行环境，并从源码直接下载即可。

### 1. 查看目录表

```bash
python svo_tool.py info <file.svo>
```

输出每个块的 `kind`、`seq`、`size`、`offset`，以及 DDS 的尺寸、格式和 mip 层数。

### 2. 解包 DDS

```bash
# 单文件
python svo_tool.py extract <file.svo>

# 目录递归
python svo_tool.py extract <dir> -r

# 指定输出目录，并写出 manifest.json
python svo_tool.py extract <file.svo> -o out/ -m
```

默认输出到 `<源>/svo_extracted/`。文件名格式为 `{块号:03d}_{清理后的名字}.dds`。

### 3. 与官方 DDS 对照

```bash
python svo_tool.py verify <file.svo> <官方 dds 目录>
```

逐块计算 SHA256，输出一致 / 不一致 / 缺失统计。

### 4. 重打包

```bash
# 依据 extract -m 写出的 manifest.json 原样重拼
python svo_tool.py pack <抽取目录>

# 同时做 1:1 字节级 roundtrip 校验
python svo_tool.py pack <抽取目录> --check
```

当前 `pack` 为**原样拼接**：

- 保留原始 header / 目录 / YABX / 块间填充 / 尾部字节
- 适合做 roundtrip 验证
- 若替换的 DDS 尺寸发生变化，目录表不会自动重建，需等待后续 `pack --rebuild`。

---

## 输出示例

```text
====================================================================
example.svo   version=5   块数=5
blk kind  seq       size     offset          dim    fmt mip  name
  0    0    0    0x4180    0x1480            -      -   -  '__HmfToSvo__example.svo'
  1    1    1    0x3f80    0x5600        64x64  A8R8G8B8  1  '__HmfToSvo__example.dds'
  2    1    2     0x180    0x9580         8x64   DXT1    1  '__HmfToSvo__example_eff.dds'
  3    0    3     0x230    0x9700            -      -   -  '__HmfToSvo__example__PNCT_0000.VBO'
  4    0    4      0x3c    0x9980            -      -   -  '__HmfToSvo__example__PNCT.IBO'
```

解包后的典型输出：

```
svo_extracted/
  000_example.dds
  001_example_eff.dds
  manifest.json     （使用 -m 时生成）
```


## ⚠已知的限制

- `pack` 目前只能做原样拼接，替换 DDS 后若尺寸变化，目录表不会自动更新
- 非 DDS 块（YABX / VBO / IBO）仅保留原始字节，未做语义解析
- `manifest.json` 中的 `pad` 与 `tail_raw` 用于保证 1:1 还原，不建议手工修改
- 仅支持小端 `AVTS` / `YABX`

## 📄相关文档

- `svo_format.md` — 本仓库内的 SVO 格式规格说明，来自**GekiChuMaiLocalizedDocument**，未作修改，仅作参考引用。内容如下：
  - AVTS chunk 目录表
  - YABX 容器与类定义
  - stevia 对象模型（19 类字段表）
  - 实例数据语法（class_tag / 字段序 / TLV / ref-list）
  - 几何数据块（VBO / IBO）
  - DDS 贴图段
  - 字体纹理 SVO 生成器说明

### 外部参考

- **SvoTool** — MikiraSora
  https://dev.s-ul.net/MikiraSora/svotool/-/tree/master/SvoTool/Svo
  
  社区内早期对 Svo 解析实现的参考之一。

- **GekiChuMaiLocalizedDocument — SVO 格式页**
  https://gzlxz190614.github.io/GekiChuMaiLocalizedDocument/Chunithm/Extra/svo/svo_format.html
  
  由社区整理的 Svo 格式说明，本工具在结构确认阶段与其交叉验证。并且仓库内的文档来源于此项目。

  
  ---
  
 ### 在此致谢
 - DeepSeek V4
 - Tencent WorkBuddy(Hy3)
 - GzLxz190614
 - MikiraSora
 
 ---
 本文档由DeepSeek V4生成后调整而来，如有雷同纯属巧合。
 
 本项目基于MIT License 许可证开源。
 
