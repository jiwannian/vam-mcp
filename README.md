# VaM MCP

Windows 上的 VaM 插件开发与测试 MCP。通过 Python stdio MCP 和 BepInEx 桥接，提供 18 个工具；不会修改游戏本体程序集。

Windows 测试进程通过系统自带的 WMI/CIM 启动，关闭或重连 MCP 后仍保留，可用 `vam_instance(action="connect", instance_id=...)` 继续使用。停止实例需要显式调用 `stop`。

## 安装与启动

要求 Python 3.10+、VaM、BepInEx 5、游戏附带的 .NET Framework 编译器/引用程序集及 Codex。安装 Python 依赖并注册 MCP：

```powershell
python -m pip install -r requirements.txt
.\scripts\Install.ps1 -GameRoot "C:\VaM"
```

将 `C:\VaM` 替换成 VaM 安装目录。安装器构建桥接并注册 Codex 的 `vam` MCP；不从网络下载 VaM 或游戏程序集。构建产物使用游戏随附 CLR 2.0 引用，不兼容默认 .NET 8 引用。

安装后重新打开 Codex 聊天或重启 Codex，使新增 MCP 被载入。MCP 服务启动不会自动启动 VaM。

在 Codex 中直接提出：

> 使用 vam MCP 启动测试实例，部署这个插件，修改参数，重载并检查错误和实际截图。

工具调用顺序：

1. `vam_capabilities` 识别插件目标；`vam_status` 检查环境；`vam_instance(action="start")` 返回 `host_...` 任务。
2. 用 `vam_job(action="wait", job_id=...)` 等待，取得 `instance_id`。
3. `vam_api(action="search", query=...)` 查询实际 VaM API；`action="type"` 读取签名及反编译片段。已安装插件 DLL 可用 `assembly_path` 查询。
4. `vam_plugin(action="compile", profile="mvrscript", entrypoint=...)` 检查原生脚本；`deploy` 指定包目录名，取得游戏相对入口。
5. `vam_plugin(action="load", instance_id=..., atom_uid=..., entrypoint=...)` 加载，返回插件槽和脚本 `storable_id`。
6. 优先用 `vam_storable` 调用 Action 或修改参数。未注册的控件用 `vam_plugin(action="open_ui")`、`vam_ui_tree`、`vam_ui_action`。
7. `vam_capture` 返回实际图片和本地 PNG；`vam_logs` 增量读取本次操作后的错误。

每次加载、卸载、重载插件或切换场景后，重新查询 UI 句柄。多个测试实例同时存在时必须传入 `instance_id`。当前普通启动的 VaM 不会打开桥接端口；桥接仅在带专用启动标记的测试实例中启用。

## 插件目标

先调用 `vam_capabilities` 查看配置文件、当前游戏/编译器依赖是否齐全，以及是否可进行 live 验证。

| Profile | 输入与构建 | 部署 | 生命周期与验证 |
|---|---|---|---|
| `mvrscript` | `.cs/.cslist`；VaM CLR 引用；C# 5；可传 `reference_paths`、`artifact_files`、`debug` | `Custom/Scripts/<package>`；资源与配置按 source root 相对路径部署 | `MVRPluginManager` 加载/热重载；参数、uGUI、截图、日志、probe |
| `bepinex` | `.cs/.cslist`；VaM 与 BepInEx 引用；C# 5；生成 DLL，可部署 PDB、依赖和资源 | `BepInEx/plugins/<package>` | `vam_instance(action="restart")` 重启同一测试实例后，`vam_plugin(action="verify", profile="bepinex", plugin_uid=<GUID>)` 检查 Chainloader；用 `vam_logs` 检查 BepInEx 日志 |
| `var_bundle` | 预制 `.var` ZIP 归档；不编译或制作归档 | `AddonPackages/<archive>.var` | 校验 `meta.json` 与成员路径；从 `Custom/Scripts/*.cs` 返回 VaM 包内路径候选，可原生加载/热重载，随后截图/日志/probe |

