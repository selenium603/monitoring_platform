# 不使用 SDK 评测本地 Agent

`tools/local_agent_eval.py` 是一个只使用 Python 标准库的本地适配器。它运行你的 Agent，收集输入、输出、耗时和错误，通过 PandaProbe REST API 创建 Trace，并可立即发起 LLM 评测。你的 Agent 本身不需要引入 PandaProbe SDK。

## 方式一：加载 LangChain/LangGraph 项目（推荐）

如果 Agent 目录中有 `langgraph.json`，只需把目录传给 `--agent-dir`。适配器会自动读取 Graph 入口、项目 `.env`、`src` 源码目录和默认运行时 Context，并捕获 LLM、工具、检索器和节点事件，不需要修改 Agent 代码。

本仓库中的 `react-agent-main` 可以这样运行：

```powershell
.\Run-LangChainAgent.ps1 `
  -AgentDir "C:\Users\carol\Desktop\pandaprobe-main\pandaprobe-main\test\react-agent-main\react-agent-main" `
  -Prompt "Who founded LangChain?" `
  -Name "ReAct Search Agent" `
  -Evaluate
```

启动脚本会优先使用 Agent 目录中的 `.venv` 或 `venv`，避免不同 Agent 的依赖相互冲突。

项目需要先有自己的 `.env`，并在运行适配器的 Python 环境中安装它自身的依赖。如果 `langgraph.json` 定义了多个 Graph，用 `--graph-id agent` 指定其中一个。需要覆盖 Context 时，可以传入 JSON：

```powershell
python tools/local_agent_eval.py `
  --agent-dir "C:\path\to\langgraph-agent" `
  --context-json '{"model":"openai/gpt-4o-mini","max_search_results":5}' `
  --prompt "Search for the latest LangGraph release"
```

不写 `--metric` 时只运行 Agent 并记录完整 Trace，不会产生评测模型费用。

普通 LangChain `AgentExecutor`、Runnable 或自定义工厂没有 `langgraph.json` 时，用 `-Entrypoint` 指定入口。适配器会读取输入 Schema；也可以明确指定输入和输出：

```powershell
.\Run-LangChainAgent.ps1 `
  -AgentDir "C:\path\to\langchain-agent" `
  -Entrypoint "my_agent.main:agent_executor" `
  -InputMode input `
  -OutputPath output `
  -Prompt "请完成这个任务" `
  -Evaluate
```

常用参数：

| 参数 | 用途 |
|---|---|
| `-Entrypoint` | `模块:属性`、`模块:工厂函数` 或 `文件.py:属性` |
| `-InputMode` | `auto`、`messages`、`input`、`query`、`question`、`prompt` 或 `string` |
| `-InputJson` | 完全自定义输入，例如 `{"task":"{prompt}"}` |
| `-OutputPath` | 最终回答路径，例如 `output` 或 `data.answer` |
| `-FactoryJson` | 创建 Agent 工厂函数所需的关键字参数 |
| `-ConfigJson` | LangChain `RunnableConfig`，包括 tags、metadata、configurable |
| `-ContextJson` | LangGraph Runtime Context 参数 |
| `-ThreadId` | 带 Checkpointer 的 LangGraph 会话 ID |

适配器优先使用 `astream_events(v2)` 捕获内部 LLM、工具、检索和 Chain。只有 `invoke/ainvoke` 的旧式 Runnable 也能运行和评测，但只能记录最外层调用。

## 方式二：直接导入单个 LangGraph 入口

如果 Agent 对外提供一个返回已编译 graph 的函数，可以让适配器直接捕获 LLM、工具、检索器和节点事件。项目里的 `test.py` 已提供 `build_agent()`，运行：

最简单的运行方式：

```powershell
.\Test-LangGraphAgent.ps1 "What's the weather in Tokyo?"
```

同时运行三个适合工具型 Agent 的评测指标：

```powershell
.\Test-LangGraphAgent.ps1 "What's the weather in Tokyo?" -Evaluate
```

等价的完整命令是：

```powershell
python tools/local_agent_eval.py `
  --langgraph test:build_agent `
  --env-file backend/.env.development `
  --prompt "What's the weather in Tokyo?" `
  --metric task_completion `
  --metric tool_correctness `
  --metric argument_correctness
```

此模式会把 `on_tool_start/end`、`on_chat_model_start/end`、`on_chain_start/end` 和检索事件转换成 PandaProbe Spans。

## 创建并评测 Session

多次运行时传入相同的 Session ID，PandaProbe 会自动把这些 Trace 合并为一个 Session：

```powershell
.\Test-LangGraphAgent.ps1 "What's the weather in Tokyo?" -SessionId weather-demo
.\Test-LangGraphAgent.ps1 "What about Shanghai?" -SessionId weather-demo
```

标准 LangGraph 项目同样使用 `--session-id`：

```powershell
python tools/local_agent_eval.py `
  --agent-dir "C:\path\to\langgraph-agent" `
  --prompt "First test question" `
  --session-id "agent-comparison-01"
```

随后在网页的 **Session** 页面选择 `weather-demo`，点击 **评估**，选择 **Agent 一致性** 或 **Agent 可靠性**。

## 方式三：Agent 是本地命令

Agent 从标准输入读取问题，并把最终答案输出到标准输出：

```powershell
python tools/local_agent_eval.py `
  --command "python C:\path\to\my_agent.py" `
  --prompt "请总结这个项目的作用"
```

如果 Agent 要求问题作为命令参数，可在命令里写 `{prompt}`：

```powershell
python tools/local_agent_eval.py `
  --command 'python C:\path\to\my_agent.py --question "{prompt}"' `
  --prompt "请总结这个项目的作用"
```

## 方式四：Agent 是本地 HTTP 接口

默认发送 `{"prompt": "..."}`。下面的例子从响应 `{"data": {"answer": "..."}}` 中取出答案：

```powershell
python tools/local_agent_eval.py `
  --agent-url http://127.0.0.1:9000/chat `
  --response-field data.answer `
  --prompt "请总结这个项目的作用"
```

## 同时运行评测

增加一个或多个 `--metric`。不写 `--judge-model` 时，会使用 `backend/.env.development` 中的 `EVAL_LLM_MODEL`：

```powershell
python tools/local_agent_eval.py `
  --command "python C:\path\to\my_agent.py" `
  --prompt "请总结这个项目的作用" `
  --metric task_completion
```

可用指标可在 PandaProbe 的 **Evaluations** 页面查看，也可以打开 `http://localhost:8000/evaluations/trace-metrics`。

适配器默认自动寻找本机 `onboarding` 项目。如果页面左上角显示的是其他项目名，加上 `--project-name "项目名"`。部署到启用登录验证的环境时，设置 `$env:PANDAPROBE_API_KEY`；适配器不会打印这个密钥。

运行后，到以下页面查看结果：

- **Traces**：Agent 的输入、输出、耗时和错误
- **Evaluations**：评测进度、分数和判定理由
- **Sessions**：传入相同 `--session-id` 的多次运行
- **Analytics**：累计趋势
