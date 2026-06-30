# ADR-0002: 桌面外壳用 pywebview，技术栈用 Python

- 状态：已接受
- 日期：2026-06-08

## 背景
工具需在 Windows 上以桌面 GUI 形态运行，面向编程开发，并要支持多模态。
需选定 UI 形态、外壳技术与实现语言。

## 决策
- **形态**：桌面 GUI。
- **语言**：Python 3.11+。AI/多模态生态最全（各家 SDK、OCR、文档解析、向量库），
  Windows 上 pip 即可，个人快速迭代成本最低。
- **外壳**：pywebview（系统 WebView2 + JS↔Python 桥）。

## 备选与权衡
| 方案 | 取舍 |
|---|---|
| **pywebview（选中）** | 轻量、纯 Python 启动；Web 前端做 markdown/代码/流式渲染最省力；后期可换 Tauri 外壳而不动内核 |
| PySide6 / Qt | 更原生，但富文本/代码流式渲染要多写很多代码 |
| Electron | 渲染能力强但体积大、需 Node 双栈 |
| Tauri (Rust) | 体积/性能最优但开发慢，AI 库需 FFI/调 Python |

## 结果
- 前端用 Web 技术，迭代快。
- 依赖系统 WebView2 运行时（Win10/11 一般自带）。
- 保留后期更换外壳（如 Tauri + Python sidecar）的空间。