显式 DLL 引用和资源路径可相对 `source_root`、VaM 根目录或绝对路径提供。部署返回 `deployment_id` 和逐文件 SHA-256 清单；`vam_plugin(action="rollback", deployment_id=...)` 仅回滚当前哈希仍与该部署相符的文件，若文件后来被改动则返回冲突。`debug=true` 启用调试编译；BepInEx DLL 部署会附带 PDB。VAR 的 `compile`/`deploy` 会返回归档内脚本候选路径，可传给 `vam_plugin(action="load", profile="var_bundle", ...)`。

BepInEx 示例流程：用 `profile="bepinex"` 编译 `examples/BepInExFixture.cs` 并部署，随后 `vam_instance(action="restart", instance_id=...)`，再按源码中的 `[BepInPlugin]` GUID `codex.vam.mcp.fixture` 调用 `vam_plugin(action="verify", profile="bepinex", instance_id=..., plugin_uid=...)`。重启保留实例 ID 和 saves 目录，但会重新启动 VaM 进程；若在退出请求提交后取消，原进程仍可能继续退出。`vam_test` 也可用 `operation="bepinex"` 对加载器注册表做断言。

## 工具

| 工具 | 能力 |
|---|---|
| `vam_capabilities` | 插件目标、工具链、打包部署、生命周期和验证能力 |
| `vam_status` | 环境、版本、实例运行状态 |
| `vam_instance` | 安装、启动、连接、列举、停止、重启测试实例 |
| `vam_api` | 按实际程序集哈希缓存的类型与成员查询 |
| `vam_plugin` | 按 profile 构建、部署、核验、回滚或操作原生插件生命周期 |
| `vam_scene` | 检查、新建、载入、保存原生场景 |
| `vam_atom` | 列举、创建、克隆、移除、调整变换 |
| `vam_storable` | 参数元数据、范围、可选值、读写、Action |
| `vam_morph` | Person Morph 的查询与有界读写 |
| `vam_ui_tree` | 控件树、当前标签、值、状态、位置和句柄 |
| `vam_ui_action` | 按钮回调、输入提交、开关、滑块、选择器与滚动 |
| `vam_file_dialog` | 原生文件／目录对话框的检查、选择与取消 |
| `vam_capture` | 实际 UI 帧缓冲截图、指定人物及五方向相机渲染 |
| `vam_logs` | Unity、BepInEx、VaM 内部消息与错误 |
| `vam_wait` | 等待就绪、参数、控件、文件及 JSON 条件 |
| `vam_probe` | 在 Unity 主线程执行临时 C# 诊断，支持反射、协程及 finally |
| `vam_test` | 执行带引用、断言、清理与故障证据的测试流程 |
| `vam_job` | 查询、等待和取消任务 |

## 可重复测试

示例：将 `examples/smoke.json` 交给 `vam_test(spec_path=..., instance_id=...)`。相对插件路径以 MCP 工程根目录解析。任务会部署示例插件、创建对象、调用 Action、核对参数、打开界面、截图并清理。

测试由 `steps` 和可选 `cleanup` 组成。步骤具有 `id`、`operation`、`arguments`、`timeout_s`、`expect`；用 `{"$ref":"steps.load.scripts.0.storable_id"}` 引用前一步结果，保留原有 JSON 类型。

```json
{
  "name": "参数回归",
  "steps": [
    {
      "id": "ready",
      "operation": "status",
      "expect": [{"path": "ready", "equals": true}]
    }
  ]
}
```

原生操作可作为步骤；还支持 `compile`、`deploy`、`probe`、`wait`。断言支持点分隔 JSON 路径、`equals`、`contains`、`exists`、`min`、`max`、`approx` 和 `tolerance`。`cleanup` 在通过、失败及取消后执行，使用原生操作与已取得的引用；清理失败会让测试失败，并保留现场记录。不会默认停止或回滚插件已经触发的第三方异步工作。

每次测试在 `runtime\runs\run_*` 保存流程、结果、增量日志和失败截图。编译或原生加载错误明确返回失败；静态编译通过不等于游戏初始化成功，功能断言通过不等于视觉质量达标。

