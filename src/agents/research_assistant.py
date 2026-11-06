"""研究助手：Agent 层的 LangChain 组件与 LangGraph 执行图示例。

LangChain 提供消息、模型接口和工具；LangGraph 管理共享 State、节点与条件分支。
主流程为输入检查 → 模型 → 工具 → 模型，直到模型不再请求工具或达到步数限制。
"""

from datetime import datetime
from typing import Literal

from langchain_community.tools import DuckDuckGoSearchResults, OpenWeatherMapQueryRun
from langchain_community.utilities import OpenWeatherMapAPIWrapper
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda, RunnableSerializable
from langgraph.graph import END, MessagesState, StateGraph
from langgraph.managed import RemainingSteps
from langgraph.prebuilt import ToolNode

from agents.safeguard import Safeguard, SafeguardOutput, SafetyAssessment
from agents.tools import calculator
from core import get_model, settings


# LangGraph：继承的 messages 使用消息合并规则；total=False 允许字段逐步补齐。
class AgentState(MessagesState, total=False):
    """`total=False` is PEP589 specs.

    documentation: https://typing.readthedocs.io/en/latest/spec/typeddict.html#totality
    """

    safety: SafeguardOutput
    remaining_steps: RemainingSteps  # LangGraph 管理的执行预算，不是 LLM 的 token 余额。


# LangChain Community：把搜索服务封装为 Tool，供模型请求、ToolNode 执行。
web_search = DuckDuckGoSearchResults(name="WebSearch")
tools = [web_search, calculator]

# Add weather tool if API key is set
# Register for an API key at https://openweathermap.org/api/
if settings.OPENWEATHERMAP_API_KEY:
    wrapper = OpenWeatherMapAPIWrapper(
        openweathermap_api_key=settings.OPENWEATHERMAP_API_KEY.get_secret_value()
    )
    tools.append(OpenWeatherMapQueryRun(name="Weather", api_wrapper=wrapper))

current_date = datetime.now().strftime("%B %d, %Y")
instructions = f"""
    You are a helpful research assistant with the ability to search the web and use other tools.
    Today's date is {current_date}.

    NOTE: THE USER CAN'T SEE THE TOOL RESPONSE.

    A few things to remember:
    - Please include markdown-formatted links to any citations used in your response. Only include one
    or two citations per response unless more are needed. ONLY USE LINKS RETURNED BY THE TOOLS.
    - Use calculator tool with numexpr to answer math questions. The user does not understand numexpr,
      so for the final response, use human readable format - e.g. "300 * 200", not "(300 \\times 200)".
    """


def wrap_model(model: BaseChatModel) -> RunnableSerializable[AgentState, AIMessage]:
    # LangChain bind_tools：向模型提供工具名称、说明和参数结构，此处不执行工具。
    bound_model = model.bind_tools(tools)
    # LangChain Runnable：把 State 转成消息列表；SystemMessage 提供角色和行为要求。
    preprocessor = RunnableLambda(
        lambda state: [SystemMessage(content=instructions)] + state["messages"],
        name="StateModifier",
    )
    # LangChain 的 | 组合执行顺序：先准备消息，再调用绑定了工具的模型。
    return preprocessor | bound_model  # type: ignore[return-value]


def format_safety_message(safety: SafeguardOutput) -> AIMessage:
    content = (
        f"This conversation was flagged for unsafe content: {', '.join(safety.unsafe_categories)}"
    )
    return AIMessage(content=content)


async def acall_model(state: AgentState, config: RunnableConfig) -> AgentState:
    m = get_model(config["configurable"].get("model", settings.DEFAULT_MODEL))
    model_runnable = wrap_model(m)
    # Python async/await：等待模型响应时让出事件循环；返回 AIMessage，可能含 tool_calls。
    response = await model_runnable.ainvoke(state, config)

    if state["remaining_steps"] < 2 and response.tool_calls:
        return {
            "messages": [
                AIMessage(
                    id=response.id,
                    content="Sorry, need more steps to process this request.",
                )
            ]
        }
    # LangGraph 合并节点更新：新消息通常追加，同 ID 的消息可替换；其他 State 字段保留。
    return {"messages": [response]}


async def safeguard_input(state: AgentState, config: RunnableConfig) -> AgentState:
    # 项目自定义检查组件：未配置 GROQ_API_KEY 时跳过检查并返回 SAFE，详见 safeguard.py。
    safeguard = Safeguard()
    safety_output = await safeguard.ainvoke(state["messages"])
    return {"safety": safety_output, "messages": []}


async def block_unsafe_content(state: AgentState, config: RunnableConfig) -> AgentState:
    safety: SafeguardOutput = state["safety"]
    return {"messages": [format_safety_message(safety)]}


# LangGraph StateGraph：注册节点并指定共享状态结构，此时还没有执行用户请求。
agent = StateGraph(AgentState)
agent.add_node("model", acall_model)
# LangGraph ToolNode：执行 AIMessage.tool_calls，并把结果写为对应的 ToolMessage。
agent.add_node("tools", ToolNode(tools))
agent.add_node("guard_input", safeguard_input)
agent.add_node("block_unsafe_content", block_unsafe_content)
agent.set_entry_point("guard_input")


# Check for unsafe input and block further processing if found
def check_safety(state: AgentState) -> Literal["unsafe", "safe"]:
    safety: SafeguardOutput = state["safety"]
    match safety.safety_assessment:
        case SafetyAssessment.UNSAFE:
            return "unsafe"
        case _:
            return "safe"


# LangGraph 条件边：依据检查结果选路；当前实现的 ERROR 也走 safe 分支。
agent.add_conditional_edges(
    "guard_input", check_safety, {"unsafe": "block_unsafe_content", "safe": "model"}
)

# Always END after blocking unsafe content
agent.add_edge("block_unsafe_content", END)

# LangGraph 普通边：工具结果进入 State 后再次交给模型，生成回答或发起下一轮调用。
agent.add_edge("tools", "model")


# After "model", if there are tool calls, run "tools". Otherwise END.
def pending_tool_calls(state: AgentState) -> Literal["tools", "done"]:
    # 读取模型的结构化工具请求决定分支；工具的实际执行发生在 ToolNode。
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        raise TypeError(f"Expected AIMessage, got {type(last_message)}")
    if last_message.tool_calls:
        return "tools"
    return "done"


agent.add_conditional_edges("model", pending_tool_calls, {"tools": "tools", "done": END})


# LangGraph compile：生成可被 ainvoke/astream 调用的图；注册入口见 agents.py。
research_assistant = agent.compile()
