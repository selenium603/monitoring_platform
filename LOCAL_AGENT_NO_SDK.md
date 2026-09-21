# 不使用 SDK 评测本地 Agent

`tools/local_agent_eval.py` 是一个只使用 Python 标准库的本地适配器。它运行你的 Agent，收集输入、输出、耗时和错误，通过 PandaProbe REST API 创建 Trace，并可立即发起 LLM 评测。你的 Agent 本身不需要引入 PandaProbe SDK。

## 方式一：Agent 是本地命令

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

## 方式二：Agent 是本地 HTTP 接口

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