临时诊断的 `code` 是 C# 5 的 IEnumerator 方法体，可使用 `args`、`result`、可选 `containingAtom`、`GetAtomById(uid)`，并可 `yield return null`。反射示例：

```csharp
var field = typeof(SuperController).GetField(
    "errorLog", BindingFlags.Instance | BindingFlags.NonPublic);
result["found"].AsBool = field != null;
```

诊断协程在完成、超时或取消时 Dispose，执行 finally。Unity 的主线程若被同步死循环或同步阻塞代码占用，协程超时无法抢占，可用 `vam_instance(action="stop", force=true)` 结束对应测试实例。每个诊断程序集使用独立名称；Mono 不支持单独卸载程序集，它随测试进程退出释放。VaM 的 SimpleJSON 会把诊断结果标量序列化为字符串；常规工具已将已知数值、布尔字段转换为 JSON 类型。

## 测试与更新

- `scripts\Test.ps1`：主机单元测试；需要 Windows .NET Framework C# 编译器，不需要运行中的 VaM。
- `scripts\Test.ps1 -Live`：全部测试，包含真实游戏与 stdio MCP 集成；需要已启动的桥接测试实例。
- `scripts\Accept-Instances.py`：从无运行实例开始，验证 stdio 启动、关闭 MCP 后重连、双实例场景独立和停止；完成后保留第一个测试实例。
- `scripts\Build.ps1 -GameRoot <VaM目录>`：仅编译；`-Install` 编译并更新桥接。
- `scripts\Uninstall.ps1`：移除 Codex 注册；`-RemoveBridge -GameRoot <VaM目录>` 同时移除桥接 DLL。

`VAM_GAME_ROOT` 由安装脚本写入 Codex MCP 环境。可通过 `$env:VAM_GAME_ROOT` 或 `-GameRoot` 指定本地游戏目录。更新游戏桥前停止 MCP 测试实例；更新 Python 服务后重启 Codex 的 MCP 连接。测试范围见 `VALIDATION.md`。

`scripts\Test.ps1` 执行 host-only 测试时会排除 live 测试；直接运行 `pytest` 时，如果没有 VaM 程序集，API 索引测试会自动跳过。

构建按源码、编译器和引用程序集哈希缓存；源码未变时可以在实例运行中重复安装。注册和卸载脚本仅更新 `mcp_servers.vam`，保留其他配置及备份。

## 运行边界

按用户选择，各测试进程共用游戏目录，专用场景位于 `Saves\VaMMcp`，日志和截图分实例保存。部署只覆盖清单内的文件并保留旧文件；共享目录的 MCP 写操作通过跨进程锁串行执行。这个锁不能约束普通游戏实例或其他外部程序；生成资源和插件固定数据路径仍然共用。

场景保存和克隆暂存使用 VaM 允许的游戏内路径。场景载入、资源访问遵循游戏原有的路径和插件检查；工具不会更改全局权限设置。文件选择要求对话框已经打开，并检查文件存在及扩展名。

UI 操作调用真实 Unity 控件回调，检查控件存活、激活及 enabled/interactable 状态，不等于模拟鼠标命中测试。控件树中的屏幕边界可结合实际截图检查裁切、遮挡和排版；游戏自带翻译插件会改变显示标签。自绘 IMGUI、原生 Windows 弹窗和不暴露 uGUI 控件的第三方界面，使用截图及 C# 诊断补充检查。

桥接使用本机 TCP、随机令牌、请求编号、串行主线程队列和可查询任务；不会自动重放断线时的写请求。启动默认 180 秒，普通操作默认 30 秒，测试默认 600 秒；等待工具每次最多等待 45 秒。取消不回滚已完成的游戏操作。实例 PID 与进程创建时间共同校验，停止操作仅针对 MCP 启动的测试实例。

完整日志保存在 `runtime\instances\vam_*\events.jsonl`，增量工具保留最近 4000 条；重启同一测试实例时日志游标保持单调。令牌与 Bearer 头被遮盖，密码控件不返回明文值。桥接描述文件包含本机连接令牌，日常工具只返回不含令牌的状态。
