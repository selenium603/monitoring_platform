import os

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI


def get_weather(city: str) -> str:
    """Get weather for a given city."""
    return f"It's always sunny in {city}!"


def build_agent():
    """Build the agent so an external runner can consume its event stream."""
    # 通过 OpenRouter 调用 gpt-5.6-terra（key 从环境变量读取）
    chat = ChatOpenAI(
        model="openai/gpt-5.6-terra",
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        temperature=0,
    )

    return create_agent(
        model=chat,
        tools=[get_weather],
        system_prompt="You are a helpful assistant",
    )


def main() -> None:
    # 适配器会把 --prompt 通过标准输入传进来
    question = input().strip()
    if not question:
        question = "What's the weather in Tokyo?"

    agent = build_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    answer = result["messages"][-1].content
    print(answer)


if __name__ == "__main__":
    main()
